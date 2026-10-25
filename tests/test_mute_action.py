"""R6-B/C gate: mute writes the PERSISTED binding and clears the
transient focus-mute.  No display, no mpv: a real Config over a scratch
dir + a stub core — the unit half of the §C mute contract (the two
processes restart proof lives in scripts/smoke_r6_mute_restart.py)."""
import _bootstrap  # noqa: F401  (sys.path for src/)

import json
import tempfile
import unittest

import config


class _Core:
    def __init__(self, mute=True):
        self.snapshot = {"mute": mute, "pause": False}
        self.props = []

    def set_property(self, name, value):
        self.props.append((name, value))
        self.snapshot[name] = value


class _Chrome:
    def __init__(self):
        self.osd = []

    def show_osd(self, text, *a, **k):
        self.osd.append(text)


class _Win:
    """bare object carrying exactly the attributes _act_mute touches —
    the REAL method is invoked unbound (no widget construction needed)."""

    def __init__(self, cfg, core):
        self.cfg = cfg
        self.core = core
        self.chrome = _Chrome()
        self._transient_muted = False


class TestMuteAction(unittest.TestCase):
    def setUp(self):
        from window import ReelseeWindow
        self.act = ReelseeWindow._act_mute
        self.dir = tempfile.TemporaryDirectory(dir=str(_bootstrap.TMP_ROOT))
        import pathlib
        self.path = pathlib.Path(self.dir.name) / "config.json"
        self.cfg = config.Config(self.path)

    def tearDown(self):
        self.dir.cleanup()

    def test_press_writes_mpv_and_config(self):
        core = _Core(mute=True)
        w = _Win(self.cfg, core)
        self.act(w)                       # muted -> unmute
        self.assertEqual(core.props, [("mute", False)])
        self.assertIs(self.cfg.get("audio.muted"), False)
        self.assertEqual(w.chrome.osd, ["unmuted"])
        self.act(w)                       # and back
        self.assertEqual(core.props[-1], ("mute", True))
        self.assertIs(self.cfg.get("audio.muted"), True)
        self.assertEqual(w.chrome.osd[-1], "muted")

    def test_press_clears_transient_focus_mute(self):
        core = _Core(mute=True)           # mpv TRUE via transient, cfg False
        self.cfg.set("audio.muted", False)
        w = _Win(self.cfg, core)
        w._transient_muted = True
        self.act(w)
        self.assertFalse(w._transient_muted)
        # toggles the PERSISTED binding (was False) -> True, never the
        # transient OR-ed flag:
        self.assertEqual(core.props[-1], ("mute", True))
        self.assertIs(self.cfg.get("audio.muted"), True)

    def test_persist_now_reaches_disk_immediately(self):
        core = _Core(mute=True)
        w = _Win(self.cfg, core)
        self.act(w)                       # muted -> unmute, PERSIST_NOW
        with open(self.path, encoding="utf-8") as fh:
            self.assertIs(json.load(fh)["audio"]["muted"], False)


if __name__ == "__main__":
    unittest.main()
