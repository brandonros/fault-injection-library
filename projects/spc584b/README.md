# SPC584B JTAG password glitch

Submits a one-bit-wrong JTAG password, glitches VDD_LV as the TAP enters
`DRUPDATE`, then checks whether debug access was granted. It does not write
flash, UTEST, DCF, lifecycle, or OTP.

## Setup

- Pico `GND` to target `GND`
- Pico trigger to JTAG `TCK`
- Pico `GLITCH` to the isolated VDD_LV injection point
- Pico `RESET` and `VTARGET` disconnected
- Board independently powered; FTDI JTAG connected

Confirm the rail disturbance and safe pulse width on an oscilloscope.

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
8–28 ns pulse width, and a shuffled 1,506-point grid with seed 584.

Useful options: `--attempts N`, `--seed N`, and `--no-store`. SQLite results
otherwise go into the gitignored `projects/spc584b/databases/` directory.

## Outcomes

- `HALT_NOT_OBSERVED`: locked; normal negative result
- `ACCESS_*`: access candidate; stop immediately and preserve the halted target
- `PROBE_ERROR` or `TRIGGER_TIMEOUT`: invalid attempt; stop with an error

Exit codes: 0 negative sweep, 10 access candidate, 2 error, 130 interrupted.

A negative sweep covers only these digital timing settings; it does not rule
out other pulse shapes, rail conditions, or comparison timing.
