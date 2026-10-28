"""R11: crop-to-window unit gates — the ONE writer, the persistence
contract, and the mechanism's pins.  No display, no mpv: the real window
methods are bound onto a recording fake (the proven _AspectWin pattern),
the palette path is exercised through a real Gio action group.

Gates (brief §A/§B/§D):
  * the DEFAULTS leaf exists, false by default, schema-listed, with a
    settings switch row (parity lint demands exactly one);
  * PROPERTY_MAP maps it onto `panscan` with the numeric bool kind, and
    the pre-load push re-applies it (survives file changes);
  * mpv_init_options seeds panscan ONLY when persisted on (restart with
    no flicker) and keepaspect is pinned "yes" (cover must never mean
    stretch);
  * ONE writer: palette activation (parameterless, exactly as the command
    palette does) and the ctx-menu activation both land in _set_crop,
    which persists (disk, NOW) AND applies the live property AND mirrors
    BOTH action states AND OSFs on/off;
  * the settings row's live push re-enters the SAME writer.
"""
import _bootstrap  # noqa: F401  (sys.path for src/)
import json
import tempfile
import unittest
from pathlib import Path

import config
import aspect
import engine
import settings_ui
import vidsup
from gi.repository import Gio, GLib
from window import ReelseeWindow, make_crop_action
from _bootstrap import TMP_ROOT  # noqa: F401


def _cfg():
    d = Path(tempfile.mkdtemp(dir=str(TMP_ROOT)))
    return config.Config(d / "config.json")


class _Core:
    def __init__(self):
        self.props = []              # (name, value), in order
        self.snapshot = {"panscan": 0.0}

    def set_property(self, name, value):
        self.props.append((name, value))


class _Chrome:
    def __init__(self):
        self.osd = []

    def show_osd(self, text):
        self.osd.append(text)


class _CropWin:
    """Fake `self`: the REAL crop logic, bound unbound.  Aspect hooks are
    counted, not run (test_aspect owns that machinery's gates)."""
    _set_crop = ReelseeWindow._set_crop
    _act_crop = ReelseeWindow._act_crop
    _sync_crop_action = ReelseeWindow._sync_crop_action
    _crop_aspect_effect = ReelseeWindow._crop_aspect_effect
    _push_all_settings = ReelseeWindow._push_all_settings
    # R16-B: `_push_all_settings` routes the audio-only switch through the
    # ONE `vid` writer, which needs the suppression state machine.
    _apply_vid = ReelseeWindow._apply_vid
    _vid_write = ReelseeWindow._vid_write
    _aspect_locked = ReelseeWindow._aspect_locked
    _sync_aspect_action = ReelseeWindow._sync_aspect_action
    on_setting_changed = ReelseeWindow.on_setting_changed

    def __init__(self, cfg):
        self.cfg = cfg
        self.core = _Core()
        self.chrome = _Chrome()
        self._closing = False
        self._transient_muted = False
        self._fs = False
        self._aspect_lock_action = None
        self._aspect_hints_seen = None      # R14-B fallback memory (the
        # real _aspect_hints_pair reads it; tests never seed it)
        self._crop_on = bool(cfg.get("video.crop_to_window", False))
        self._vidsup = vidsup.VidSuppression(
            bool(cfg.get("playback.suppress_video_when_hidden", True)))
        self._crop_action = make_crop_action(self._crop_on)
        self._crop_action.connect("change-state", self._act_crop)
        self._ctx_crop = Gio.SimpleAction.new_stateful(
            "crop-to-window", None, GLib.Variant("b", self._crop_on))
        self._ctx_crop.connect(   # the window's real wiring, reproduced
            "change-state",
            lambda a, v: self._set_crop(
                a.get_state().get_boolean() if v is None
                else v.get_boolean()))
        self.realigns = 0
        self.forgets = 0
        self.hints = []
        self.hints_soon = 0

    def _aspect_realign(self):
        # faithful stand-in: the REAL _aspect_realign early-returns on
        # exactly this eligibility question, then corrects.
        if self._aspect_locked():
            self.realigns += 1

    def _aspect_forget(self):
        self.forgets += 1

    # R14-B seam, recorded like realign/forget above (live X behaviour is
    # scripts/smoke_aspecthints.py's job); the pair is the REAL pure math
    # so a crop flip asserts the same decision production would publish.
    _aspect_hints_pair = ReelseeWindow._aspect_hints_pair

    def _aspect_hints_apply(self):
        self.hints.append(self._aspect_hints_pair()
                          if self._aspect_locked() else None)

    def _aspect_hints_soon(self, *_a):
        self.hints_soon += 1

    def is_fullscreen(self):
        return False

    def is_maximized(self):
        return False


