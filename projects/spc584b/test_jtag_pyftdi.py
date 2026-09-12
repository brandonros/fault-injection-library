import unittest

from jtag_pyftdi import (
    LCSTAT_JUN,
    NEXUS_RWCS_ERROR,
    SPC584BJtag,
)


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


if __name__ == "__main__":
    unittest.main()
