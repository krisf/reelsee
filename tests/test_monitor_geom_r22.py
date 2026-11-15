"""ROUND 22 gates: monitor geometry in PHYSICAL pixels
(references/ROUND22-BRIEF.md §1/§2).

The measured cause (§0, operator, live GNOME Wayland + XWayland, scale=2):
the app is an XWayland client, RandR reports ONE screen of 5120x2880
PHYSICAL, GTK reports ONE monitor of 2560x1440 with ``scale_factor=2`` —
LOGICAL units — while every rect the app saves and moves is raw-Xlib
PHYSICAL (``x11pos``).  So the placement code compared physical rects
against logical screens: a window closed at x=2700 (the user's RIGHT
display) failed the corner rule and was clamped to ``2560 - width`` =
1660, the seam between the two displays.  The user's words: "still goes
back to center".

The fix is three helpers in ``src/window.py`` that multiply each Gdk
monitor's geometry/workarea fields by ``get_scale_factor()`` (clamped to
>= 1), so the placement maths runs entirely in the raw-Xlib physical
space.  ``wingeom.py`` stays pure — it never learns about scale.

Harness (the tests/test_wingeom.py / test_wingeom_r20.py pattern): the
REAL window methods, unbound on a bare stub with a fake display, so a
scale=2 desktop is testable with no HiDPI hardware, no display and no
sleep.  B4 is the regression guard for the scale=1 world every Xvfb gate
lives in: with scale 1 the helpers must return byte-identical output to
today's code, which is ``wingeom.work_areas`` over the raw monitors.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import _bootstrap  # noqa: F401  (sys.path + writable TMP_ROOT)
import config
import wingeom
from window import ReelseeWindow

# ---------------------------------------------------------------------------
# The operator's live desktop, transcribed from the brief's measurements.
# GTK reports LOGICAL units; x11pos moves windows in RandR PHYSICAL units.
# A 40 px logical top bar ⇒ work area (0, 40, 2560, 1400).
# ---------------------------------------------------------------------------
LOGICAL_GEO = (0, 0, 2560, 1440)
LOGICAL_WA = (0, 40, 2560, 1400)
PHYS_GEO = (0, 0, 5120, 2880)
PHYS_WA = (0, 80, 5120, 2800)

STORED = (2700, 200, 900, 700)        # the measured rect: the RIGHT display
OFFSCREEN = (6000, 200, 900, 700)     # genuinely off the 5120 px screen


class FakeMonitor:
    """A Gdk.Monitor stand-in that reports LOGICAL extents plus a scale
    factor — exactly what GTK hands an XWayland client under HiDPI.

    ``scale=None`` omits ``get_scale_factor`` entirely (a monitor object
    from before the helper asked for it); the helpers must then behave as
    scale 1 rather than dropping the monitor."""

    def __init__(self, geo, wa=None, scale=2):
        self._geo = geo
        self._wa = wa if wa is not None else geo
        self._scale = scale

    def get_geometry(self):
        return SimpleNamespace(x=self._geo[0], y=self._geo[1],
                               width=self._geo[2], height=self._geo[3])

    def get_workarea(self):
        return SimpleNamespace(x=self._wa[0], y=self._wa[1],
                               width=self._wa[2], height=self._wa[3])

    def get_scale_factor(self):
        if self._scale is None:
            raise AttributeError("get_scale_factor")
        return self._scale


class GeomStub:
    """The REAL R10/R20 placement helpers (and plan_open_geometry) on a
    bare object with a fake display."""
    _geom_enabled = ReelseeWindow._geom_enabled
    _geom_offset = ReelseeWindow._geom_offset
    _geom_monitor_areas = ReelseeWindow._geom_monitor_areas
    _geom_monitor_geos = ReelseeWindow._geom_monitor_geos
    _geom_monitor_index = ReelseeWindow._geom_monitor_index
    plan_open_geometry = ReelseeWindow.plan_open_geometry

    def __init__(self, store, monitors, remember=True):
        self.app = SimpleNamespace(geometry_store=store)
        self.cfg = SimpleNamespace(
            get=lambda k, d=None: {"ui.remember_geometry": remember}.get(k, d))
        self._monitors = monitors

    def get_display(self):
        return SimpleNamespace(get_monitors=lambda: list(self._monitors))


class BrokenDisplayStub(GeomStub):
    """No display at all (Wayland, a destroyed Gdk.Display): the helpers
    must degrade to "no monitors", never raise."""

    def get_display(self):
        raise RuntimeError("no display")


def hi_dpi_stub(store=None, monitors=None, **kw):
    mons = monitors if monitors is not None else [
        FakeMonitor(LOGICAL_GEO, LOGICAL_WA, scale=2)]
    return GeomStub(store if store is not None
                    else config.GeometryStore(_tmp_path()), mons, **kw)


def _tmp_path():
    d = Path(tempfile.mkdtemp(prefix="r22-geom-", dir=str(_bootstrap.TMP_ROOT)))
    return d / "window.json"


class B1HelpersReturnPhysical(unittest.TestCase):
    """B1: with a scale=2 stub the helpers return PHYSICAL rects — the
    space x11pos moves in.  Red on current code, which returns GTK's
    LOGICAL 2560x1440."""

    def setUp(self):
        self.stub = hi_dpi_stub()

    def test_geos_are_physical(self):
        self.assertEqual(self.stub._geom_monitor_geos(), [PHYS_GEO])

    def test_workareas_are_physical(self):
        # the PANEL is included in the physical math: the workarea fields
        # are multiplied, the offsets are not re-derived
        self.assertEqual(self.stub._geom_monitor_areas(), [PHYS_WA])

    def test_two_physical_monitors_side_by_side(self):
        mons = [FakeMonitor((0, 0, 2560, 1440), (0, 40, 2560, 1400), scale=2),
                FakeMonitor((2560, 0, 2560, 1440), (2560, 40, 2560, 1400),
                            scale=2)]
        stub = hi_dpi_stub(monitors=mons)
        self.assertEqual(stub._geom_monitor_geos(),
                         [(0, 0, 5120, 2880), (5120, 0, 5120, 2880)])
        self.assertEqual(stub._geom_monitor_areas(),
                         [(0, 80, 5120, 2800), (5120, 80, 5120, 2800)])

    def test_scale_below_one_is_clamped_to_one(self):
        # a driver reporting 0 (or a negative) must never shrink the screen
        for bad in (0, -2):
            stub = hi_dpi_stub(monitors=[FakeMonitor(LOGICAL_GEO, LOGICAL_WA,
                                                     scale=bad)])
            self.assertEqual(stub._geom_monitor_geos(), [LOGICAL_GEO], bad)
            self.assertEqual(stub._geom_monitor_areas(), [LOGICAL_WA], bad)

    def test_monitor_without_scale_factor_behaves_as_scale_one(self):
        # today's callers (and the R20 FakeMonitor) have no get_scale_factor
        stub = hi_dpi_stub(monitors=[FakeMonitor(LOGICAL_GEO, LOGICAL_WA,
                                                 scale=None)])
        self.assertEqual(stub._geom_monitor_geos(), [LOGICAL_GEO])
        self.assertEqual(stub._geom_monitor_areas(), [LOGICAL_WA])

    def test_degradation_without_a_display_is_unchanged(self):
        # the `except Exception` contract stays exactly as it was: no
        # monitors, no raise, no traceback
        stub = BrokenDisplayStub(config.GeometryStore(_tmp_path()), [])
        self.assertEqual(stub._geom_monitor_geos(), [])
        self.assertEqual(stub._geom_monitor_areas(), [])
        self.assertIsNone(stub._geom_monitor_index(STORED))


class B2RestoreVerbatimOnHiDPI(unittest.TestCase):
    """B2: the user's case.  Stored (2700, 200, 900, 700) on the 5120 px
    physical screen ⇒ 0 corners off the SCREEN ⇒ verbatim restore.  Red on
    current code, which measures against the 2560 px LOGICAL screen and
    returns the measured clamp (1660, 200, 900, 700)."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="r22-plan-",
                                         dir=str(_bootstrap.TMP_ROOT)))
        self.path = self.dir / "window.json"

    def plan(self, rect, monitor=0, monitors=None):
        store = config.GeometryStore(self.path)
        if rect is not None:
            store.remember(rect, monitor)
            store.flush()
        return hi_dpi_stub(store=config.GeometryStore(self.path),
                           monitors=monitors).plan_open_geometry(None)

    def test_right_display_rect_restores_verbatim(self):
        # controls first: the rect really is off the LOGICAL screen — that
        # is the measured bug — and fully on the PHYSICAL one
        self.assertEqual(wingeom.corners_offscreen(STORED, LOGICAL_GEO), 4,
                         "control: the whole rect is off the LOGICAL screen")
        self.assertEqual(wingeom.clamp_into_workarea(STORED, LOGICAL_WA),
                         (1660, 200, 900, 700),
                         "control: the measured clamp to the seam")
        self.assertEqual(wingeom.corners_offscreen(STORED, PHYS_GEO), 0,
                         "control: no corner off the PHYSICAL screen")
        self.assertEqual(self.plan(STORED), ("restore", STORED))

    def test_a_window_straddling_the_physical_seam_is_verbatim(self):
        mons = [FakeMonitor((0, 0, 2560, 1440), (0, 40, 2560, 1400), scale=2),
                FakeMonitor((2560, 0, 2560, 1440), (2560, 40, 2560, 1400),
                            scale=2)]
        self.assertEqual(self.plan((4900, 300, 600, 400), monitors=mons),
                         ("restore", (4900, 300, 600, 400)))

    def test_cascade_branch_uses_the_physical_work_area(self):
        # the same physical areas feed the R20 centerward cascade: a source
        # on the right of the 5120 px screen steps LEFT toward its centre
        stub = hi_dpi_stub()
        src = SimpleNamespace(rect_for_cascade=lambda: (4000, 300, 900, 700))
        self.assertEqual(stub.plan_open_geometry(src),
                         ("cascade", (3985, 315, 900, 700)))