class TestAspectSuspension(unittest.TestCase):
    """R11-C: crop SUSPENDS the round-8 aspect lock — one pure truth,
    crop-on frees the box, crop-off re-arms and snaps back."""

    def test_pure_truth_table(self):
        # aspect_lock_active = setting AND NOT crop AND NOT fs AND NOT max
        for setting in (True, False):
            for crop in (True, False):
                for fs in (True, False):
                    for mx in (True, False):
                        self.assertIs(
                            aspect.aspect_lock_active(setting, crop,
                                                      fs, mx),
                            setting and not crop and not fs and not mx)
        # the brief's rows, spelled out for the reader
        self.assertIs(aspect.aspect_lock_active(True, False, False, False),
                      True)                       # plain: locked (R8)
        self.assertIs(aspect.aspect_lock_active(True, True, False, False),
                      False)                      # crop pauses the lock
        self.assertIs(aspect.aspect_lock_active(True, False, True, False),
                      False)                      # fullscreen: not ours
        self.assertIs(aspect.aspect_lock_active(False, True, False, False),
                      False)                      # setting off stays off

    def test_crop_on_suspends_and_off_rearms_the_eligibility(self):
        win = _CropWin(_cfg())                    # lock default True
        self.assertTrue(win._aspect_locked())
        win._set_crop(True)
        self.assertFalse(win._aspect_locked())    # freely resizable NOW
        win._set_crop(False)
        self.assertTrue(win._aspect_locked())     # re-armed

    def test_toggle_effects_forget_once_realign_once_hint_once(self):
        win = _CropWin(_cfg())                    # lock default True
        win._set_crop(True)
        self.assertEqual((win.forgets, win.realigns), (1, 0))
        # the ONE hint rides the toggle OSD — spelled out exactly once
        self.assertIn("Crop to window: on", win.chrome.osd[-1])
        self.assertIn("aspect lock paused", win.chrome.osd[-1])
        win._set_crop(False)
        self.assertEqual((win.forgets, win.realigns), (1, 1))
        # the off-line carries no suffix (nothing was paused visually)
        self.assertEqual(win.chrome.osd[-1], "Crop to window: off")

    def test_lock_off_means_no_suspend_bookkeeping(self):
        cfg = _cfg()
        cfg.set("ui.lock_aspect", False)
        win = _CropWin(cfg)
        win._set_crop(True)
        win._set_crop(False)
        self.assertEqual((win.forgets, win.realigns), (0, 0))
        self.assertFalse(any("paused" in t for t in win.chrome.osd))
        self.assertFalse(win._aspect_locked())    # setting off = never on

    def test_crop_flip_declares_and_retracts_the_aspect_hints(self):
        # R14-B: crop-ON must PULL the WM_NORMAL_HINTS (a WM still
        # enforcing PAspect would undo the crop on the next drag); crop-OFF
        # hands them back together with the lock.  The pair is the REAL
        # paspect_pair over the fake's observed dims.
        win = _CropWin(_cfg())                    # lock default True
        win.core.snapshot.update({"dwidth": 1920, "dheight": 1080})
        self.assertEqual(win._aspect_hints_pair(), (16, 9))
        win._set_crop(True)
        self.assertEqual(win.hints, [None])             # cleared, exactly 1
        win._set_crop(False)
        self.assertEqual(win.hints, [None, (16, 9)])    # published, exactly 1

    def test_setting_flip_while_crop_on_does_not_fight(self):
        # user toggles ui.lock_aspect (settings row) WHILE cropping: the
        # eligibility truth says suspended — no snap-back, no correction,
        # until the crop itself ends.
        win = _CropWin(_cfg())
        win._set_crop(True)
        # a settings flip persists the value (settings_ui's half) and then
        # calls on_setting_changed (the window's half); mirror both:
        win.cfg.set("ui.lock_aspect", False)      # OFF while crop is on
        win.on_setting_changed("ui.lock_aspect", False)
        self.assertEqual(win.realigns, 0)
        self.assertIs(win.cfg.get("ui.lock_aspect"), False)
        win.cfg.set("ui.lock_aspect", True)       # back ON, still cropping
        win.on_setting_changed("ui.lock_aspect", True)
        self.assertEqual(win.realigns, 0)
        # …and crop-off re-arms with the setting as the user left it
        win._set_crop(False)
        self.assertEqual(win.realigns, 1)


class TestConfigShape(unittest.TestCase):
    def test_default_false_and_schema_listed(self):
        self.assertIs(config.DEFAULTS["video"]["crop_to_window"], False)
        self.assertIn("video.crop_to_window", config.schema_leaf_keys())

    def test_settings_switch_row_exists(self):
        row = next(b for b in settings_ui.BINDINGS
                   if b["key"] == "video.crop_to_window")
        self.assertEqual(row["kind"], "switch")
        self.assertEqual(row["page"], "video")

    def test_persist_now_key(self):
        self.assertIn(("video", "crop_to_window"), config.PERSIST_NOW_KEYS)

    def test_property_map_rides_panscan(self):
        self.assertEqual(config.PROPERTY_MAP["video.crop_to_window"],
                         ("panscan", "boolnum"))


