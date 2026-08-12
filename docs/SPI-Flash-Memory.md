# SPI Flash Memory — CH341A, NeoProgrammer & Python

## Objective

Programmatically access, erase, program, read, and verify an SPI NOR flash device using a CH341A USB programmer and Python, while using NeoProgrammer and a Logic Analyzer as reference and measurement tools.

## 1. Hardware Setup

### Programmer

- CH341A USB programmer
- 3.3 V SPI operation
- Windows 64-bit host
- `CH341DLLA64.DLL`

### Flash Device

- Package marking: `WINBOND 25Q32JVSTQ2518`
- Nominal capacity: 32 Mbit = 4 MiB class
- SPI NOR flash

### SPI Wiring

| CH341A SPI signal | Flash signal |
|---|---|
| CS0 / D0 | CS# |
| D3 / SCLK | CLK |
| D5 / MOSI | DI / IO0 |
| D7 / MISO | DO / IO1 |
| 3.3 V | VCC |
| GND | GND |

A Logic Analyzer was connected to observe the SPI bus during erase/program/read operations.

## 2. NeoProgrammer Baseline

NeoProgrammer was first used as the baseline programmer.

The physical IC is marked `WINBOND 25Q32JVSTQ2518`, but automatic detection did not uniquely select W25Q32JV. The detected choices were:

- M45PE32
- XM25QH32B
- XM25QE32B

The device was manually selected as W25Q32JV. Programming and verification were successful.

This behavior is documented as an identification anomaly rather than assuming a definitive silicon manufacturer from the package marking alone.

## 3. Logic Analyzer Capture

The Logic 2 capture was used to observe the actual SPI transactions generated through the CH341A.

### Logic Analyzer Channels

| Logic 2 channel | Signal |
|---|---|
| D0 | SPI MOSI |
| D1 | SPI MISO |
| D4 | SPI Clock |

The original capture and CSV export are stored in the repository:

- [`spi erase write verify.sal`](../spi%20erase%20write%20verify.sal)
- [`LA waveform CSV export.csv`](../LA%20waveform%20CSV%20export.csv)

The screenshot used for the documentation should be treated as a visual overview; the `.sal` capture is the primary analyzer artifact.

## 4. Python CH341A Interface

Python accesses the vendor DLL using `ctypes`:

```text
Python
  ↓
ctypes
  ↓
CH341DLLA64.DLL
  ↓
CH341A
  ↓
SPI Flash
```

The implementation is split into two layers:

- `CH341`: low-level DLL loading, device open/close, and SPI transfer
- `SPIFlash`: flash commands such as identification, read, erase, program, status polling, and verification

Source: [`ch341_flash.py`](../ch341_flash.py)

## 5. CH341 SPI Transfer Constraint

`CH341StreamSPI4()` is limited to 32 bytes per transaction. The software therefore splits larger operations into smaller SPI transfers.

For a normal `0x03 READ` transaction:

```text
4-byte protocol header
  1 byte command
  3 byte address
+
28 bytes flash data
=
32 byte CH341 transaction
```

The same transport limit is respected during programming operations.

## 6. SPI Commands Implemented

| Command | Function | Status |
|---|---|---|
| `0x9F` | Read JEDEC ID | Tested |
| `0x03` | Read data | Tested |
| `0x05` | Read Status Register-1 | Tested |
| `0x06` | Write Enable | Tested |
| `0x20` | 4 KiB sector erase | Tested |
| `0x02` | Page Program | Tested |
| `0x5A` | Read SFDP | Tested |

## 7. Device Identification Results

### JEDEC ID (`0x9F`)

The Python implementation repeatedly returned:

```text
20 40 16
```

The result was stable across repeated reads.

### `0x90` Manufacturer / Device Identification

The test transaction returned:

```text
FF FF FF FE 20 15
```

The `20` manufacturer/device family information is therefore not consistent with the standard Winbond `EF 40 16` JEDEC identification expected for a conventional W25Q32JV.

### SFDP (`0x5A`)

The flash returned the valid SFDP signature:

```text
53 46 44 50
```

The SFDP header reported revision 1.0 and two parameter headers. The retrieved density information corresponds to a 32-Mbit / 4-MiB class device.

The device identity discrepancy remains documented as an unresolved identification anomaly. The successful NeoProgrammer W25Q32JV profile and the successful Python erase/program/read/verify sequence demonstrate protocol compatibility sufficient for the tested operations.

## 8. Python Read Test

The `0x03 READ` command was first tested against erased memory. Reading the first 256 bytes returned `0xFF` throughout, confirming the erased state of the tested region.

## 9. Python Erase / Program / Verify Test

A controlled 32-byte test was then performed at address `0x000000`.

### Initial data

The first 32 bytes were read before modification.

### Sector Erase

The sector containing address `0x000000` was erased using:

```text
0x20 + 24-bit address
```

### Test Pattern

The following 32-byte pattern was programmed:

```text
00 01 02 03 04 05 06 07
08 09 0A 0B 0C 0D 0E 0F
10 11 12 13 14 15 16 17
18 19 1A 1B 1C 1D 1E 1F
```

### Read Back

The programmed bytes were read using `0x03` and matched the transmitted test pattern exactly.

### Verification Result

```text
Verification PASSED
```

This establishes a complete working path:

```text
CH341A open
   ↓
JEDEC ID
   ↓
Read
   ↓
Write Enable
   ↓
Sector Erase
   ↓
Page Program
   ↓
Read Back
   ↓
Byte-for-byte Verification
```

## 10. Evidence

The repository contains the main executable source and Logic Analyzer artifacts used during the experiment:

- [`ch341_flash.py`](../ch341_flash.py) — Python CH341A SPI flash programmer
- [`spi erase write verify.sal`](../spi%20erase%20write%20verify.sal) — Logic 2 session
- [`LA waveform CSV export.csv`](../LA%20waveform%20CSV%20export.csv) — exported analyzer data

## 11. Current Software Scope

The current implementation is a working experimental programmer. It supports device access, status polling, reading, sector erase, page programming, and verification.

The high-level write path currently assumes the target region is already erased. A future production-oriented version should add:

- `.bin` file input/output
- full-chip backup
- automatic sector calculation
- automatic erase before programming
- progress reporting
- complete-image verification
- safer range and capacity checks

## 12. Outcome

The experiment progressed from using a GUI programmer to implementing the underlying SPI flash operations directly from Python through the CH341A DLL. The Logic Analyzer provided independent visibility into the SPI bus, while the successful erase/program/read/verify test demonstrated functional control of the flash device without NeoProgrammer.