class B3GenuinelyOffscreenStillClamps(unittest.TestCase):
    """B3: the corner rule is unchanged — it is now measured against the
    PHYSICAL extents, so a rect that really is off the 5120 px screen is
    still clamped back into the physical work area."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="r22-off-",
                                         dir=str(_bootstrap.TMP_ROOT)))
        self.path = self.dir / "window.json"

    def plan(self, rect, monitor=0):
        store = config.GeometryStore(self.path)
        store.remember(rect, monitor)
        store.flush()
        return hi_dpi_stub(store=config.GeometryStore(self.path)
                           ).plan_open_geometry(None)

    def test_offscreen_rect_is_clamped_into_the_physical_work_area(self):
        self.assertEqual(wingeom.corners_offscreen(OFFSCREEN, PHYS_GEO), 4,
                         "control: genuinely off the PHYSICAL screen entirely")
        out = self.plan(OFFSCREEN)
        self.assertEqual(out[0], "restore")
        self.assertNotEqual(out[1], OFFSCREEN, "must not restore off-screen")
        self.assertEqual(out[1], wingeom.clamp_into_workarea(OFFSCREEN, PHYS_WA))
        self.assertEqual(out[1], (4220, 200, 900, 700))
        _x, _y, w, h = out[1]
        self.assertGreaterEqual(out[1][0], PHYS_WA[0])
        self.assertGreaterEqual(out[1][1], PHYS_WA[1])
        self.assertLessEqual(out[1][0] + w, PHYS_WA[0] + PHYS_WA[2])
        self.assertLessEqual(out[1][1] + h, PHYS_WA[1] + PHYS_WA[3])

    def test_two_corners_off_the_physical_screen_are_clamped_too(self):
        # the >=2 boundary, measured against the PHYSICAL extents: a rect
        # hanging off the right edge of the 5120 px screen (2 corners off)
        # is repositioned exactly as R20 does on a scale-1 desktop
        rect = (4900, 200, 900, 700)
        self.assertEqual(wingeom.corners_offscreen(rect, PHYS_GEO), 2)
        self.assertEqual(self.plan(rect), ("restore", (4220, 200, 900, 700)))

    def test_rect_beyond_the_physical_bottom_right_is_clamped(self):
        out = self.plan((5000, 2600, 900, 700))
        self.assertEqual(out, ("restore", (4220, 2180, 900, 700)))


class B4ScaleOneIsByteIdentical(unittest.TestCase):
    """B4: the Xvfb / single-monitor regression guard.  With scale=1 the
    helpers must return EXACTLY what today's code returns — the raw
    ``wingeom.work_areas`` of the monitors — so no existing gate moves."""

    XVFB_GEO = (0, 0, 1600, 1000)
    XVFB_WA = (0, 0, 1600, 940)

    def setUp(self):
        self.mons = [FakeMonitor(self.XVFB_GEO, self.XVFB_WA, scale=1)]
        self.stub = GeomStub(config.GeometryStore(_tmp_path()), self.mons)

    def test_helpers_match_today_formula(self):
        ref_areas = wingeom.work_areas(list(self.mons))
        ref_geos = wingeom.work_areas([m.get_geometry() for m in self.mons])
        self.assertEqual(self.stub._geom_monitor_areas(), ref_areas)
        self.assertEqual(self.stub._geom_monitor_geos(), ref_geos)
        for rect in ((140, 90, 760, 430), (1500, 100, 760, 430),
                     (3400, 50, 760, 430), (2700, 200, 900, 700)):
            self.assertEqual(self.stub._geom_monitor_index(rect),
                             wingeom.pick_monitor(rect, ref_geos), rect)

    def test_helpers_match_literal_xvfb_values(self):
        self.assertEqual(self.stub._geom_monitor_geos(), [self.XVFB_GEO])
        self.assertEqual(self.stub._geom_monitor_areas(), [self.XVFB_WA])
        self.assertEqual(self.stub._geom_monitor_index((140, 90, 760, 430)), 0)

    def test_scale_factor_absent_is_byte_identical_too(self):
        mons = [FakeMonitor(self.XVFB_GEO, self.XVFB_WA, scale=None)]
        stub = GeomStub(config.GeometryStore(_tmp_path()), mons)
        self.assertEqual(stub._geom_monitor_geos(), [self.XVFB_GEO])
        self.assertEqual(stub._geom_monitor_areas(), [self.XVFB_WA])

    def test_r20_panel_decisions_are_unchanged(self):
        # the R20 §0.1 corner-rule outcomes on the scale=1 world, byte for
        # byte: panel-hung ⇒ verbatim, >=2 corners off the screen ⇒ clamped
        d = Path(tempfile.mkdtemp(prefix="r22-s1-", dir=str(_bootstrap.TMP_ROOT)))
        path = d / "window.json"

        def plan(rect):
            store = config.GeometryStore(path)
            store.remember(rect, 0)
            store.flush()
            return GeomStub(config.GeometryStore(path), self.mons
                            ).plan_open_geometry(None)

        self.assertEqual(plan((140, 545, 760, 430)),
                         ("restore", (140, 545, 760, 430)))
        self.assertEqual(plan((1500, 100, 760, 430)),
                         ("restore", (840, 100, 760, 430)))
        self.assertEqual(plan((3400, 50, 760, 430)),
                         ("restore", (840, 50, 760, 430)))


class B5MonitorIndexIsPhysical(unittest.TestCase):
    """B5: the monitor index stored with a rect must refer to the PHYSICAL
    monitor that actually holds it — a right-side rect used to be mapped
    against the LOGICAL list, where it overlaps nothing at all."""

    def setUp(self):
        self.stub = hi_dpi_stub()

    def test_right_side_rect_maps_to_the_physical_monitor(self):
        idx = self.stub._geom_monitor_index(STORED)
        self.assertIsNotNone(idx, "a right-display rect must map to a monitor")
        geos = self.stub._geom_monitor_geos()
        self.assertEqual(len(geos), 1)
        self.assertEqual(idx, 0)
        self.assertTrue(wingeom.overlaps(STORED, geos[idx]),
                        "the monitor it picked must CONTAIN the rect")
        # control: against GTK's LOGICAL list the same rect overlaps nothing
        logical = wingeom.work_areas([m.get_geometry()
                                      for m in self.stub._monitors])
        self.assertEqual(wingeom.overlap_area(STORED, logical[0]), 0,
                         "control: the LOGICAL mapping is the corrupt one")

    def test_mixed_dpi_index_follows_the_physical_layout(self):
        # The rule is PER MONITOR (brief §1.1): each monitor's own fields
        # are multiplied by its own scale_factor.  HiDPI left (scale 2 ⇒
        # 0..5120) + scale-1 right ⇒ the physical list GTK's numbers
        # produce.  A rect at x=4000 sits inside the HiDPI head; the same
        # numbers against GTK's LOGICAL list fall on the OTHER head.
        mons = [FakeMonitor((0, 0, 2560, 1440), (0, 40, 2560, 1400), scale=2),
                FakeMonitor((2560, 0, 1920, 1080), (2560, 0, 1920, 1020),
                            scale=1)]
        stub = GeomStub(config.GeometryStore(_tmp_path()), mons)
        rect = (4000, 200, 900, 700)
        self.assertEqual(stub._geom_monitor_geos(),
                         [(0, 0, 5120, 2880), (2560, 0, 1920, 1080)])
        self.assertEqual(stub._geom_monitor_index(rect), 0)
        self.assertEqual(
            wingeom.pick_monitor(rect, [(0, 0, 2560, 1440),
                                        (2560, 0, 1920, 1080)]), 1,
            "control: the LOGICAL list sends the same rect to the wrong head")

    def test_two_physical_heads_pick_the_right_one(self):
        mons = [FakeMonitor((0, 0, 2560, 1440), (0, 40, 2560, 1400), scale=2),
                FakeMonitor((2560, 0, 2560, 1440), (2560, 40, 2560, 1400),
                            scale=2)]
        stub = GeomStub(config.GeometryStore(_tmp_path()), mons)
        self.assertEqual(stub._geom_monitor_index((5400, 200, 900, 700)), 1)
        self.assertEqual(stub._geom_monitor_index((100, 200, 900, 700)), 0)


if __name__ == "__main__":
    unittest.main()
