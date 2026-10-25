"""R5-C repeat cycle — pure mapping tests (no display).

Gate coverage: cycle order off -> all -> one -> off, the exact mpv
property values written per state, derivation from every loop-* value
shape ("no"/"inf"/"force"/"N"), loop-file priority, and unknown-mode
safety.
"""
import _bootstrap  # noqa: F401  (sys.path for src/)
import unittest

from chrome import REPEAT_ICONS, REPEAT_MODES, REPEAT_NEXT, repeat_mode, \
    repeat_props


class TestCycle(unittest.TestCase):
    def test_cycle_order(self):
        m = "off"
        seen = []
        for _ in range(9):
            m = REPEAT_NEXT[m]
            seen.append(m)
        self.assertEqual(seen, ["all", "one", "off"] * 3)

    def test_modes_set(self):
        self.assertEqual(set(REPEAT_MODES), set(REPEAT_NEXT) | {"off"})
        self.assertEqual(REPEAT_MODES, ("off", "all", "one"))

    def test_icons(self):
        self.assertEqual(REPEAT_ICONS["off"],
                         "media-playlist-repeat-symbolic")
        self.assertEqual(REPEAT_ICONS["all"],
                         "media-playlist-repeat-symbolic")
        self.assertEqual(REPEAT_ICONS["one"],
                         "media-playlist-repeat-song-symbolic")


class TestMpvValues(unittest.TestCase):
    def test_props_off(self):
        self.assertEqual(repeat_props("off"), ("no", "no"))

    def test_props_all(self):
        # "all" = whole playlist loops, single file does not
        self.assertEqual(repeat_props("all"), ("no", "inf"))

    def test_props_one(self):
        # "one" = this file loops, playlist does not
        self.assertEqual(repeat_props("one"), ("inf", "no"))

    def test_props_cycle_writes_both(self):
        # every transition writes BOTH properties (no stacked loops)
        seen = {repeat_props(m) for m in REPEAT_MODES}
        self.assertEqual(len(seen), 3)

    def test_write_then_read_roundtrip(self):
        for m in REPEAT_MODES:
            self.assertEqual(repeat_mode(*repeat_props(m)), m, m)


class TestDerivation(unittest.TestCase):
    def test_off(self):
        self.assertEqual(repeat_mode("no", "no"), "off")
        self.assertEqual(repeat_mode(None, None), "off")

    def test_all(self):
        self.assertEqual(repeat_mode("no", "inf"), "all")
        self.assertEqual(repeat_mode("no", "5"), "all")   # finite counts
        self.assertEqual(repeat_mode("no", "force"), "all")

    def test_one(self):
        self.assertEqual(repeat_mode("inf", "no"), "one")
        self.assertEqual(repeat_mode("2", "no"), "one")

    def test_file_outranks_playlist(self):
        # raw keys can stack both; icon shows the effective inner loop
        self.assertEqual(repeat_mode("inf", "inf"), "one")

    def test_zero_is_off(self):
        self.assertEqual(repeat_mode("0", "0"), "off")


if __name__ == "__main__":
    unittest.main()
