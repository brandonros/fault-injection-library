#!/usr/bin/env python3
"""Minimal OpenOCD support for the SPC584B password-glitching example."""

from __future__ import annotations

from collections import deque
import hashlib
import os
from pathlib import Path
import re
import shutil
import socket
import stat
import subprocess
import threading
import time


EXPECTED_IDCODE = 0x20144041
PASS_LCSTAT = 0xF7FF4000
PASSWORD_SIZE = 32
RPC_TOKEN = b"\x1a"


class OpenOCDSession:
    """Run OpenOCD and exchange commands through its local Tcl RPC socket."""

    def __init__(self, executable: str, adapter_speed_khz: int = 100):
        self.executable = executable
        self.adapter_speed_khz = adapter_speed_khz
        self.port = _reserve_local_port()
        self.process: subprocess.Popen[str] | None = None
        self.sock: socket.socket | None = None
        self.output: deque[str] = deque(maxlen=2000)
        self.halted = False

    def _command_line(self) -> list[str]:
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

    def _collect_output(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        for line in self.process.stdout:
            self.output.append(line)

    def __enter__(self) -> "OpenOCDSession":
        self.process = subprocess.Popen(
            self._command_line(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        threading.Thread(target=self._collect_output, daemon=True).start()

        try:
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    raise OpenOCDError(
                        "OpenOCD exited during startup:\n" + "".join(self.output)
                    )
                try:
                    self.sock = socket.create_connection(
                        ("127.0.0.1", self.port), timeout=0.5
                    )
                    self.sock.settimeout(10.0)
                    break
                except OSError:
                    time.sleep(0.05)
            else:
                raise OpenOCDError("timed out waiting for the OpenOCD Tcl server")

            self.command("init")
            return self
        except Exception:
            self._close()
            raise

    def _rpc(self, script: str) -> str:
        if self.sock is None:
            raise OpenOCDError("OpenOCD Tcl connection is not available")
        self.sock.sendall(script.encode("utf-8") + RPC_TOKEN)
        chunks: list[bytes] = []
        while True:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise OpenOCDError("OpenOCD closed the Tcl connection")
            if RPC_TOKEN in chunk:
                before, _, _ = chunk.partition(RPC_TOKEN)
                chunks.append(before)
                return b"".join(chunks).decode("utf-8", errors="replace")
            chunks.append(chunk)

    def command(self, command: str, error_label: str | None = None) -> str:
        wrapped = (
            f"set __spc_rc [catch {{{command}}} __spc_msg]; "
            "list $__spc_rc $__spc_msg"
        )
        response = self._rpc(wrapped).strip()
        match = re.match(r"^(-?\d+)(?:\s+(.*))?$", response, re.DOTALL)
        if not match:
            raise OpenOCDError(f"unexpected OpenOCD Tcl response: {response!r}")
        message = (match.group(2) or "").strip()
        if len(message) >= 2 and message[0] == "{" and message[-1] == "}":
            message = message[1:-1]
        if match.group(1) != "0":
            raise OpenOCDError(
                f"OpenOCD command failed: {error_label or command}\n{message}"
            )
        return message

    def read_word(self, address: int) -> int:
        result = self.command(f"read_memory 0x{address:08x} 32 1")
        tokens = re.findall(r"(?:0x[0-9a-fA-F]+)|(?:\b[0-9]+\b)", result)
        if len(tokens) != 1:
            raise OpenOCDError(
                f"read at 0x{address:08x} returned {len(tokens)} words, expected 1"
            )
        return int(tokens[0], 0) & 0xFFFFFFFF

    def _close(self) -> None:
        if self.sock is not None:
            if self.halted:
                try:
                    self.command("resume")
                except Exception:
                    pass
            try:
                self._rpc("shutdown")
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
        self._close()


class OpenOCDError(RuntimeError):
    pass


def _reserve_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def find_openocd() -> str:
    configured = os.environ.get("SPC_OPENOCD")
    if not configured:
        raise OpenOCDError(
            "SPC_OPENOCD is required. Vanilla OpenOCD is unsupported; set "
            "SPC_OPENOCD to an executable built from STMicroelectronics' "
            "openocd-automotive-mcu-r1 branch."
        )
    executable = shutil.which(configured)
    if executable is None:
        raise OpenOCDError(
            f"ST automotive OpenOCD not found: {configured}"
        )

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

    output = f"{probe.stdout}\n{probe.stderr}"
    if probe.returncode != 0:
        raise OpenOCDError(
            f"OpenOCD capability check failed with exit status {probe.returncode}"
        )
    if "powerpc" not in output.split():
        raise OpenOCDError(
            "SPC_OPENOCD does not provide the required 'powerpc' target. "
            "Vanilla OpenOCD is unsupported; use STMicroelectronics' "
            "openocd-automotive-mcu-r1 branch."
        )
    return executable


def fingerprint(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_password_file(path: Path) -> bytes:
    try:
        data = path.read_bytes()
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError as error:
        raise OpenOCDError(f"cannot read password file {path}: {error}") from error
    if len(data) != PASSWORD_SIZE:
        raise OpenOCDError(f"password file must be exactly {PASSWORD_SIZE} bytes")
    if data in (b"\x00" * PASSWORD_SIZE, b"\xff" * PASSWORD_SIZE):
        raise OpenOCDError("refusing an all-zero or all-0xff password")
    if mode & 0o077:
        raise OpenOCDError(
            f"password file permissions are {mode:04o}; run: chmod 600 {path}"
        )
    return data


def destructive_reset(session: OpenOCDSession, settle_time_s: float = 0.2) -> None:
    """Reset through JTAGC while retaining TAP access for password submission."""
    session.command("irscan spc584b.tap 0x0e")
    session.command("drscan spc584b.tap 32 0x00000100")
    time.sleep(settle_time_s)
    session.command("irscan spc584b.tap 0x0e")
    session.command("drscan spc584b.tap 32 0x00000000")


def password_scan_value(words: tuple[int, ...]) -> int:
    """Build a 256-bit value; OpenOCD shifts its least-significant bit first."""
    return sum(word << (32 * index) for index, word in enumerate(words))
