"""§5.5 CLI: _split_cli parsing + raw `--` option sanitization."""
import _bootstrap  # noqa: F401

import unittest

from reelsee import _mpv_opts_from_raw, _split_cli


class Split(unittest.TestCase):
    def test_targets_and_flags(self):
        t, n, raw, sim = _split_cli(["a.mp4", "-n", "b.mkv"])
        self.assertEqual(t, ["a.mp4", "b.mkv"])
        self.assertTrue(n)
        self.assertEqual(raw, [])
        self.assertFalse(sim)

    def test_dashdash_tail_is_raw(self):
        t, _, raw, sim = _split_cli(["--simulate-overload", "x.mp4",
                                     "--", "--start=30", "--no-video"])
        self.assertEqual(t, ["x.mp4"])
        self.assertTrue(sim)
        self.assertEqual(raw, ["--start=30", "--no-video"])

    def test_unknown_option_rejected_but_targets_survive(self):
        t, _, _, _ = _split_cli(["--bogus", "v.mp4"])
        self.assertEqual(t, ["v.mp4"])

    def test_dash_is_target(self):
        t, *_ = _split_cli(["-"])
        self.assertEqual(t, ["-"])


class RawOpts(unittest.TestCase):
    def test_pass_through_forms(self):
        self.assertEqual(
            _mpv_opts_from_raw(["--start=30", "--no-video", "--volume=80"]),
            ["start=30", "no-video", "volume=80"])

    def test_blocklist_dropped(self):
        out = _mpv_opts_from_raw(["--start=1", "--script=/tmp/evil.lua",
                                  "--load-script=x", "--input-conf=/dev/null",
                                  "--config=no", "--include=x.conf"])
        self.assertEqual(out, ["start=1"])

    def test_script_prefix_family(self):
        # script-opts etc. share the script* stem
        out = _mpv_opts_from_raw(["--script-opts=x=y", "--no-script=yes"])
        self.assertEqual(out, [])

    def test_pinned_and_fixed_init_keys_dropped(self):
        out = _mpv_opts_from_raw(["--vo=gpu", "--wid=123", "--gpu-api=vulkan",
                                  "--no-terminal", "--sub-pos=5"])
        self.assertEqual(out, ["sub-pos=5"])

    def test_no_equals_bare_arg(self):
        self.assertEqual(_mpv_opts_from_raw(["--no-video"]), ["no-video"])

    def test_garbage_ignored(self):
        self.assertEqual(_mpv_opts_from_raw(["start=30", "--"]), [])


if __name__ == "__main__":
    unittest.main()
