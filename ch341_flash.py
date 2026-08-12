"""
CH341A SPI Flash Programmer

A Python utility for reading, erasing, and programming SPI NOR flash
using a CH341A USB programmer.

Supported operations:
    - Read JEDEC ID (0x9F)
    - Read data (0x03)
    - Sector erase (0x20, 4 KiB)
    - Page program (0x02, up to 256 bytes per page)
    - Write enable (0x06)
    - Read status register (0x05)

The CH341StreamSPI4 function is limited to 32-byte transactions,
so all larger transfers are automatically split into safe chunks.

NOTE:
    This code is intended for educational and engineering use.
    Always back up the original flash contents before performing
    destructive operations.
"""

import ctypes
import logging
import time
from pathlib import Path
from typing import List, Optional, Union

# ----------------------------------------------------------------------
# Constants
# ----------------------------------------------------------------------

DLL_PATH = r"C:\Windows\System32\CH341DLLA64.DLL"

# SPI mode: MSB first, lowest SPI clock, SPI communication
CH341_SPI_MODE = 0x80

# CH341StreamSPI4 maximum transaction size (bytes)
CH341_MAX_TRANSFER = 32

# Flash commands
CMD_WRITE_ENABLE  = 0x06
CMD_READ_STATUS   = 0x05
CMD_PAGE_PROGRAM  = 0x02
CMD_READ          = 0x03
CMD_SECTOR_ERASE  = 0x20   # 4 KiB sector erase
CMD_JEDEC_ID      = 0x9F

# Flash geometry (W25Q32 / compatible)
FLASH_SIZE = 4 * 1024 * 1024   # 4 MiB
SECTOR_SIZE = 4 * 1024         # 4 KiB
PAGE_SIZE = 256                # 256 bytes per page

# ----------------------------------------------------------------------
# Logging setup
# ----------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger("ch341_flash")


# ----------------------------------------------------------------------
# Exceptions
# ----------------------------------------------------------------------

class CH341Error(Exception):
    """Base exception for CH341-related errors."""
    pass


class CH341OpenError(CH341Error):
    """Raised when the CH341 device cannot be opened."""
    pass


class CH341TransferError(CH341Error):
    """Raised when a CH341 SPI transfer fails."""
    pass


class FlashError(Exception):
    """Base exception for flash operations."""
    pass


class FlashTimeoutError(FlashError):
    """Raised when the flash does not become ready in time."""
    pass


class FlashVerifyError(FlashError):
    """Raised when read-back data does not match the written data."""
    pass


# ----------------------------------------------------------------------
# Low-level CH341 wrapper
# ----------------------------------------------------------------------

class CH341:
    """
    Thin wrapper around the CH341 DLL.
    """

    def __init__(self, dll_path: str = DLL_PATH, index: int = 0):
        self.dll_path = dll_path
        self.index = index
        self._dll = None
        self._handle = None

    # ------------------------------------------------------------------
    # DLL loading
    # ------------------------------------------------------------------
    def _load_dll(self) -> None:
        """Load the CH341 DLL and configure ctypes signatures."""
        try:
            self._dll = ctypes.WinDLL(self.dll_path)
        except OSError as exc:
            raise CH341OpenError(
                f"Failed to load CH341 DLL from {self.dll_path}: {exc}"
            ) from exc

        # CH341OpenDevice
        self._dll.CH341OpenDevice.argtypes = [ctypes.c_ulong]
        self._dll.CH341OpenDevice.restype = ctypes.c_ulong

        # CH341CloseDevice
        self._dll.CH341CloseDevice.argtypes = [ctypes.c_ulong]
        self._dll.CH341CloseDevice.restype = None

        # CH341SetStream
        self._dll.CH341SetStream.argtypes = [
            ctypes.c_ulong,
            ctypes.c_ulong
        ]
        self._dll.CH341SetStream.restype = ctypes.c_ulong

        # CH341StreamSPI4
        self._dll.CH341StreamSPI4.argtypes = [
            ctypes.c_ulong,               # iIndex
            ctypes.c_ulong,               # iChipSelect
            ctypes.c_ulong,               # iLength
            ctypes.POINTER(ctypes.c_ubyte) # ioBuffer
        ]
        self._dll.CH341StreamSPI4.restype = ctypes.c_ulong

    # ------------------------------------------------------------------
    # Device management
    # ------------------------------------------------------------------
    def open(self) -> None:
        """Open the CH341 device and configure SPI stream mode."""
        if self._dll is None:
            self._load_dll()

        self._handle = self._dll.CH341OpenDevice(self.index)
        if self._handle == 0:
            raise CH341OpenError(
                f"CH341OpenDevice({self.index}) returned 0 (no device?)"
            )

        log.info("CH341 device opened (handle=%d)", self._handle)

        ret = self._dll.CH341SetStream(self.index, CH341_SPI_MODE)
        if ret == 0:
            self.close()
            raise CH341OpenError("CH341SetStream failed")

        log.debug("SPI stream mode set to 0x%02X", CH341_SPI_MODE)

    def close(self) -> None:
        """Close the CH341 device."""
        if self._dll is not None and self._handle is not None:
            self._dll.CH341CloseDevice(self.index)
            self._handle = None
            log.info("CH341 device closed")

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    # ------------------------------------------------------------------
    # SPI transfer
    # ------------------------------------------------------------------
    def transfer(self, data: bytes, cs: int = 0x80) -> bytes:
        """
        Perform a full-duplex SPI transfer.

        :param data: bytes to send (will be replaced with received data)
        :param cs:   chip-select value (default 0x80 = CS0)
        :return:     received bytes
        :raises:     CH341TransferError if the transfer fails
        """
        if not self._dll or not self._handle:
            raise CH341Error("Device not open")

        length = len(data)
        if length > CH341_MAX_TRANSFER:
            raise CH341TransferError(
                f"Transfer length {length} exceeds CH341 limit "
                f"of {CH341_MAX_TRANSFER} bytes"
            )

        buf = (ctypes.c_ubyte * length).from_buffer_copy(data)
        ret = self._dll.CH341StreamSPI4(self.index, cs, length, buf)

        if ret == 0:
            raise CH341TransferError(
                f"CH341StreamSPI4 failed for {length} bytes"
            )

        return bytes(buf)


