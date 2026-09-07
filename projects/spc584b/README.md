# SPC584B JTAG password glitch

Submits a one-bit-wrong JTAG password, glitches VDD_LV as the TAP enters
`DRUPDATE`, then checks whether debug access was granted. It does not write
flash, UTEST, DCF, lifecycle, or OTP.

## Setup

- Pico `GND` to target `GND`
- Pico trigger to JTAG `TCK`
- Pico `GLITCH` to the rail-side pad of the VDD_LV injection point
- Pico `RESET` and `VTARGET` disconnected
- Board independently powered; FTDI JTAG connected

On the stock discovery board, C43 is on VDD_LV but is not isolated from Q1 or
the rest of the rail capacitance. Confirm the disturbance at an MCU-side
VDD_LV point and the safe pulse width on an oscilloscope.

## Run

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
export SPC_OPENOCD=/path/to/st-automotive-openocd/src/openocd
chmod 600 /secure/path/jtag-password.bin

.venv/bin/python projects/spc584b/spc584b_password_glitch.py \
  --rpico /dev/cu.usbmodemXXXX \
  --password-file /secure/path/jtag-password.bin
```

Defaults: 1 MHz JTAG, 50 ms reset, low-power crowbar, 0–1,000 ns delay,
8–28 ns pulse width, and a shuffled 1,506-point grid with seed 584. Before and
after a negative sweep, 20 correct/wrong control pairs verify that the known
password unlocks and the one-bit-wrong password remains locked. Every password
submission uses a fresh OpenOCD process. Use `--controls 0` to explicitly
disable the controls.

For 100 trials at one setting:

```bash
.venv/bin/python projects/spc584b/spc584b_password_glitch.py \
  --rpico /dev/cu.usbmodemXXXX \
  --password-file /secure/path/jtag-password.bin \
  --delay 500 500 --length 20 20 \
  --repeats-per-point 100 --attempts 100
```

`--repeats-per-point N` creates distinct, seeded repeat entries. `--attempts N`
is the desired total, not an additional count. SQLite results and a compact
adjacent manifest go into the gitignored `projects/spc584b/databases/` directory.
Resume by rerunning the same command and configuration with
`--resume-database projects/spc584b/databases/RUN.sqlite --attempts N`. The
manifest and every existing row must match the expected shuffled prefix.

`--no-store` disables both files and cannot be combined with resume.
Use `--correct-password` to map disruptions of a known-good submission.

## Outcomes

- `HALT_NOT_OBSERVED`: locked; normal negative result
- `ACCESS_*`: access candidate; stop immediately and preserve the halted target
- `PROBE_ERROR` or `TRIGGER_TIMEOUT`: invalid attempt; stop with an error

Correct-password probes retry a 50 ms halt miss at 100, 200, 250, 300, 500,
1,000, and 2,000 ms without resetting. A recovered `JUN=1` is recorded as
delayed access, not a password-check disruption.

Exit codes: 0 negative sweep, 10 access candidate, 2 error, 130 interrupted.

A negative sweep covers only these digital timing settings; it does not rule
out other pulse shapes, rail conditions, or comparison timing.
