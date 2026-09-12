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

Each process now asserts the board FTDI's nTRST/nSRST signals before its first
transaction and releases OnCE ownership before closing. This prevents an
auxiliary TAP selected by the preceding run from contaminating the next run.
This startup cleanup was added after an unglitched correct-password control
caught that condition and safely aborted before arming the Pico.

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
CONTROL password=correct_recovery result=ACCESS_JUN_SET
EXPERIMENT_RESULT=CONTROLS_PASS
```

The correct control proves the password word order, OnCE route, Nexus
transactions, target byte order, LCSTAT address, and JUN mask through the raw
Python implementation. The wrong control proves that a destructive reset
re-arms security and that one changed password bit does not set JUN. The final
correct-password control proves recovery from that denied state in the same
persistent PyFtdi session. A campaign must not run if any control fails.

The initial hardware acceptance run on 2026-09-12 returned raw IDCODE
`0x20144041`; three correct-password reads of `LCSTAT=0xe0000002` with
`RWCS=0x10c00005`, stable data, `JUN=1`, and no transport error; and three
wrong-password reads of zero with `JUN=0` and no transport error. This validates
the direct positive and negative oracle. The following correct-password
recovery in the same persistent session again returned three stable
`LCSTAT=0xe0000002` reads with `RWCS=0x10c00005`, `JUN=1`, and no transport
error. The complete raw harness acceptance sequence therefore passes.

## Characterize the connected crowbar

The raw 20 us authority run on 2026-09-12 changed all five correct-password
shots from valid LCSTAT data to stable all-ones LCSTAT/RWCS data. Every shot
then recovered to three stable `LCSTAT=0xe0000002` reads with `JUN=1`. This
passes the digital authority gate: the connected low-power crowbar reproducibly
affects the target/JTAG path and the effect is recoverable. It does not identify
the mechanism; at this width a broad brownout or reset remains the likely
explanation.

At the other endpoint, five raw shots at edge 260, delay 0 ns, and width 8 ns
all returned three stable `LCSTAT=0xe0000002` reads with `JUN=1`. The 8 ns pulse
therefore had no detectable effect. The measured transition lies somewhere
between 8 ns and 20 us at this edge and delay.

A subsequent coarse sweep tested widths from 1 us through 20 us in 1 us steps
with three repetitions per cell. The 1 us cell passed 3/3. Every cell from
2 us through 20 us produced stable all-ones LCSTAT/RWCS data 3/3 and recovered
with the correct password. The coarse transition is therefore between 1 us and
2 us; the uniform behavior above 2 us continues to look like rail collapse.

A temporal control then applied the same 2 us pulse 100 us after the final
password edge. All five shots still produced stable all-ones LCSTAT/RWCS data
and all five recovered. The 2 us failure is therefore independent of password
comparison timing and is excluded as a bypass signal. It establishes only a
generic target or JTAG-path collapse threshold.

The recorded command was:

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