# ----------------------------------------------------------------------
# SPI Flash operations
# ----------------------------------------------------------------------

class SPIFlash:
    """
    High-level SPI NOR flash programmer using a CH341 instance.
    """

    def __init__(self, ch341: CH341):
        self.ch341 = ch341

    # ------------------------------------------------------------------
    # Basic SPI command helpers
    # ------------------------------------------------------------------
    def _send_cmd(self, cmd: int, *args: int) -> bytes:
        """Send a command byte followed by optional bytes."""
        data = bytes([cmd]) + bytes(args)
        return self.ch341.transfer(data)

    def read_status(self) -> int:
        """Read the Status Register-1."""
        rx = self.ch341.transfer(bytes([CMD_READ_STATUS, 0x00]))
        return rx[1]

    def wait_until_ready(self, timeout: float = 5.0) -> int:
        """
        Wait until the flash is no longer busy.

        :param timeout: maximum time to wait in seconds
        :return:        final status register value
        :raises:        FlashTimeoutError if the flash stays busy
        """
        start = time.monotonic()
        while True:
            status = self.read_status()
            if not (status & 0x01):  # BUSY bit = 0
                return status
            if time.monotonic() - start > timeout:
                raise FlashTimeoutError(
                    "Flash remained busy for too long "
                    f"(timeout={timeout:.1f}s)"
                )
            time.sleep(0.01)

    def write_enable(self) -> None:
        """Send Write Enable (0x06) and verify WEL bit."""
        self._send_cmd(CMD_WRITE_ENABLE)
        status = self.read_status()
        if not (status & 0x02):  # WEL bit
            raise FlashError(
                "Write Enable Latch (WEL) not set after 0x06"
            )
        log.debug("Write Enable successful (status=0x%02X)", status)

    # ------------------------------------------------------------------
    # Identification
    # ------------------------------------------------------------------
    def read_jedec_id(self) -> bytes:
        """
        Read the 3-byte JEDEC manufacturer/device ID.

        :return: bytes (manufacturer, memory type, capacity)
        """
        rx = self.ch341.transfer(bytes([CMD_JEDEC_ID, 0x00, 0x00, 0x00]))
        # First byte is the command response, next 3 are ID
        return rx[1:4]

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------
    def read(self, address: int, length: int) -> bytes:
        """
        Read flash data using 0x03 READ.

        :param address: 24-bit start address
        :param length:  number of bytes to read
        :return:        bytes read
        """
        result = bytearray()
        max_data = CH341_MAX_TRANSFER - 4  # 4 bytes for cmd + address

        while length > 0:
            n = min(length, max_data)
            tx = bytearray(4 + n)
            tx[0] = CMD_READ
            tx[1] = (address >> 16) & 0xFF
            tx[2] = (address >> 8) & 0xFF
            tx[3] = address & 0xFF

            rx = self.ch341.transfer(bytes(tx))
            result.extend(rx[4:4 + n])

            address += n
            length -= n

        return bytes(result)

    # ------------------------------------------------------------------
    # Erase
    # ------------------------------------------------------------------
    def sector_erase(self, address: int) -> None:
        """
        Erase a 4 KiB sector using 0x20.

        :param address: any address within the sector
        """
        tx = bytearray(4)
        tx[0] = CMD_SECTOR_ERASE
        tx[1] = (address >> 16) & 0xFF
        tx[2] = (address >> 8) & 0xFF
        tx[3] = address & 0xFF

        self.write_enable()
        self.ch341.transfer(bytes(tx))
        self.wait_until_ready()
        log.info("Sector erased at 0x%06X", address)

    # ------------------------------------------------------------------
    # Program
    # ------------------------------------------------------------------
    def program_page(self, address: int, data: bytes) -> None:
        """
        Program up to 256 bytes within a single page using 0x02.

        :param address: start address (must be within a page)
        :param data:    bytes to program (<= PAGE_SIZE)
        """
        if len(data) > PAGE_SIZE:
            raise ValueError(
                f"Page program size cannot exceed {PAGE_SIZE} bytes"
            )

        # Page program requires address + data to stay within page boundary
        page_start = address // PAGE_SIZE * PAGE_SIZE
        if address + len(data) > page_start + PAGE_SIZE:
            raise ValueError("Data crosses page boundary")

        max_data = CH341_MAX_TRANSFER - 4  # 1 cmd + 3 address bytes

        offset = 0
        while offset < len(data):
            chunk_size = min(max_data, len(data) - offset)
            chunk = data[offset:offset + chunk_size]
            current_addr = address + offset

            # Write enable is required before EACH program operation
            self.write_enable()

            tx = bytearray(4 + chunk_size)
            tx[0] = CMD_PAGE_PROGRAM
            tx[1] = (current_addr >> 16) & 0xFF
            tx[2] = (current_addr >> 8) & 0xFF
            tx[3] = current_addr & 0xFF
            tx[4:] = chunk

            self.ch341.transfer(bytes(tx))
            self.wait_until_ready()

            offset += chunk_size

        log.info("Programmed %d bytes at 0x%06X", len(data), address)

    # ------------------------------------------------------------------
    # High-level combined operations
    # ------------------------------------------------------------------
    def erase_sectors(self, address: int, length: int) -> None:
        """Erase all sectors covering the given address range."""
        start_sector = address // SECTOR_SIZE
        end_sector = (address + length + SECTOR_SIZE - 1) // SECTOR_SIZE

        for sector in range(start_sector, end_sector):
            self.sector_erase(sector * SECTOR_SIZE)

    def write(self, address: int, data: bytes) -> None:
        """
        Write arbitrary data, handling page boundaries and erasing if needed.

        NOTE: This is a simplified version that assumes the target area
        has already been erased. For full implementation, include erase
        and verification steps.
        """
        remaining = len(data)
        offset = 0
        while offset < len(data):
            page_offset = address % PAGE_SIZE
            chunk_size = min(PAGE_SIZE - page_offset, remaining)
            chunk = data[offset:offset + chunk_size]
            self.program_page(address, chunk)
            address += chunk_size
            offset += chunk_size
            remaining -= chunk_size

    def verify(self, address: int, expected: bytes) -> bool:
        """
        Read back data and compare with expected bytes.

        :return: True if match, otherwise raises FlashVerifyError.
        """
        actual = self.read(address, len(expected))
        if actual != expected:
            raise FlashVerifyError(
                f"Data mismatch at address 0x{address:06X}"
            )
        return True


