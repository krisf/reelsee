"""R10 unit tests: cascade placement math + the remembered-geometry store.

wingeom.cascade_place is the whole WHERE-decision of round 10 and it is
deliberately GTK-free (§B.5): right shift, left flip at the edge, clamp
with size preserved, y clamp, offset 0 == exactly on top, the invariant
that the result ALWAYS overlaps the source, and the two-monitor rule
that the SOURCE monitor's work area is the reference frame.  The store
side (§D): round-trip, corrupt/missing file ignored without a trace,
the fullscreen exclusion, and the debounced save that still flushes on
close — all driven through the real classes, the real window methods
running unbound on bare stubs exactly like tests/test_aspect.py does.
"""
from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

import _bootstrap  # noqa: F401  (sys.path + writable TMP_ROOT)
import config
import wingeom
from window import ReelseeWindow

WA = (0, 0, 1600, 1000)          # the single-Xvfb-monitor world
TWO = [(0, 0, 1600, 1000), (1600, 0, 1920, 1080)]


def overlaps(a, b):
    return wingeom.overlaps(a, b)


class CascadePlace(unittest.TestCase):
    def test_shifts_right_when_it_fits(self):
        self.assertEqual(
            wingeom.cascade_place((140, 90, 760, 430), WA, 50),
            (190, 90, 760, 430))

    def test_flips_left_at_the_right_edge(self):
        # 810 + 760 + 50 = 1620 > 1600, 810 - 50 = 760 >= 0 ⇒ flip
        self.assertEqual(
            wingeom.cascade_place((810, 90, 760, 430), WA, 50),
            (760, 90, 760, 430))
        # chained: the flipped window cascades again the same way
        self.assertEqual(
            wingeom.cascade_place((760, 90, 760, 430), WA, 50),
            (810, 90, 760, 430))

    def test_clamped_inside_work_area_size_preserved(self):
        # neither +200 nor -200 fits: clamp x into [0, 100] at the SAME size
        out = wingeom.cascade_place((50, 0, 900, 200), (0, 0, 1000, 1000), 200)
        self.assertEqual(out[2:], (900, 200))          # never resized
        self.assertEqual(out, (100, 0, 900, 200))
        self.assertTrue(overlaps((50, 0, 900, 200), out))

    def test_y_clamped_into_work_area(self):
        # a source partly above the screen: y is pulled in, x shifts right
        out = wingeom.cascade_place((100, -60, 400, 300), WA, 50)
        self.assertEqual(out, (150, 0, 400, 300))
        # work area not starting at the origin (bottom clamp)
        out = wingeom.cascade_place((100, 950, 400, 300), WA, 50)
        self.assertEqual(out[2:], (400, 300))
        self.assertLessEqual(out[1] + out[3], 1000)

    def test_offset_zero_is_exactly_on_top(self):
        for src in ((140, 90, 760, 430), (810, 90, 760, 430),
                    (-40, 20, 300, 200)):
            self.assertEqual(wingeom.cascade_place(src, WA, 0),
                             wingeom.norm_rect(src))

    def test_result_always_overlaps_source(self):
        # the §B.2 invariant over an aggressive sweep, absurd offsets and
        # a tiny window included (the w-1 cap is what makes this total)
        sources = [(0, 0, 960, 540), (600, 400, 760, 430),
                   (1550, 900, 400, 90), (0, 0, 40, 40),
                   (100, 950, 500, 40)]
        for off in (0, 1, 50, 199, 200, 500, 5000):
            for src in sources:
                for wa in (WA, *TWO):
                    out = wingeom.cascade_place(src, wa, off)
                    self.assertIsNotNone(out, (src, wa, off))
                    self.assertTrue(overlaps(src, out),
                                    f"src={src} wa={wa} off={off} -> {out}")
                    self.assertEqual(out[2:], src[2:],   # size NEVER changes
                                     f"src={src} wa={wa} off={off} -> {out}")

    def test_two_monitors_use_the_source_monitor(self):
        # source sits on monitor 1: the +50 lands there...
        self.assertEqual(
            wingeom.cascade_place((1650, 100, 800, 500), TWO, 50),
            (1700, 100, 800, 500))
        # ...and the flip at monitor 1's RIGHT edge (x=3520) stays on
        # monitor 1 — monitor 0's work area must NOT be the reference
        out = wingeom.cascade_place((3100, 0, 400, 300), TWO, 50)
        self.assertEqual(out, (3050, 0, 400, 300))
        # a boundary window belongs to the monitor holding more of it;
        # a perfect tie resolves to the lower index (deterministic)
        self.assertEqual(wingeom.pick_monitor((1500, 0, 200, 100), TWO), 0)
        self.assertEqual(wingeom.pick_monitor((1610, 0, 200, 100), TWO), 1)

    def test_garbage_in_no_crash_out(self):
        for bad in (None, "nope", (1, 2), {"x": 1}, object()):
            self.assertIsNone(wingeom.cascade_place(bad, WA, 50))
        self.assertIsNone(wingeom.cascade_place((0, 0, 100, 100), None, 50))
        self.assertIsNone(wingeom.cascade_place((0, 0, 100, 100), (), 50))


