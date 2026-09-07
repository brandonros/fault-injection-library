# SPC584B method-2 JTAG password campaign

This project combines Findus/Pico Glitcher campaign control with a minimal
SPC584B OpenOCD helper. Each attempt submits the same one-bit-wrong password,
injects one voltage glitch during the 256-bit JTAG password scan, and then
checks whether debug access was unexpectedly granted.

The script does not write flash, UTEST, DCF, lifecycle, or other OTP. It does
perform a destructive reset before each attempt, which restarts the target.

## Connections

- Connect Pico Glitcher `GND` to target `GND`.
- Connect Pico Glitcher `TRIGGER` to JTAG `TCK`.
- Connect `GLITCH` to the appropriately isolated target core supply rail.
- Keep the onboard FTDI JTAG connection attached normally.
- Probe TCK and the core rail with an oscilloscope while establishing timing.
- For a board powered independently, do not pass `--enable-vtarget`.

Do not assume a particular TCK edge count is correct. The count includes JTAG
state-machine transitions around the DR scan; establish the useful count with
an oscilloscope before starting a long campaign.

## Installation

From the local fault-injection-library directory:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

Vanilla OpenOCD is unsupported. This target requires ST's
[`openocd-automotive-mcu-r1`](https://github.com/STMicroelectronics/OpenOCD/tree/openocd-automotive-mcu-r1)
branch with the `powerpc` target. `SPC_OPENOCD` is mandatory, and the campaign
checks for that target before accessing the PicoGlitcher or target hardware:

```bash
export SPC_OPENOCD=/path/to/st-automotive-openocd/src/openocd
```

The password file must contain exactly 32 raw bytes and should be readable only
by its owner (`chmod 600 PASSWORD_FILE`). It is read for constructing the
one-bit-wrong negative control and is never printed or stored in the database.

## Run

```bash
python projects/spc584b/spc584b-password-glitch.py \
  --rpico /dev/cu.usbmodemXXXX \
  --password-file /secure/path/jtag-password.bin \
  --edge-count EDGE \
  --delay MIN_DELAY_NS MAX_DELAY_NS \
  --length MIN_LENGTH_NS MAX_LENGTH_NS \
  --attempts 1000
```

Add `--optimize` after the basic trigger and classification loop are proven.
Use `--unique-grid-step-ns STEP` instead for a shuffled grid that visits each
aligned delay/length pair at most once. The step must be a multiple of the
PicoGlitcher's runtime PIO tick (4 ns on PicoGlitcher v3 at 250 MHz). With a
unique grid, zero attempts means the full grid; requesting more attempts than
available points is rejected.

The command defaults to a conservative 100 kHz adapter clock, 200 ms reset
settle time, and 250 ms debug-probe timeout. Override these with
`--adapter-speed-khz`, `--reset-delay-ms`, and `--halt-timeout-ms` only after
validating both the correct-password control and normal rejection path.

Use `--glitch-power high` only when the low-power crowbar cannot move the core
rail sufficiently and the electrical setup has been checked on an oscilloscope.

Results are stored under `projects/spc584b/databases/`. They can be viewed with:

```bash
analyzer --directory projects/spc584b/databases
```

The result classes are:

- `PASSWORD_REJECTED`: normal locked behavior.
- `JTAG_SCAN_ERROR`: the JTAG transaction was disturbed but access stayed denied.
- `TRIGGER_TIMEOUT`: the configured TCK trigger was not observed.
- `UNEXPECTED_DEBUG_ACCESS`: halt and PC read succeeded after the wrong password.

On `UNEXPECTED_DEBUG_ACCESS`, the campaign stops without issuing an additional
reset. A subsequent destructive reset or power cycle restores the normal locked
state.

## Final-edge sweep

After validating the electrical setup and safe pulse-width range on an
oscilloscope, sweep the final password-scan edges with:

```bash
./projects/spc584b/sweep-final-password-edges.sh \
  /dev/cu.usbmodemXXXX /secure/path/jtag-password.bin
```

The wrapper uses the hardware-validated fast settings: a 1 MHz adapter clock,
50 ms reset settling, and a 50 ms debug-probe timeout. For PicoGlitcher v3,
each edge visits all 1,506 combinations of delays from 0 through 1,000 ns and
pulse lengths from 8 through 28 ns on the firmware's 4 ns PIO lattice. The old
10 ns lower bound was already floored to the same PIO value as 8 ns; this grid
retains that setting and adds the previously skipped 16 ns state. It tests
edges 256 through 264 by default; override the bounds with `FIRST_EDGE` and
`LAST_EDGE`. `ATTEMPTS` may select a smaller random subset of the 1,506 points.

This exhausts only the selected digital PIO timing grid. It does not establish
that edges 256 through 264 cover the physical comparison window or exhaust the
analog effects of the crowbar circuit.

The wrapper exits with status 10 if unexpected access is observed, 0 after a
completed sweep with no access, and any underlying error status for a campaign
failure.
