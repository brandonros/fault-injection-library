"""Serial relay control for cold-cycling the SPC584B-DISP 12 V input."""

from __future__ import annotations

import time

import serial


class RelayError(RuntimeError):
    pass


class SerialPowerRelay:
    """Control the AT+CH1 relay already used by the MPC574X rig."""

    def __init__(
        self,
        port: str,
        *,
        baudrate: int = 9600,
        on_state: int = 0,
        off_state: int = 1,
        off_seconds: float = 1.5,
        settle_seconds: float = 4.0,
        serial_factory=serial.Serial,
    ):
        if on_state == off_state or {on_state, off_state} != {0, 1}:
            raise ValueError("relay on/off states must be different values from {0, 1}")
        self.port = port
        self.baudrate = baudrate
        self.on_state = on_state
        self.off_state = off_state
        self.off_seconds = off_seconds
        self.settle_seconds = settle_seconds
        self._serial_factory = serial_factory
        self.serial = None

    def open(self) -> "SerialPowerRelay":
        if self.serial is None:
            self.serial = self._serial_factory(
                self.port,
                self.baudrate,
                timeout=0.4,
            )
            time.sleep(0.2)
        return self

    def close(self, *, ensure_on: bool = True) -> None:
        if self.serial is None:
            return
        if ensure_on:
            try:
                self.turn_on()
            except Exception:
                pass
        self.serial.close()
        self.serial = None

    def _set_state(self, state: int) -> str:
        if self.serial is None:
            raise RelayError("power relay is not open")
        self.serial.reset_input_buffer()
        self.serial.write(f"AT+CH1={state}\r\n".encode())
        self.serial.flush()
        time.sleep(0.12)
        response = self.serial.read(64).decode(errors="replace").strip()
        return response

    def turn_off(self) -> str:
        return self._set_state(self.off_state)

    def turn_on(self) -> str:
        return self._set_state(self.on_state)

    def power_cycle(self) -> None:
        self.turn_off()
        time.sleep(self.off_seconds)
        self.turn_on()
        time.sleep(self.settle_seconds)

    def __enter__(self) -> "SerialPowerRelay":
        return self.open()

    def __exit__(self, *_: object) -> None:
        self.close()