class RestoreClamp(unittest.TestCase):
    def test_position_clamped_size_untouched(self):
        # remembered from a bigger monitor onto a smaller one
        out = wingeom.clamp_into_workarea((3400, 50, 760, 430), WA)
        self.assertEqual(out, (840, 50, 760, 430))

    def test_never_oversized(self):
        out = wingeom.clamp_into_workarea((0, 0, 5000, 4000), WA)
        self.assertEqual(out, (0, 0, 1600, 1000))

    def test_negative_garbage_rect(self):
        self.assertIsNone(wingeom.clamp_into_workarea(None, WA))
        self.assertIsNone(wingeom.clamp_into_workarea((0, 0, 100, 100), None))
        self.assertIsNone(wingeom.clamp_into_workarea((0, 0, -5, 100), WA))

    def test_valid_rect_gate(self):
        self.assertEqual(wingeom.valid_rect({"x": 1, "y": 2, "w": 300,
                                             "h": 200}), (1, 2, 300, 200))
        self.assertEqual(wingeom.valid_rect(
            SimpleNamespace(x=5, y=6, width=300, height=200)),
            (5, 6, 300, 200))
        for bad in ({"x": 0, "y": 0, "w": 4, "h": 300},        # too small
                    {"x": 0, "y": 0, "w": True, "h": 300},     # bool≠number
                    {"x": 0, "y": 0, "w": 99999, "h": 300},    # absurd
                    {"x": "x", "y": 0, "w": 300, "h": 200},    # nonsense
                    {"w": 300, "h": 200}):                      # missing keys
            self.assertIsNone(wingeom.valid_rect(bad), bad)


class GeometryStore(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="r10-store-",
                                         dir=str(_bootstrap.TMP_ROOT)))
        self.path = self.dir / "window.json"

    def test_roundtrip(self):
        s = config.GeometryStore(self.path)
        self.assertIsNone(s.last())
        s.remember((140, 90, 760, 430), 1)
        s.flush()
        again = config.GeometryStore(self.path)
        self.assertEqual(again.last(), (140, 90, 760, 430))
        self.assertEqual(again.last_monitor(), 1)
        doc = json.loads(self.path.read_text())
        self.assertEqual(doc["rect"], {"x": 140, "y": 90, "w": 760, "h": 430})

    def test_missing_and_corrupt_are_silent(self):
        self.assertIsNone(config.GeometryStore(self.path).last())
        self.path.write_text("this is not json {{{")
        self.assertIsNone(config.GeometryStore(self.path).last())
        self.path.write_text(json.dumps({"rect": {"x": 1}}))
        self.assertIsNone(config.GeometryStore(self.path).last())
        self.path.write_text(json.dumps([1, 2, 3]))
        self.assertIsNone(config.GeometryStore(self.path).last())

    def test_fullscreen_rect_is_excluded(self):
        s = config.GeometryStore(self.path)
        s.remember((140, 90, 760, 430), 0)
        s.flush()
        # "closing while fullscreen": windowed=False must not touch anything
        self.assertFalse(s.remember((0, 0, 1600, 1000), 0, windowed=False))
        s.flush()
        self.assertEqual(config.GeometryStore(self.path).last(),
                         (140, 90, 760, 430))
        # and an invalid rect never gets remembered either
        self.assertFalse(s.remember((0, 0, 5, 5)))
        self.assertEqual(s.last(), (140, 90, 760, 430))

    def test_debounced_save_still_flushes_on_close(self):
        # a debounce that will clearly NOT have fired inside the test
        s = config.GeometryStore(self.path, debounce_s=30)
        s.remember((10, 20, 800, 600), 0)
        time.sleep(0.25)                      # past the PRODUCTION 250 ms
        self.assertFalse(self.path.exists(),
                         "debounce must not have written yet")
        s.flush()                             # "saved on quit" anyway
        self.assertEqual(config.GeometryStore(self.path).last(),
                         (10, 20, 800, 600))

    def test_flush_without_changes_writes_nothing(self):
        s = config.GeometryStore(self.path)
        s.flush()
        self.assertFalse(self.path.exists())


