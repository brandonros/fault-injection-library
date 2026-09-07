# SPC584B JTAG password glitch

This directory contains one experiment: submit a one-bit-wrong JTAG password
and voltage-glitch its Update-DR transition.

Each attempt:

1. Resets the target into its locked state.
2. Shifts the wrong password to `DRPAUSE`.
3. Arms the Pico Glitcher and enters `DRUPDATE`.
4. Tries to halt and read registers plus `PASS_LCSTAT`, without another reset.

The Pico trigger counts two rising TCK edges after `DRPAUSE`; the second enters
`DRUPDATE`. This is a timing reference, not proof that the physical password
comparison occurs on that exact edge.

The code does not write flash, UTEST, DCF, lifecycle, or OTP.

## Setup

- Connect Pico Glitcher `GND` to target `GND`.
- Connect its trigger input to JTAG `TCK`.
- Connect `GLITCH` to the isolated core rail under test.
- Leave Pico `RESET` and `VTARGET` disconnected.
- Keep the board independently powered and the FTDI JTAG adapter connected.
- Confirm the rail disturbance and pulse bounds on an oscilloscope.

From the repository root:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
export SPC_OPENOCD=/path/to/st-automotive-openocd/src/openocd
chmod 600 /secure/path/jtag-password.bin
```

## Run

```bash
.venv/bin/python projects/spc584b/spc584b_password_glitch.py \
  --rpico /dev/cu.usbmodemXXXX \
  --password-file /secure/path/jtag-password.bin
```

The defaults are the bench-tested campaign: 1 MHz JTAG, 50 ms reset hold,
low-power crowbar, delays from 0 through 1,000 ns, and pulse lengths from 8
through 28 ns. PicoGlitcher v3's 4 ns timing lattice produces 1,506 distinct
settings, shuffled with seed 584.

Use `--attempts N` for a shorter run, `--seed N` for another shuffle, or
`--no-store` to disable SQLite output. Results otherwise go into the gitignored
`projects/spc584b/databases/` directory.

## Results

- `HALT_NOT_OBSERVED`: normal locked response.
- `ACCESS_JUN_SET`: debug access confirmed with `JUN=1`.
- `ACCESS_JUN_CLEAR`: register access worked, but `JUN` was clear.
- `ACCESS_UNVERIFIED`: halt worked but follow-up proof failed.
- `PROBE_ERROR` or `TRIGGER_TIMEOUT`: invalid attempt; the campaign stops.

Any `ACCESS_*` result stops immediately and leaves the target halted without
another reset. Exit status is 0 for a completed negative sweep, 10 for an
access candidate, 2 for an error, and 130 for interruption.

A negative sweep proves only that no access was observed at these 1,506
digital settings in this run. It does not rule out other pulse shapes, rail
conditions, comparison timing, password-bit choices, or missed transients.
