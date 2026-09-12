# SPC584B JTAG password fault experiment

This experiment sends one uninterrupted 256-bit JTAG password transaction.
The Pico PIO counts FTDI-generated TCK rising edges and emits the crowbar pulse
at one scope-calibrated edge. One external FTDI and OpenOCD session remains
alive for the campaign. Each shot applies the SPC584B destructive reset, proves
the exact TAP IDCODE, reinitializes the FTDI reset lines and scan chain, arms the
Pico, sends one uninterrupted password scan, and then verifies halt, live
registers, and three stable LCSTAT reads.

The code never writes flash, UTEST, DCF, lifecycle, or OTP. Every result is
printed to stdout.

## Setup

- Pico `GND` to target `GND`
- Pico trigger input to JTAG `TCK`
- Pico `GLITCH` SMA center to the VDD_LV pad of C43
- Pico `GLITCH` SMA shield/ground to the GND pad of C43
- Pico `RESET` and `VTARGET` disconnected
- Board independently powered; external FTDI JTAG connected
- Oscilloscope probes on TCK, Pico `GLITCH`, and an MCU-side VDD_LV point

On the stock discovery board, C43 is a non-polarized capacitor between VDD_LV
and GND; verify its pads by continuity to TP6 and GND with power removed. C43
is not isolated from Q1 or the remaining rail capacitance. Do not interpret a
campaign until the scope shows a repeatable disturbance at the MCU-side
measurement point.

The FTDI probe and Pico must remain powered independently of the target. The
SPC584B DCI destructive reset re-arms password security while allowing the
external FTDI/OpenOCD process to remain alive. The script then asks OpenOCD to
cycle its FTDI-controlled TRST/SRST lines and reinitialize the scan chain. This
second step is required after a locked-core halt attempt: raw IDCODE can remain
valid while OpenOCD can no longer recover correct-password core access. Before
every password submission, the script requires IDCODE `0x20144041`; it retries
the reset three times and aborts instead of firing if the TAP does not recover.

The per-shot order mirrors the proven MPC574X flow:

1. keep the external FTDI/OpenOCD controller alive;
2. destructively reset the target to re-arm the password check;
3. cycle the external FTDI reset lines and reinitialize the scan chain;
4. verify the target IDCODE, retrying reset on failure;
5. configure and arm the Pico edge trigger;
6. send one uninterrupted 256-bit wrong-password transaction;
7. require a real halt and register reads, then sample LCSTAT three times.

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
  --length 8 28 \
  --strict-oracle
```

`FAULT_OBSERVED` proves only that the pulse changed target behavior. A reset,
TAP failure, and disruption of the password check are not equivalent. Correlate
the result with TCK and VDD_LV traces, then repeat a narrow neighborhood to
establish that the effect is reproducible and non-destructive.

### No-scope alternative: correct-password sensitivity map

When scope calibration is intentionally skipped, map the correct-password
pass/fail boundary before spending repetitions on the wrong-password path.
This does not prove that VDD_LV moved, but it separates timing cells where the
correct password remains accepted from cells where the pulse changes target
behavior. Every completed attempt is flushed immediately to a CSV file.

At 1 MHz JTAG, edge 260 is the computed `Update-DR` point for this uninterrupted
256-bit scan. Run its complete 4 ns timing lattice with:

```bash
.venv/bin/python projects/spc584b/spc584b_password_glitch.py \
  --mode sensitivity-map \
  --rpico /dev/cu.usbmodem1301 \
  --password-file /Users/brandon/Desktop/mpc/spc584b-jtag-password.bin \
  --edge-count 260 \
  --delay 0 1000 \
  --length 8 28 \
  --repeats 1 \
  --strict-oracle \
  --output projects/spc584b/run-artifacts/edge260-sensitivity.csv
```

The final `SENSITIVITY_BOUNDARY` lines identify changed cells directly adjacent
to stable accepted cells. Repeat a narrower region around those cells before
using the same region in `attack` mode. The output path must not already exist;
the script refuses to overwrite an earlier run.

`wait_halt` failure alone cannot distinguish a locked core from a core that was
reset, crashed, or otherwise became unresponsive. Use `--strict-oracle` when
mapping a point without a scope. Before every shot, strict mode applies the DCI
destructive reset, verifies the exact IDCODE, proves that debug access is denied
without a password, then applies a second destructive reset before the password
scan. After every no-access result it resets again and requires the correct
password to produce live registers and three stable `LCSTAT` reads with `JUN=1`.
The run aborts instead of counting the shot if that recovery check fails.

```bash
.venv/bin/python projects/spc584b/spc584b_password_glitch.py \
  --mode sensitivity-map \
  --rpico /dev/cu.usbmodem1301 \
  --password-file /Users/brandon/Desktop/mpc/spc584b-jtag-password.bin \
  --edge-count 260 \
  --delay 0 0 \
  --length 8 8 \
  --repeats 100 \
  --halt-timeout-ms 2000 \
  --strict-oracle \
  --output projects/spc584b/run-artifacts/edge260-d0-l8-strict-repeat100.csv
```

A destructive reset is required between independent attempts because a
successful password scan sets temporary debug authorization. Reusing that state
would allow one accepted password to contaminate later results. The normal mode
already resets and verifies IDCODE before every shot; strict mode adds the paired
denial and recovery checks around it.

### Invalidated direct characterization, 2026-09-12

The SPC584B-DIS was tested with its onboard PLS FTDI at 1 MHz, PicoGlitcher and
findus 1.14.1 in low-power mode, TCK edge 260, delay 0 ns, and width 8 ns. Scope
calibration was intentionally skipped. This command was run twice:

```bash
.venv/bin/python projects/spc584b/spc584b_password_glitch.py \
  --mode characterize \
  --rpico /dev/cu.usbmodem1301 \
  --password-file /Users/brandon/Desktop/mpc/spc584b-jtag-password.bin \
  --edge-count 260 \
  --delay 0 0 \
  --length 8 8 \
  --attempts 1
```

Both runs appeared to pass the controls, and a subsequent fixed-point run
returned `LOCKED_OR_UNRESPONSIVE` for all 100 correct-password shots. Those
results are invalid as evidence of an authentication disturbance. A strict
recovery test showed that, after the first failed halt, DCI reset plus an IDCODE
check did not restore correct-password access in the same OpenOCD state. A fresh
OpenOCD process restored it immediately. IDCODE had therefore been proving only
that the TAP remained alive while the core-debug path stayed stale.

The harness now runs `jtag arp_init-reset` after DCI reset while keeping the
OpenOCD process and FTDI controller alive. With that fix, five strict
correct-password repetitions at edge 260, delay 0 ns, and width 8 ns all
returned `ACCESS_JUN_SET`. A strict wrong-password shot returned
`LOCKED_OR_UNRESPONSIVE`, followed by successful correct-password recovery with
live registers and three stable `LCSTAT=0xe0000002` reads. The existing evidence
therefore says that this 8 ns point does not block the correct password.

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
  --length MIN_LENGTH_NS MAX_LENGTH_NS \
  --strict-oracle
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