class CaptureStub:
    """The REAL window methods, unbound on a bare object (test_aspect's
    pattern): capture_geometry only ever needs these, no display."""
    _geom_windowed = ReelseeWindow._geom_windowed
    _geom_live_rect = ReelseeWindow._geom_live_rect
    _geom_enabled = ReelseeWindow._geom_enabled
    _geom_monitor_index = ReelseeWindow._geom_monitor_index
    capture_geometry = ReelseeWindow.capture_geometry

    def __init__(self, store, rect, fs=False):
        self.app = SimpleNamespace(geometry_store=store)
        self.cfg = SimpleNamespace(
            get=lambda key, d=None: {
                "ui.remember_geometry": True,
                "ui.cascade_offset_px": 50}.get(key, d))
        self._geom_rect = rect
        self._fs = fs
        self._closing = False

    def is_fullscreen(self):
        return self._fs

    def is_maximized(self):
        return False

    def get_width(self):
        return 0          # ≤ 1 ⇒ size-only fallback declines, rect stands

    def get_height(self):
        return 0

    def get_display(self):
        raise AssertionError("no display in a unit test")


class WindowCapture(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="r10-cap-",
                                         dir=str(_bootstrap.TMP_ROOT)))
        self.path = self.dir / "window.json"

    def test_close_capture_flushes(self):
        store = config.GeometryStore(self.path, debounce_s=30)
        stub = CaptureStub(store, (140, 90, 760, 430))
        stub.capture_geometry()               # the close/quit promise
        self.assertEqual(config.GeometryStore(self.path).last(),
                         (140, 90, 760, 430))

    def test_fullscreen_capture_leaves_memory_intact(self):
        first = config.GeometryStore(self.path)
        first.remember((140, 90, 760, 430), 0)
        first.flush()
        stub = CaptureStub(config.GeometryStore(self.path, debounce_s=30),
                           (0, 0, 1600, 1000), fs=True)
        stub.capture_geometry()               # quit WHILE fullscreen
        again = config.GeometryStore(self.path)
        self.assertEqual(again.last(), (140, 90, 760, 430))

    def test_adopted_flag_skips_the_first_aspect_fit(self):
        # R10-B.1: a cascade/restore window's FIRST file must not refit —
        # the real _aspect_on_file consumed-one-shot logic on a stub.
        stub = SimpleNamespace(
            _closing=False,
            core=SimpleNamespace(snapshot={"dwidth": 1080, "dheight": 1920}),
            _aspect_file_id=None, _aspect_applied=None,
            _geom_adopted=True)
        stub._geom_consume = lambda: ReelseeWindow._geom_consume(stub)
        self.assertFalse(ReelseeWindow._aspect_on_file(stub))
        self.assertIsNone(stub._aspect_file_id)     # polling stopped
        self.assertFalse(stub._geom_adopted)        # consumed exactly once
        # the NEXT file is normal again (no suppression left): with no
        # lock/size state below it takes the round-8 path, which needs a
        # real window — so we only assert the flag itself is gone here.

    def test_plan_persistence_settings_keys(self):
        # the two R10 keys exist in DEFAULTS with the specified defaults
        self.assertTrue(config.DEFAULTS["ui"]["remember_geometry"])
        self.assertEqual(config.DEFAULTS["ui"]["cascade_offset_px"], 50)
