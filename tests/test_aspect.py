"""R8-B unit tests: the pure aspect maths + the setting it hangs off.

`fit_aspect` is the whole feature's brain and it is deliberately GTK-free, so
this file proves it with no display, no mpv and no clock (§R8-B.5 asks for
wide, vertical, square, unknown aspect, odd sizes, rounding, minimum clamp,
work-area clamp).  The second class covers the two config facts the round
depends on: `ui.lock_aspect` defaults to true, and a config still carrying
the deleted `ui.show_top_menu` key loads inert; the third class drives the
TOGGLE itself — the real window methods running unbound on a bare stub (§B.6:
off means free resizing immediately, on adopts the current shape, and the
palette action really fires).
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import _bootstrap
import aspect
import config
from window import ReelseeWindow
from _bootstrap import TMP_ROOT  # noqa: F401  (writable scratch)


class FitAspectWideVerticalSquare(unittest.TestCase):
    def test_wide_box_holds_wide_video(self):
        # The §R8-B.5 worked example: a 1000x1000 drag with 16:9 video
        # loses height, never width.
        self.assertEqual(aspect.fit_aspect(1000, 1000, 16 / 9), (1000, 563))

    def test_wide_box_holds_vertical_video(self):
        # 1080x1920 in a 1000x400 drag: the WIDTH is cut, height is kept.
        self.assertEqual(aspect.fit_aspect(1000, 400, 1080 / 1920), (225, 400))

    def test_narrow_box_holds_vertical_video(self):
        # Same video, a box that is already too narrow: the HEIGHT goes.
        # (floor off here — this test is about the aspect rule alone)
        self.assertEqual(aspect.fit_aspect(200, 900, 1080 / 1920,
                                           min_w=0, min_h=0), (200, 356))
        # with the sane floor the 200 px box is raised to 320 first
        self.assertEqual(aspect.fit_aspect(200, 900, 1080 / 1920), (320, 569))

    def test_square(self):
        self.assertEqual(aspect.fit_aspect(1000, 1000, 1.0), (1000, 1000))
        self.assertEqual(aspect.fit_aspect(800, 400, 1.0), (400, 400))

    def test_exactly_faithful_box_is_left_alone(self):
        self.assertEqual(aspect.fit_aspect(1920, 1080, 16 / 9), (1920, 1080))
        self.assertEqual(aspect.fit_aspect(1080, 1920, 9 / 16), (1080, 1920))

    def test_fit_never_grows_the_box(self):
        # A drag always shrinks into itself: a small wide box with vertical
        # video gets smaller, it does not get taller.
        w, h = aspect.fit_aspect(640, 200, 9 / 16)
        self.assertLessEqual(w, 640)
        self.assertLessEqual(h, 200)


class FitAspectOddRoundingClamps(unittest.TestCase):
    def test_odd_sizes(self):
        self.assertEqual(aspect.fit_aspect(999, 501, 16 / 9), (891, 501))
        self.assertEqual(aspect.fit_aspect(641, 361, 16 / 9), (641, 361))
        # odd/odd square fit keeps the odd numbers symmetric (floor off)
        self.assertEqual(aspect.fit_aspect(333, 111, 1.0, min_w=0, min_h=0),
                         (111, 111))

    def test_half_rounds_up_not_to_even(self):
        # round(562.5) is 562 (banker's); the brief's example demands 563.
        self.assertEqual(aspect.fit_aspect(1000, 1000, 16 / 9)[1], 563)
        self.assertEqual(aspect._round(562.5), 563)
        self.assertEqual(aspect._round(562.49), 562)

    def test_result_is_always_int(self):
        for box in ((1000, 1000), (777, 333), (320, 180)):
            for a in (16 / 9, 9 / 16, 2.39, 1 / 2.39, 1.0):
                out = aspect.fit_aspect(*box, a)
                self.assertTrue(all(isinstance(v, int) for v in out), out)

    def test_minimum_clamp(self):
        # The FLOOR applies to the requested box (§R8-B.2: ">= 320x180 before
        # clamping to the aspect"), so a microscopic drag is raised to it.
        self.assertEqual(aspect.fit_aspect(100, 100, 16 / 9), (320, 180))
        self.assertEqual(aspect.fit_aspect(10, 10, 16 / 9), (320, 180))
        self.assertEqual(aspect.fit_aspect(320, 180, 16 / 9), (320, 180))
        # ... and the aspect then wins inside that box, which is why the
        # brief's own vertical example (225x400) is narrower than 320.
        self.assertEqual(aspect.fit_aspect(300, 400, 9 / 16), (225, 400))

    def test_workarea_clamp(self):
        # Never exceed the monitor's work area (1600x1000 here).
        self.assertEqual(aspect.fit_aspect(4000, 4000, 16 / 9,
                                           max_w=1600, max_h=1000),
                         (1600, 900))
        self.assertEqual(aspect.fit_aspect(4000, 4000, 9 / 16,
                                           max_w=1600, max_h=1000),
                         (563, 1000))
        w, h = aspect.fit_aspect(9999, 100, 16 / 9, max_w=1280, max_h=800)
        self.assertLessEqual(w, 1280)
        self.assertLessEqual(h, 800)

    def test_workarea_smaller_than_the_floor_still_wins(self):
        # "Never exceed the work area" is the hard rule: a 200x150 work area
        # must not be blown past by the 320x180 floor.
        w, h = aspect.fit_aspect(5000, 5000, 16 / 9, max_w=200, max_h=150)
        self.assertLessEqual(w, 200)
        self.assertLessEqual(h, 150)

    def test_never_below_one_pixel(self):
        # A microscopic box with the floor switched off still cannot come
        # back with a 0 (a 0x0 set_default_size is a silent no-op, which
        # would look like the lock having "eaten" the drag).
        self.assertEqual(aspect.fit_aspect(320, 1, 16 / 9, min_w=0, min_h=0),
                         (2, 1))
        self.assertEqual(aspect.fit_aspect(1, 320, 16 / 9, min_w=0, min_h=0),
                         (1, 1))
        self.assertEqual(aspect.fit_aspect(1000, 1, 0.4, min_w=0, min_h=0),
                         (1, 1))


class FitAspectUnknownAspect(unittest.TestCase):
    """Unknown/zero aspect leaves resizing COMPLETELY free: the box that
    comes back is the box that went in, at any value, unraised and
    uncapped."""

    def test_none_aspect_is_identity(self):
        for box in ((1000, 1000), (640, 200), (320, 180), (97, 51)):
            self.assertEqual(aspect.fit_aspect(*box, None), box)

    def test_zero_and_negative_aspect_is_identity(self):
        for a in (0, -1, -16 / 9, 0.0):
            self.assertEqual(aspect.fit_aspect(1234, 567, a), (1234, 567))

    def test_garbage_aspect_is_identity(self):
        for a in ("16:9", [], {}, (), True, False, object()):
            self.assertEqual(aspect.fit_aspect(800, 600, a), (800, 600))

    def test_degenerate_box_returns_degenerate_box(self):
        # No geometry yet (unrealized surface): pass it through, never
        # invent a size.
        self.assertEqual(aspect.fit_aspect(0, 0, 16 / 9), (0, 0))
        self.assertEqual(aspect.fit_aspect(1000, 0, 16 / 9), (1000, 0))
        self.assertEqual(aspect.fit_aspect(-5, 400, 16 / 9), (-5, 400))

    def test_numeric_strings_are_accepted(self):
        # A str can legitimately arrive from config/JSON-ish paths.
        self.assertEqual(aspect.fit_aspect(1000, 1000, "1.7777777777777777"),
                         (1000, 563))


class AspectOf(unittest.TestCase):
    """The aspect source is the observed display size and nothing else —
    dwidth/dheight (post-rotation), coded width/height only as the fallback
    §R8-B.1 allows, never the configured `video.aspect`."""

    def test_display_size_first(self):
        self.assertAlmostEqual(
            aspect.aspect_of({"dwidth": 1080, "dheight": 1920,
                              "width": 1920, "height": 1080}), 9 / 16)

    def test_falls_back_to_coded_size(self):
        self.assertAlmostEqual(
            aspect.aspect_of({"dwidth": 0, "dheight": 0,
                              "width": 1280, "height": 720}), 16 / 9)

    def test_partial_is_not_an_aspect(self):
        self.assertIsNone(aspect.aspect_of({"dwidth": 1920, "dheight": 0}))
        self.assertIsNone(aspect.aspect_of({"dwidth": 1920}))

    def test_idle_and_audio_only_have_no_aspect(self):
        for snap in ({}, {"dwidth": 0, "dheight": 0}, {"width": 0},
                     None, {"dwidth": None, "dheight": None}):
            self.assertIsNone(aspect.aspect_of(snap))

    def test_video_aspect_key_is_never_read(self):
        # The configured override is NOT an aspect source: the observed
        # display size is, and mpv applies the override to it.
        self.assertIsNone(aspect.aspect_of({"video.aspect": "16:9"}))

    def test_flags_are_not_dimensions(self):
        self.assertIsNone(aspect.aspect_of({"dwidth": True, "dheight": True}))

    def test_garbage_values(self):
        self.assertIsNone(aspect.aspect_of({"dwidth": "n/a",
                                            "dheight": "n/a"}))
        self.assertAlmostEqual(aspect.aspect_of({"dwidth": "640",
                                                 "dheight": "360"}), 16 / 9)


class NeedsCorrection(unittest.TestCase):
    def test_more_than_one_pixel(self):
        self.assertTrue(aspect.needs_correction(1000, 1000, 16 / 9))
        self.assertTrue(aspect.needs_correction(1000, 400, 9 / 16))

    def test_one_pixel_or_less_is_a_noop(self):
        # 1000x562 is 1 px off the 16:9 fit of a 1000 px wide window.
        self.assertFalse(aspect.needs_correction(1000, 562, 16 / 9))
        self.assertFalse(aspect.needs_correction(1000, 563, 16 / 9))
        self.assertFalse(aspect.needs_correction(960, 540, 16 / 9))

    def test_unknown_aspect_needs_nothing(self):
        self.assertFalse(aspect.needs_correction(1000, 1000, None))
        self.assertFalse(aspect.needs_correction(1000, 1000, 0))

    def test_our_own_correction_is_a_noop_when_replayed(self):
        # Feed the applied size back in: it must never look like a drag
        # (the re-entrancy story, mathematically).
        fit = aspect.fit_aspect(1000, 1000, 16 / 9)
        self.assertFalse(aspect.needs_correction(fit[0], fit[1], 16 / 9))
        fit = aspect.fit_aspect(1000, 400, 9 / 16)
        self.assertFalse(aspect.needs_correction(fit[0], fit[1], 9 / 16))


class MismatchAndWorkArea(unittest.TestCase):
    def test_mismatch_only_for_a_wrong_shape(self):
        self.assertFalse(aspect.mismatch(960, 540, 16 / 9))
        self.assertFalse(aspect.mismatch(1000, 563, 16 / 9))
        self.assertTrue(aspect.mismatch(1000, 1000, 16 / 9))
        self.assertTrue(aspect.mismatch(1000, 400, 9 / 16))

    def test_mismatch_is_false_without_an_aspect(self):
        self.assertFalse(aspect.mismatch(1000, 1000, None))
        self.assertFalse(aspect.mismatch(0, 0, 16 / 9))

    def test_fit_in_workarea(self):
        # 80 % of a 1600x1000 screen = 1280x800.
        self.assertEqual(aspect.fit_in_workarea(1600, 1000, 16 / 9),
                         (1280, 720))
        self.assertEqual(aspect.fit_in_workarea(1600, 1000, 1080 / 1920),
                         (450, 800))
        # a 4K 21:9 clip opens wide, and still inside the work area
        w, h = aspect.fit_in_workarea(1600, 1000, 2560 / 1080)
        self.assertEqual((w, h), (1280, 540))

    def test_fit_in_workarea_unknown_aspect(self):
        # Nothing to lock to => (0, 0), "no size to size to": a caller that
        # forgot to check first still cannot move the window by accident.
        self.assertEqual(aspect.fit_in_workarea(1600, 1000, None), (0, 0))
        self.assertEqual(aspect.fit_in_workarea(1600, 1000, 0), (0, 0))
        self.assertEqual(aspect.fit_in_workarea(0, 0, 16 / 9), (0, 0))


class SettingAndConfig(unittest.TestCase):
    def test_lock_aspect_defaults_true(self):
        self.assertTrue(config.DEFAULTS["ui"]["lock_aspect"])

    def test_lock_aspect_is_bound_in_settings(self):
        import settings_ui as S
        self.assertEqual(S.validate_bindings(), [])
        row = [b for b in S.BINDINGS if b["key"] == "ui.lock_aspect"]
        self.assertEqual(len(row), 1)
        self.assertEqual(row[0]["page"], "interface")
        self.assertEqual(row[0]["kind"], "switch")

    def test_setting_can_be_turned_off_and_persists(self):
        import tempfile
        from pathlib import Path
        d = Path(tempfile.mkdtemp(dir=str(_bootstrap.TMP_ROOT)))
        p = d / "config.json"
        p.write_text(json.dumps({"ui": {"lock_aspect": False}}))
        cfg = config.Config(p)
        self.assertIs(cfg.get("ui.lock_aspect"), False)
        cfg.set("ui.lock_aspect", True, immediate=True)
        self.assertIs(config.Config(p).get("ui.lock_aspect"), True)

    def test_dead_show_top_menu_key_loads_inert(self):
        # R8-A deleted that setting; R8-B must not resurrect it either.
        import tempfile
        from pathlib import Path
        import settings_ui as S
        for value in (True, False):
            d = Path(tempfile.mkdtemp(dir=str(_bootstrap.TMP_ROOT)))
            p = d / "config.json"
            p.write_text(json.dumps({"ui": {"show_top_menu": value}}))
            cfg = config.Config(p)                   # loads, no exception
            # deep_merge keeps unknown keys (they must survive a round trip
            # rather than be silently destroyed) — but NOTHING in the schema
            # or the settings model knows about it any more.
            self.assertIs(cfg.get("ui.lock_aspect"), True)
        self.assertNotIn("show_top_menu", config.DEFAULTS["ui"])
        self.assertNotIn("ui.show_top_menu", config.schema_leaf_keys())
        self.assertNotIn("ui.show_top_menu", S.binding_keys())
        # a ui section that never had the key is equally fine
        d = Path(tempfile.mkdtemp(dir=str(_bootstrap.TMP_ROOT)))
        p = d / "config.json"
        p.write_text(json.dumps({"ui": {"ontop": True}}))
        self.assertIs(config.Config(p).get("ui.ontop"), True)


# --------------------------------------------------------------------------
# §R8-B.6 — the SETTING TOGGLE.
#
# The rule to prove: off restores completely free resizing IMMEDIATELY (no
# stale guard, no pending correction left to fire later), on adopts the shape
# the user already has the window in, and both writers of the key — the
# command-palette action and the settings switch — end up agreeing.
#
# The real window methods run UNBOUND against a fake self carrying exactly
# the state they touch (`_toggle_decode`'s recipe from the R6 window tests):
# no widget, no display, no mpv.  `_work_area` is the single method replaced
# rather than reused, because the real one asks GDK for a monitor.
# --------------------------------------------------------------------------

WIDE_SNAP = {"dwidth": 1920, "dheight": 1080}      # 16:9, as observed


class _AspectWin:
    """Fake `self`: the REAL aspect logic, bound unbound."""
    _aspect_locked = ReelseeWindow._aspect_locked
    _aspect_frozen = ReelseeWindow._aspect_frozen
    _on_surface_layout = ReelseeWindow._on_surface_layout
    _aspect_correct = ReelseeWindow._aspect_correct
    _apply_aspect_size = ReelseeWindow._apply_aspect_size
    _end_aspect_guard = ReelseeWindow._end_aspect_guard
    _aspect_forget = ReelseeWindow._aspect_forget
    _aspect_realign = ReelseeWindow._aspect_realign
    _sync_aspect_action = ReelseeWindow._sync_aspect_action
    _act_aspect_lock = ReelseeWindow._act_aspect_lock
    on_setting_changed = ReelseeWindow.on_setting_changed

    def __init__(self, cfg, snapshot=None, size=(1000, 1000)):
        self.cfg = cfg
        self.core = SimpleNamespace(snapshot=dict(snapshot or {}))
        self.chrome = _Chrome()
        self._closing = False
        self._fs = False
        self._aspect_free_until = 0.0
        self._aspect_size = size
        self._aspect_surf = None
        self._aspect_file_id = None
        self._aspect_debounce_ms = aspect.DEBOUNCE_MS
        self._aspect_debounce_id = None
        self._aspect_guard_id = None
        self._aspect_pending = None
        self._aspect_applying = False
        self._aspect_applied = None
        self._aspect_corrections = 0
        self._aspect_lock_action = None
        self.requests = []

    # what a widget would answer, recorded instead of mapped
    def is_fullscreen(self):
        return False

    def is_maximized(self):
        return False

    def set_default_size(self, w, h):
        self.requests.append((w, h))

    def _work_area(self):                  # the only replaced method
        return (1600, 1000)


class _Chrome:
    def __init__(self):
        self.osd = []

    def show_osd(self, text, *a, **k):
        self.osd.append(text)


def _cfg():
    d = Path(tempfile.mkdtemp(dir=str(_bootstrap.TMP_ROOT)))
    return config.Config(d / "config.json")


class TestSettingToggle(unittest.TestCase):
    def tearDown(self):
        # no GLib source may outlive a test (the suite runs no main loop, but
        # an armed timer here is exactly the "stale source" the brief is
        # worried about)
        win = getattr(self, "win", None)
        if win is not None:
            ReelseeWindow._aspect_forget(win)

    def test_setting_defaults_true_with_a_switch_row(self):
        import settings_ui as S
        self.assertIs(config.DEFAULTS["ui"]["lock_aspect"], True)
        self.assertIn("ui.lock_aspect", config.schema_leaf_keys())
        row = next(b for b in S.BINDINGS if b["key"] == "ui.lock_aspect")
        self.assertEqual(row["kind"], "switch")
        self.assertEqual(row["page"], "interface")

    def test_palette_activation_really_reaches_the_handler(self):
        # The trap: with a "b" parameter type, the palette's parameterless
        # activation trips g_simple_action_activate and the toggle silently
        # never fires.  This is the unit half of that measured fact.
        from gi.repository import Gio
        from window import make_aspect_lock_action
        cfg = _cfg()
        self.win = _AspectWin(cfg, WIDE_SNAP)
        act = make_aspect_lock_action(True)
        self.assertIsNone(act.get_parameter_type())
        self.assertTrue(act.get_state().get_boolean())
        act.connect("change-state", self.win._act_aspect_lock)
        grp = Gio.SimpleActionGroup()
        grp.add_action(act)
        grp.activate_action("toggle-aspect-lock", None)   # as the palette does
        # (the return value is None on this PyGObject; the proof is the effect)
        self.assertIs(cfg.get("ui.lock_aspect"), False)
        self.assertFalse(act.get_state().get_boolean())
        self.assertEqual(self.win.chrome.osd, ["aspect lock off"])

    def test_off_drops_everything_in_flight_and_stays_free(self):
        from gi.repository import GLib
        from window import make_aspect_lock_action
        cfg = _cfg()
        self.win = win = _AspectWin(cfg, WIDE_SNAP)
        # a correction genuinely in flight: both timers armed, our own echo
        # guard up, a remembered request, a queued size
        win._aspect_debounce_id = GLib.timeout_add(5000, lambda: False)
        win._aspect_guard_id = GLib.timeout_add(5000, lambda: False)
        win._aspect_pending = (1234, 500)
        win._aspect_applying = True
        win._aspect_applied = (999, 999)
        act = make_aspect_lock_action(True)
        win._aspect_lock_action = act
        win._act_aspect_lock(act, GLib.Variant("b", False))
        self.assertIs(cfg.get("ui.lock_aspect"), False)
        self.assertIsNone(win._aspect_pending)      # nothing queued any more
        self.assertFalse(win._aspect_applying)      # no stale guard
        self.assertIsNone(win._aspect_applied)
        self.assertIsNone(win._aspect_debounce_id)  # both sources removed
        self.assertIsNone(win._aspect_guard_id)
        # and free NOW: a wildly off-aspect drag moves nothing, queues
        # nothing and leaves no source behind
        win._on_surface_layout(None, 1400, 500)
        self.assertIsNone(win._aspect_debounce_id)
        self.assertEqual(win.requests, [])

    def test_on_adopts_the_shape_the_user_is_in(self):
        from gi.repository import GLib
        from window import make_aspect_lock_action
        cfg = _cfg()
        cfg.set("ui.lock_aspect", False)
        self.win = win = _AspectWin(cfg, WIDE_SNAP, size=(1000, 1000))
        act = make_aspect_lock_action(False)
        win._aspect_lock_action = act
        win._act_aspect_lock(act, GLib.Variant("b", True))
        self.assertIs(cfg.get("ui.lock_aspect"), True)
        self.assertEqual(win.requests, [(1000, 563)])
        self.assertEqual(win._aspect_corrections, 1)
        self.assertTrue(act.get_state().get_boolean())

    def test_settings_row_direction_mirrors_the_action(self):
        # The settings switch never goes through the action: settings_ui
        # ._commit writes the key and then hands the hook the value through
        # apply_live.  Both halves are reproduced here (the hook alone does
        # NOT write config — measured in the smoke, so the unit test must not
        # pretend otherwise); what it proves is that the hook mirrors the
        # palette state and applies the same on/off semantics.
        from window import make_aspect_lock_action
        cfg = _cfg()
        self.win = win = _AspectWin(cfg, WIDE_SNAP, size=(1000, 1000))
        act = make_aspect_lock_action(True)
        win._aspect_lock_action = act
        cfg.set("ui.lock_aspect", False)
        win.on_setting_changed("ui.lock_aspect", False)
        self.assertFalse(act.get_state().get_boolean())
        win._on_surface_layout(None, 1400, 500)
        self.assertIsNone(win._aspect_debounce_id)
        self.assertEqual(win.requests, [])
        cfg.set("ui.lock_aspect", True)
        win.on_setting_changed("ui.lock_aspect", True)
        self.assertTrue(act.get_state().get_boolean())
        self.assertEqual(win.requests, [(889, 500)])    # 1400x500 -> 16:9

    def test_unknown_aspect_never_corrects(self):
        self.win = win = _AspectWin(_cfg(), {})         # audio-only / idle
        win._on_surface_layout(None, 1000, 1000)
        win._on_surface_layout(None, 700, 700)
        self.assertIsNone(win._aspect_debounce_id)
        self.assertEqual(win.requests, [])
        win._aspect_pending = (1000, 1000)              # a queued one dies
        self.assertIs(win._aspect_correct(), False)
        self.assertEqual(win.requests, [])

    def test_own_correction_echo_is_not_a_new_event(self):
        self.win = win = _AspectWin(_cfg(), WIDE_SNAP, size=(1000, 563))
        win._aspect_applying = True                     # correction in flight
        win._on_surface_layout(None, 1000, 900)
        self.assertIsNone(win._aspect_debounce_id)
        self.assertEqual(win.requests, [])

    def test_one_correction_per_burst(self):
        self.win = win = _AspectWin(_cfg(), WIDE_SNAP)
        for w, h in ((1000, 1000), (1100, 1100), (1200, 1200)):
            win._on_surface_layout(None, w, h)
        self.assertIsNotNone(win._aspect_debounce_id)   # armed ONCE
        self.assertIs(win._aspect_correct(), False)
        # exactly one request, computed from the NEWEST box of the burst
        self.assertEqual(win.requests, [(1200, 675)])


if __name__ == "__main__":
    unittest.main()
