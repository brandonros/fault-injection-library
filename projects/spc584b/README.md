# SPC584B JTAG password fault experiment

This experiment uses one persistent PyFtdi object to control the onboard
SPC584B-DISP FTDI directly. It does not launch a debugger daemon. The Python
code owns every JTAG state transition and implements the complete path used by
the campaign:

1. assert the SPC584B DCI destructive reset to re-arm password security;
2. cycle the FTDI-controlled nTRST/nSRST lines and reset the main TAP;
3. require the exact raw IDCODE `0x20144041`;
4. select JTAGC instruction `0x07`;
5. shift one uninterrupted 256-bit password scan;
6. enter core-2 OnCE with JTAGC instruction `0x2a`;
7. select OnCE Nexus3 access with 10-bit command `0x07c`;
8. read `PASS_LCSTAT` at `0xf7ff4000` three times through Nexus;
9. report access only when all reads are stable, valid, free of a Nexus bus
   error, and LCSTAT bit 30 (`JUN`) is set.

No CPU halt request, process-state cache, socket, or timeout is used as an
authorization result. The raw implementation is in `jtag_pyftdi.py`. The
campaign is in `spc584b_password_glitch.py`.

The code never writes flash, UTEST, DCF, lifecycle, or OTP.

## Wiring

- Pico `GND` to target `GND`
- Pico `TRIGGER` input to JTAG `TCK`
- Pico `GLITCH` SMA center to the VDD_LV pad of C43
- Pico `GLITCH` SMA shield to the GND pad of C43
- Pico `RESET` and `VTARGET` disconnected
- Board independently powered from its wall adapter
- Board's onboard FTDI connected over USB

C43 is a capacitor, not a resistor. On the stock discovery board it is 2.2 uF
between VDD_LV and GND. Verify the VDD_LV pad by continuity to TP8 and verify
the other pad by continuity to ground with power removed. C35 through C48 put
about 4.8 uF of listed capacitance on that rail, and Q1 actively supplies it
while the board is powered. A long pulse may therefore be a broad brownout or
reset rather than a fault in the password comparison.

## Install

```bash
cd /Users/brandon/Desktop/mpc/fault-injection-library
python3 -m venv .venv
.venv/bin/pip install -e .
chmod 600 /Users/brandon/Desktop/mpc/spc584b-jtag-password.bin
```

The project pins PyFtdi, PyUSB, and a packaged libusb runtime. No system daemon
or external debugger executable is required.

## Mandatory raw-JTAG controls

Run this before another glitch attempt:

```bash
cd /Users/brandon/Desktop/mpc/fault-injection-library

.venv/bin/python projects/spc584b/spc584b_password_glitch.py \
  --mode controls \
  --password-file /Users/brandon/Desktop/mpc/spc584b-jtag-password.bin
```

This does not initialize or fire the Pico. It must show all of the following:

```text
RAW_IDCODE=0x20144041
CONTROL password=correct result=ACCESS_JUN_SET
CONTROL password=wrong result=<anything other than ACCESS_JUN_SET>
EXPERIMENT_RESULT=CONTROLS_PASS
```

The correct control proves the password word order, OnCE route, Nexus
transactions, target byte order, LCSTAT address, and JUN mask through the raw
Python implementation. The wrong control proves that a destructive reset
re-arms security and that one changed password bit does not set JUN. A campaign
must not run if either control fails.

The raw FTDI path has independently read the exact IDCODE on this board. Direct
LCSTAT control validation is the remaining hardware acceptance test after the
daemon removal.

## Characterize the connected crowbar

The previous 20 us test caused a repeatable changed response and recovered with
the correct password. Repeat that authority check with the raw oracle before
using its result:

```bash
.venv/bin/python projects/spc584b/spc584b_password_glitch.py \
  --mode sensitivity-map \
  --rpico /dev/cu.usbmodem1301 \
  --password-file /Users/brandon/Desktop/mpc/spc584b-jtag-password.bin \
  --edge-count 260 \
  --delay 0 0 \
  --length 20000 20000 \
  --repeats 5 \
  --strict-oracle \
  --output projects/spc584b/run-artifacts/raw-authority-lp-20us-repeat5.csv
```

For every shot, strict mode first proves that a fresh destructive reset has not
left JUN set. After every changed result it resets again and requires the
correct password to restore stable `LCSTAT.JUN=1`. This distinguishes a
recoverable electrical effect from persistent harness failure. It still does
not show whether the target brownouted or whether the password comparison was
disturbed.

## Sensitivity map

At 1 MHz JTAG, edge 260 is the computed `Update-DR` neighborhood for the raw
256-bit scan. Without a scope, first reduce pulse width until the correct
password has both passing and changed cells. Record every attempt in a new CSV.
Do not scan a 4 ns lattice through the full 20 us interval; narrow the width
range with successive authority tests.

```bash
.venv/bin/python projects/spc584b/spc584b_password_glitch.py \
  --mode sensitivity-map \
  --rpico /dev/cu.usbmodem1301 \
  --password-file /Users/brandon/Desktop/mpc/spc584b-jtag-password.bin \
  --edge-count 260 \
  --delay MIN_DELAY_NS MAX_DELAY_NS \
  --length MIN_LENGTH_NS MAX_LENGTH_NS \
  --step-ns 4 \
  --repeats 5 \
  --strict-oracle \
  --output projects/spc584b/run-artifacts/raw-edge260-sensitivity.csv
```

The `SENSITIVITY_BOUNDARY` lines identify changed cells directly adjacent to
cells where the correct password still sets JUN.

## Attack

Only after locating a repeatable correct-password boundary should the script
submit the one-bit-wrong password in the same timing region:

```bash
.venv/bin/python projects/spc584b/spc584b_password_glitch.py \
  --mode attack \
  --rpico /dev/cu.usbmodem1301 \
  --password-file /Users/brandon/Desktop/mpc/spc584b-jtag-password.bin \
  --edge-count 260 \
  --delay MIN_DELAY_NS MAX_DELAY_NS \
  --length MIN_LENGTH_NS MAX_LENGTH_NS \
  --strict-oracle
```

An `ACCESS_CANDIDATE` requires three stable direct reads with LCSTAT.JUN set.
The script stops without applying another reset so the authorization state can
be checked independently. Exit codes are 0 for no candidate, 10 for an access
candidate, 2 for an experimental error, and 130 for interruption.

## Edge mapping with a scope

With the glitch output physically disconnected from the target rail:

```bash
.venv/bin/python projects/spc584b/spc584b_password_glitch.py \
  --mode edge-map \
  --rpico /dev/cu.usbmodem1301 \
  --password-file /Users/brandon/Desktop/mpc/spc584b-jtag-password.bin \
  --edge-range 252 264 \
  --confirm-glitch-disconnected
```

Use TCK and the marker output to identify the final password-scan clocks. The
Pico PIO counts the externally generated TCK waveform in hardware.
