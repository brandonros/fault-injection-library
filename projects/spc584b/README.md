# SPC584B JTAG password fault experiment

This experiment sends one uninterrupted 256-bit JTAG password transaction.
The Pico PIO counts FTDI-generated TCK rising edges and emits the crowbar pulse
at one scope-calibrated edge. One external FTDI and OpenOCD session remains
alive for the campaign. Each shot applies the SPC584B destructive reset, proves
the exact TAP IDCODE, arms the Pico, sends one uninterrupted password scan, and
then verifies halt, live registers, and three stable LCSTAT reads.

The code never writes flash, UTEST, DCF, lifecycle, or OTP. Every result is
printed to stdout.

## Setup

- Pico `GND` to target `GND`
- Pico trigger input to JTAG `TCK`
- Pico `GLITCH` to the rail-side pad of the VDD_LV injection point
- Pico `RESET` and `VTARGET` disconnected
- Board independently powered; external FTDI JTAG connected
- Oscilloscope probes on TCK, Pico `GLITCH`, and an MCU-side VDD_LV point

On the stock discovery board, C43 is on VDD_LV but is not isolated from Q1 or
the remaining rail capacitance. Do not interpret a campaign until the scope
shows a repeatable disturbance at the MCU-side measurement point.

The FTDI probe and Pico must remain powered independently of the target. The
SPC584B DCI destructive reset has the same password-security effect as a full
board power cycle, while allowing the external FTDI/OpenOCD process to remain
alive. Before every password submission, the script requires IDCODE
`0x20144041`; it retries the reset three times and aborts instead of firing if
the TAP does not recover.

The per-shot order mirrors the proven MPC574X flow:

1. keep the external FTDI/OpenOCD controller alive;
2. destructively reset the target to re-arm the password check;
3. verify the target IDCODE, retrying reset on failure;
4. configure and arm the Pico edge trigger;
5. send one uninterrupted 256-bit wrong-password transaction;
6. require a real halt and register reads, then sample LCSTAT three times.

## Install

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
export SPC_OPENOCD=/path/to/st-automotive-openocd/src/openocd
chmod 600 /secure/path/jtag-password.bin
```

## Stage 0: map TCK count to the waveform

Physically disconnect Pico `GLITCH` from the target rail. Keep it connected
only to an oscilloscope channel, then run:

```bash
.venv/bin/python projects/spc584b/spc584b_password_glitch.py \
  --mode edge-map \
  --rpico /dev/cu.usbmodemXXXX \
  --password-file /secure/path/jtag-password.bin \
  --edge-range 252 264 \
  --confirm-glitch-disconnected
```

The safety confirmation is mandatory because this mode intentionally emits a
marker for every requested edge. Use the TCK and marker traces to identify the
single count whose marker begins at, or immediately before, the final
`Update-DR` transition of the uninterrupted password scan. A
`marker_observed=no` line means that count was not reached.

The FTDI does not need to generate a separate trigger initially. The Pico PIO
counts the FTDI TCK waveform in hardware, so the trigger is synchronous with
the transaction. The scope measurement is what determines the exact count and
reveals any unacceptable jitter.

## Stage 1: characterize a physical effect

Reconnect Pico `GLITCH` to the injection point only after Stage 0. Supply the
one measured edge count, not an edge range. The script first proves without a
glitch that the correct password unlocks and its one-bit-wrong variant does
not. It then faults the correct password and stops at the first changed result.

```bash
.venv/bin/python projects/spc584b/spc584b_password_glitch.py \
  --mode characterize \
  --rpico /dev/cu.usbmodemXXXX \
  --password-file /secure/path/jtag-password.bin \
  --edge-count MEASURED_EDGE \
  --delay 0 1000 \
  --length 8 28
```

`FAULT_OBSERVED` proves only that the pulse changed target behavior. A reset,
TAP failure, and disruption of the password check are not equivalent. Correlate
the result with TCK and VDD_LV traces, then repeat a narrow neighborhood to
establish that the effect is reproducible and non-destructive.

## Stage 2: attack the wrong-password path

After Stage 1 establishes a repeatable physical effect near the comparison,
fault the one-bit-wrong password using the same calibrated edge:

```bash
.venv/bin/python projects/spc584b/spc584b_password_glitch.py \
  --mode attack \
  --rpico /dev/cu.usbmodemXXXX \
  --password-file /secure/path/jtag-password.bin \
  --edge-count MEASURED_EDGE \
  --delay MIN_DELAY_NS MAX_DELAY_NS \
  --length MIN_LENGTH_NS MAX_LENGTH_NS
```

Attack mode defaults to at most 100,000 shuffled attempts and 100 repetitions
per grid point. Override those with `--attempts` and `--repeats`. OpenOCD stays
alive across attempts; the script restarts it and reruns both controls only if
the Tcl session becomes unusable.

An `ACCESS_CANDIDATE` requires a successful halt and verified register reads.
Three LCSTAT reads distinguish a stable `JUN` result from an unstable or
invalid status read; every real-access classification stops immediately and
leaves the target halted without an additional reset. Exit codes are 0 for no
candidate, 10 for a fault/access candidate, 2 for an experimental error, and
130 for interruption.

The timing lattice defaults to 4 ns steps, delays from 0 through 1,000 ns, and
pulse widths from 8 through 28 ns at 1 MHz JTAG. Use `--high-power` only after
validating the electrical effect and safe pulse width on an oscilloscope.
