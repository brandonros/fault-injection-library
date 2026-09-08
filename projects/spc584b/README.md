# SPC584B JTAG password fault experiment

This experiment sends one uninterrupted 256-bit JTAG password transaction and
uses an earlier TCK edge as the Pico Glitcher trigger. It does not pause in
`DRPAUSE` and does not assume that entering `DRUPDATE` is the password-comparison
event.

The code never writes flash, UTEST, DCF, lifecycle, or OTP. It does not create a
database; every result is printed directly to stdout.

## Setup

- Pico `GND` to target `GND`
- Pico trigger input to JTAG `TCK`
- Pico `GLITCH` to the rail-side pad of the VDD_LV injection point
- Pico `RESET` and `VTARGET` disconnected
- Board independently powered; FTDI JTAG connected
- Oscilloscope probes on TCK and an MCU-side VDD_LV point

On the stock discovery board, C43 is on VDD_LV but is not isolated from Q1 or
the remaining rail capacitance. Do not interpret a campaign until the scope
shows a repeatable disturbance at the MCU-side measurement point.

## Install

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
export SPC_OPENOCD=/path/to/st-automotive-openocd/src/openocd
chmod 600 /secure/path/jtag-password.bin
```

## Stage 1: characterize a physical effect

Start with the correct password. The script first proves, without a glitch,
that the correct password unlocks and its one-bit-wrong variant does not. It
then stops at the first timing point where the correct-password result changes.

```bash
.venv/bin/python projects/spc584b/spc584b_password_glitch.py \
  --mode characterize \
  --rpico /dev/cu.usbmodemXXXX \
  --password-file /secure/path/jtag-password.bin \
  --edge-count 252 260 \
  --delay 0 1000 \
  --length 8 28
```

`FAULT_OBSERVED` proves only that the pulse changed target behavior. A reset,
TAP failure, and disruption of the password check are not equivalent. Correlate
the result with TCK and VDD_LV scope traces before narrowing the search.

The edge-count range is intentionally configurable. OpenOCD adds TAP transition
clocks around the 256 data clocks, and the Pico firmware's observed count must
be checked on the oscilloscope rather than inferred from the number 256.

## Stage 2: attack the wrong-password path

After Stage 1 establishes repeatable, non-destructive faulting near the end of
the password transaction, search a narrow neighborhood with the one-bit-wrong
password:

```bash
.venv/bin/python projects/spc584b/spc584b_password_glitch.py \
  --mode attack \
  --rpico /dev/cu.usbmodemXXXX \
  --password-file /secure/path/jtag-password.bin \
  --edge-count MIN_EDGE MAX_EDGE \
  --delay MIN_DELAY_NS MAX_DELAY_NS \
  --length MIN_LENGTH_NS MAX_LENGTH_NS \
  --attempts 1000 \
  --repeats 10
```

An `ACCESS_CANDIDATE` stops immediately and leaves the target halted without an
additional reset. Exit codes are 0 for no candidate, 10 for a fault/access
candidate, 2 for an experimental error, and 130 for interruption.

The defaults use the low-power crowbar, 1 MHz JTAG, a shuffled 4 ns timing
lattice, edges 252 through 260, delays from 0 through 1,000 ns, and pulse widths
from 8 through 28 ns. Use `--high-power` only after validating the electrical
effect and safe pulse width on an oscilloscope.