class TestEnginePins(unittest.TestCase):
    def _init_opts(self, cfg):
        return engine.mpv_init_options(cfg, True, 1,
                                       "/nonexistent-sys",
                                       "/nonexistent-dev")

    def test_keepaspect_pinned_yes(self):
        # cover, never stretch: panscan's zoom semantics depend on it
        self.assertEqual(engine.fixed_init_options()["keepaspect"], "yes")

    def test_panscan_seeded_only_when_on(self):
        off = _cfg()
        self.assertNotIn("panscan", self._init_opts(off))
        on = _cfg()
        on.set("video.crop_to_window", True)
        self.assertEqual(self._init_opts(on)["panscan"], "1")


class TestOneWriter(unittest.TestCase):
    def setUp(self):
        self.cfg = _cfg()
        self.win = _CropWin(self.cfg)

    def test_writer_persists_to_disk_now_and_applies_property(self):
        self.win._set_crop(True)
        self.assertIs(self.cfg.get("video.crop_to_window"), True)
        # PERSIST_NOW: on disk BEFORE any quit/flush, not debounced away
        disk = json.loads(Path(self.cfg.path).read_text())
        self.assertIs(disk["video"]["crop_to_window"], True)
        # live property applied NOW, numeric
        self.assertIn(("panscan", 1.0), self.win.core.props)
        self.win._set_crop(False)
        self.assertIn(("panscan", 0.0), self.win.core.props)
        disk = json.loads(Path(self.cfg.path).read_text())
        self.assertIs(disk["video"]["crop_to_window"], False)

    def test_writer_mirrors_both_surfaces_and_osds(self):
        self.win._set_crop(True)
        self.assertTrue(self.win._crop_action.get_state().get_boolean())
        self.assertTrue(self.win._ctx_crop.get_state().get_boolean())
        self.assertTrue(any(t.startswith("Crop to window: on")
                            for t in self.win.chrome.osd),
                        self.win.chrome.osd)
        self.win._set_crop(False)
        self.assertFalse(self.win._crop_action.get_state().get_boolean())
        self.assertFalse(self.win._ctx_crop.get_state().get_boolean())
        self.assertIn("Crop to window: off", self.win.chrome.osd)

    def test_palette_activation_round_trips(self):
        # exactly what the command palette does: parameterless activation
        # of a bool-state action (the trap make_aspect_lock_action pinned).
        grp = Gio.SimpleActionGroup()
        grp.add_action(self.win._crop_action)
        self.assertIsNone(self.win._crop_action.get_parameter_type())
        grp.activate_action("crop-to-window", None)
        self.assertTrue(self.win._crop_action.get_state().get_boolean())
        self.assertTrue(self.cfg.get("video.crop_to_window"))
        self.assertIn(("panscan", 1.0), self.win.core.props)
        grp.activate_action("crop-to-window", None)
        self.assertFalse(self.win._crop_action.get_state().get_boolean())
        self.assertFalse(self.cfg.get("video.crop_to_window"))

    def test_menu_activation_state_toggle_writes_through(self):
        # ctx menu path: Gtk flips the bool state and change-state hands
        # the transition to the SAME writer; the palette twin mirrors.
        grp = Gio.SimpleActionGroup()
        grp.add_action(self.win._ctx_crop)
        grp.activate_action("crop-to-window", None)
        self.assertTrue(self.win._ctx_crop.get_state().get_boolean())
        self.assertTrue(self.win._crop_action.get_state().get_boolean())
        self.assertIs(self.cfg.get("video.crop_to_window"), True)
        self.assertIn(("panscan", 1.0), self.win.core.props)
        grp.activate_action("crop-to-window", None)
        self.assertFalse(self.win._ctx_crop.get_state().get_boolean())
        self.assertFalse(self.win._crop_action.get_state().get_boolean())

    def test_settings_push_reenters_the_writer(self):
        self.win.on_setting_changed("video.crop_to_window", True)
        self.assertTrue(self.win._crop_action.get_state().get_boolean())
        self.assertTrue(self.win._ctx_crop.get_state().get_boolean())
        self.assertIs(self.cfg.get("video.crop_to_window"), True)
        self.assertIn(("panscan", 1.0), self.win.core.props)


class TestPreLoadPush(unittest.TestCase):
    def test_push_reapplies_crop(self):
        # the file-change leg of persistence: every replace-load runs the
        # pre-load push, and the crop key now rides it
        cfg = _cfg()
        cfg.set("video.crop_to_window", True)
        win = _CropWin(cfg)
        win._push_all_settings()
        self.assertIn(("panscan", 1.0), win.core.props)
        cfg.set("video.crop_to_window", False)
        win2 = _CropWin(cfg)
        win2._push_all_settings()
        self.assertIn(("panscan", 0.0), win2.core.props)


if __name__ == "__main__":
    unittest.main()