# ----------------------------------------------------------------------
# Helper functions for output formatting
# ----------------------------------------------------------------------

def hex_dump(data: bytes, start_addr: int = 0) -> None:
    """Print a formatted hex dump."""
    for offset in range(0, len(data), 16):
        chunk = data[offset:offset + 16]
        hex_part = " ".join(f"{b:02X}" for b in chunk)
        ascii_part = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        log.info("%06X  %-47s  %s",
                 start_addr + offset, hex_part, ascii_part)


# ----------------------------------------------------------------------
# Example / test routine
# ----------------------------------------------------------------------

def main():
    """Demonstrate read, erase, program, and verify operations."""
    address = 0x000000
    test_data = bytes(range(0x20))  # 32-byte test pattern

    log.info("=" * 60)
    log.info("CH341A SPI Flash Programmer - Test")
    log.info("=" * 60)

    with CH341() as ch341:
        flash = SPIFlash(ch341)

        # 1. Read JEDEC ID
        jedec_id = flash.read_jedec_id()
        log.info("JEDEC ID: %s", " ".join(f"{b:02X}" for b in jedec_id))

        # 2. Read original data (first 32 bytes)
        log.info("Reading original data at 0x%06X...", address)
        original = flash.read(address, 32)
        hex_dump(original, address)

        # 3. Erase sector
        log.info("Erasing sector at 0x%06X...", address)
        flash.sector_erase(address)

        # 4. Program test pattern
        log.info("Programming test pattern...")
        flash.program_page(address, test_data)

        # 5. Read back and verify
        log.info("Reading back programmed data...")
        read_back = flash.read(address, len(test_data))
        hex_dump(read_back, address)

        if read_back == test_data:
            log.info("✓ Verification PASSED")
        else:
            log.error("✗ Verification FAILED")
            for i, (exp, act) in enumerate(zip(test_data, read_back)):
                if exp != act:
                    log.error("Mismatch at 0x%06X: expected %02X, got %02X",
                              address + i, exp, act)


if __name__ == "__main__":
    main()