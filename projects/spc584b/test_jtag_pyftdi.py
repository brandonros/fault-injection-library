import unittest
from unittest.mock import patch

from jtag_pyftdi import (
    LCSTAT_JUN,
    NEXUS_RWCS_ERROR,
    SPC584BJtag,
)
from power_relay import SerialPowerRelay


class RawJtagTests(unittest.TestCase):
    def test_password_words_preserve_sequential_scan_order(self):
        words = tuple(0x10203040 + index for index in range(8))
        wire = SPC584BJtag.password_wire_value(words)
        shifted_bytes = wire.to_bytes(32, "little")
        expected = b"".join(word.to_bytes(4, "little") for word in words)
        self.assertEqual(shifted_bytes, expected)

    def probe(self, responses):
        jtag = SPC584BJtag()
        jtag.enter_nexus = lambda: setattr(jtag, "_in_once", True)
        iterator = iter(responses)
        jtag.nexus_read_word = lambda _address: next(iterator)
        return jtag.probe_access(samples=3)

    def test_probe_requires_stable_jun_set(self):
        probe = self.probe([(0xE0000002, 0)] * 3)
        self.assertEqual(probe.state, "ACCESS_JUN_SET")
        self.assertTrue(probe.access_observed)
        self.assertTrue(probe.lcstats[0] & LCSTAT_JUN)

    def test_probe_rejects_nexus_bus_error(self):
        probe = self.probe([(0xE0000002, NEXUS_RWCS_ERROR)] * 3)
        self.assertEqual(probe.state, "NO_VALID_LCSTAT")
        self.assertFalse(probe.access_observed)

    def test_invalid_all_ones_does_not_report_jun(self):
        probe = self.probe([(0xFFFFFFFF, 0xFFFFFFFF)] * 3)
        self.assertEqual(probe.state, "NO_VALID_LCSTAT")
        self.assertIn("valid=0;jun=0", probe.detail)

    def test_probe_rejects_unstable_data(self):
        probe = self.probe([(0xE0000002, 0), (0xE0000003, 0), (0xE0000002, 0)])
        self.assertEqual(probe.state, "ACCESS_LCSTAT_UNSTABLE")
        self.assertFalse(probe.access_observed)

    def test_probe_separates_transport_error_from_denial(self):
        jtag = SPC584BJtag()
        jtag.enter_nexus = lambda: (_ for _ in ()).throw(OSError("USB read failed"))
        probe = jtag.probe_access(samples=3)
        self.assertEqual(probe.state, "ORACLE_ERROR")
        self.assertIn("USB read failed", probe.detail)

    def test_close_releases_auxiliary_tap_before_ftdi(self):
        events = []

        class FakeEngine:
            def sync(self):
                events.append("sync")

            def close(self):
                events.append("close")

        jtag = SPC584BJtag()
        jtag.engine = FakeEngine()
        jtag._in_once = True
        jtag.release_once = lambda: events.append("release_once")
        jtag._set_reset_lines = lambda **_kwargs: events.append("deassert_reset")
        jtag.close()

        self.assertEqual(
            events,
            ["release_once", "deassert_reset", "sync", "close"],
        )
        self.assertIsNone(jtag.engine)

    def test_configured_power_cycle_replaces_dci_reset(self):
        events = []
        jtag = SPC584BJtag(power_cycle=lambda: events.append("power_cycle"))
        jtag._in_once = True
        jtag.release_once = lambda: events.append("release_once")
        jtag.reset_lines_and_tap = lambda hold_ms: events.append(
            f"reset_lines:{hold_ms}"
        )
        jtag.tap_reset = lambda: self.fail("DCI fallback must not run")

        jtag.destructive_reset()

        self.assertEqual(
            events,
            ["release_once", "power_cycle", "reset_lines:100"],
        )

    @patch("power_relay.time.sleep", return_value=None)
    def test_serial_relay_uses_mpc574x_at_protocol(self, _sleep):
        class FakeSerial:
            def __init__(self, *_args, **_kwargs):
                self.writes = []

            def reset_input_buffer(self):
                pass

            def write(self, data):
                self.writes.append(data)

            def flush(self):
                pass

            def read(self, _size):
                return b"OK\r\n"

            def close(self):
                pass

        fake = FakeSerial()
        relay = SerialPowerRelay(
            "/dev/fake",
            serial_factory=lambda *_args, **_kwargs: fake,
        ).open()
        relay.power_cycle()
        relay.close(ensure_on=False)

        self.assertEqual(fake.writes, [b"AT+CH1=1\r\n", b"AT+CH1=0\r\n"])


if __name__ == "__main__":
    unittest.main()
