"""ROUND 20 gates: exact reopen unless >=2 corners are off the screen, and
a 15 px CENTERWARD cascade.

The user's two rules (§0 of references/ROUND20-BRIEF.md):

  1. a window reopens at the SAME coordinates and dimensions it was closed
     at — UNLESS at least 2 of its corners are not on the current screen;
     only then may it be repositioned;
  2. a second window opens 15 px from the open one TOWARD THE SCREEN CENTRE.

`corners_offscreen` counts corner POINTS that fall outside the screen
(edge-exact counts as ON screen), `centerward_place` moves a rect toward
the work-area centre per axis without ever overshooting it, and
`plan_open_geometry` glues them together.  Same harness pattern as
tests/test_wingeom.py: the REAL window methods run unbound on a bare stub
with a fake display, so the whole decision tree is testable with no
display and no clock.

The corner rule is measured against the SCREEN (monitor geometry), not the
work area — see the comment in window.plan_open_geometry and the report:
an axis-aligned rect can never have exactly ONE corner outside a convex
work area (any partial hang-off-edge puts BOTH corners on that side out),
so "count < 2 ⇒ verbatim" against the work area would be the no-op the
user is complaining about, and the operator recipe ("close a window with
one corner under the panel ⇒ reopen at the identical coords+dims") would
still reposition.  A corner under the panel IS on the current screen.
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

WA = (0, 0, 1600, 1000)                  # the single-Xvfb-monitor world
PANEL = (0, 0, 1600, 940)                # same screen, 60 px bottom panel
TWO = [(0, 0, 1600, 1000), (1600, 0, 1920, 1080)]
# an L-shaped two-head layout: the notch (x >= 1200, y >= 1000) is the only
# place a single axis-aligned rect can miss the screen with EXACTLY one
# corner (see test_exactly_one_corner_never_off_a_single_screen).
LAYOUT = [(0, 0, 1600, 1000), (0, 1000, 1200, 800)]


class CornersOffscreen(unittest.TestCase):
    """B1 truth table: 0/1/2/3/4 corners off, edge-exact = on screen,
    garbage ⇒ None."""

    def test_zero_corners_off_fully_inside(self):
        self.assertEqual(wingeom.corners_offscreen((100, 100, 760, 430), WA), 0)

    def test_one_corner_off(self):
        # the notch of an L-shaped two-head layout: three corners land on a
        # monitor, the bottom-right one is on no screen at all
        self.assertEqual(
            wingeom.corners_offscreen((1150, 950, 200, 100), LAYOUT), 1)

    def test_two_corners_off_same_side(self):
        # hangs off the right edge: top-right AND bottom-right are out
        self.assertEqual(wingeom.corners_offscreen((1500, 500, 400, 300), WA), 2)
        # ...and off the bottom edge: bottom-left AND bottom-right
        self.assertEqual(wingeom.corners_offscreen((100, 900, 400, 300), WA), 2)

    def test_three_corners_off(self):
        # off the bottom-right corner region: only the top-left survives
        self.assertEqual(wingeom.corners_offscreen((1500, 950, 400, 200), WA), 3)

    def test_four_corners_off(self):
        # remembered from a bigger monitor that is gone now
        self.assertEqual(wingeom.corners_offscreen((3400, 50, 760, 430), WA), 4)
        self.assertEqual(wingeom.corners_offscreen((-900, -900, 200, 200), WA), 4)

    def test_edge_exact_counts_as_on_screen(self):
        # a corner exactly ON an edge is ON screen (inclusive bounds)
        self.assertEqual(wingeom.corners_offscreen((0, 0, 1600, 1000), WA), 0)
        self.assertEqual(wingeom.corners_offscreen((840, 570, 760, 430), WA), 0)
        self.assertEqual(wingeom.corners_offscreen((840, 570, 761, 430), WA), 2)
        # a work area that does not start at the origin
        self.assertEqual(
            wingeom.corners_offscreen((2000, 0, 1520, 1080), (1600, 0, 1920, 1080)),
            0)

    def test_multi_monitor_union_counts_screen_not_monitor(self):
        # straddling the seam of two side-by-side monitors: every corner is
        # on A screen, so none is off the current screen — even though the
        # window hangs off monitor 0's right edge
        self.assertEqual(wingeom.corners_offscreen((1500, 100, 400, 300), TWO), 0)
        # off the far right of the SECOND monitor: 2 corners off
        self.assertEqual(wingeom.corners_offscreen((3400, 100, 400, 300), TWO), 2)

    def test_exactly_one_corner_never_off_a_single_screen(self):
        # Geometry, not a whim: for an axis-aligned rect against ONE convex
        # work area the count is 0, 2, 3 or 4 — a hang-off-edge always takes
        # both corners on that side.  (This is why the corner rule is
        # measured against the SCREEN, so "one corner under the panel"
        # counts 0 and restores verbatim.)
        seen = set()
        for x in (-200, 0, 100, 840, 1100, 1500, 1600, 3400):
            for y in (-200, 0, 100, 570, 900, 950, 1000, 1200):
                for w, h in ((760, 430), (400, 300), (2000, 1200)):
                    seen.add(wingeom.corners_offscreen((x, y, w, h), WA))
        self.assertEqual(seen, {0, 2, 3, 4})

    def test_garbage_inputs_are_none(self):
        for bad in (None, "nope", (1, 2), {"x": 1}, object(), (1, 2, 3, "x")):
            self.assertIsNone(wingeom.corners_offscreen(bad, WA), bad)
        for bad_wa in (None, "nope", (), [], (1, 2), [None], [object()]):
            self.assertIsNone(wingeom.corners_offscreen((0, 0, 100, 100), bad_wa),
                              bad_wa)

    def test_accepts_gdk_like_objects_and_dicts(self):
        mon = SimpleNamespace(x=0, y=0, width=1600, height=1000)
        self.assertEqual(wingeom.corners_offscreen({"x": 100, "y": 100,
                                                    "w": 760, "h": 430}, mon), 0)
        self.assertEqual(wingeom.corners_offscreen((100, 100, 760, 430),
                                                   [mon]), 0)


class CenterwardPlace(unittest.TestCase):
    """B2: move toward the work-area centre, per axis, never overshooting,
    size NEVER changes, offset 0 == identical, multi-monitor picks the
    SOURCE monitor."""

    def test_left_of_centre_moves_right(self):
        # source centre (520, 305), work-area centre (800, 500): +15 on both
        self.assertEqual(
            wingeom.centerward_place((140, 90, 760, 430), WA, 15),
            (155, 105, 760, 430))

    def test_right_of_centre_moves_left(self):
        # source centre (1080, 715), both right of and below the centre:
        # -15 on x, -15 on y
        self.assertEqual(
            wingeom.centerward_place((700, 500, 760, 430), WA, 15),
            (685, 485, 760, 430))

    def test_above_centre_moves_down(self):
        # x left of centre (+15), y BELOW centre (-15): the axes are decided
        # independently — that is the whole point of "per axis"
        self.assertEqual(
            wingeom.centerward_place((100, 600, 400, 300), WA, 15),
            (115, 585, 400, 300))

    def test_below_centre_moves_up(self):
        self.assertEqual(
            wingeom.centerward_place((1000, 100, 400, 300), WA, 15),
            (985, 115, 400, 300))

    def test_never_overshoots_the_centre(self):
        # 5 px from the centre on x, exactly centred on y
        self.assertEqual(
            wingeom.centerward_place((755, 450, 100, 100), WA, 15),
            (750, 450, 100, 100))
        # a source already centred on an axis does not move on that axis
        self.assertEqual(
            wingeom.centerward_place((700, 450, 200, 100), WA, 15),
            (700, 450, 200, 100))
        # an offset far larger than the distance lands ON the centre
        self.assertEqual(
            wingeom.centerward_place((100, 100, 1400, 800), WA, 5000),
            (100, 100, 1400, 800))          # centre already reached: (100,100)
        self.assertEqual(
            wingeom.centerward_place((0, 0, 760, 430), WA, 5000),
            (420, 285, 760, 430))          # exactly centred, not past it

    def test_size_never_changes(self):
        for src in ((140, 90, 760, 430), (900, 600, 760, 430),
                    (1500, 950, 400, 200), (-40, 20, 300, 200),
                    (0, 0, 40, 40), (1590, 990, 40, 40)):
            for off in (0, 1, 15, 200, 5000):
                for wa in (WA, PANEL, *TWO):
                    out = wingeom.centerward_place(src, wa, off)
                    self.assertIsNotNone(out, (src, wa, off))
                    self.assertEqual(out[2:], src[2:],
                                     f"src={src} wa={wa} off={off} -> {out}")

    def test_result_stays_in_the_work_area(self):
        # a source hanging off the bottom-right: pulled toward the centre
        # AND clamped fully inside, at the same size
        out = wingeom.centerward_place((1500, 900, 760, 430), WA, 15)
        self.assertEqual(out, (840, 570, 760, 430))
        for src in ((-500, -500, 300, 200), (3000, 2000, 300, 200),
                    (1590, 990, 40, 40)):
            out = wingeom.centerward_place(src, WA, 15)
            self.assertGreaterEqual(out[0], 0)
            self.assertGreaterEqual(out[1], 0)
            self.assertLessEqual(out[0] + out[2], WA[2])
            self.assertLessEqual(out[1] + out[3], WA[3])

    def test_offset_zero_is_identical(self):
        for src in ((140, 90, 760, 430), (900, 600, 760, 430),
                    (-40, 20, 300, 200)):
            self.assertEqual(wingeom.centerward_place(src, WA, 0),
                             wingeom.norm_rect(src))

    def test_non_numeric_offset_behaves_as_zero(self):
        src = (140, 90, 760, 430)
        for off in (None, "x", -5, True):
            self.assertEqual(wingeom.centerward_place(src, WA, off), src, off)

    def test_two_monitors_use_the_source_monitor(self):
        # source on monitor 1 (centre 2560, 540): it is LEFT of and ABOVE
        # that centre ⇒ +15/+15.  Using monitor 0's centre instead would
        # move it LEFT — the reference frame is the source monitor.
        self.assertEqual(
            wingeom.centerward_place((1650, 100, 800, 500), TWO, 15),
            (1665, 115, 800, 500))
        # a source on monitor 0 moves toward monitor 0's centre (right/down)
        self.assertEqual(
            wingeom.centerward_place((100, 100, 760, 430), TWO, 15),
            (115, 115, 760, 430))
        # straddling the seam: the monitor holding MORE of the window is the
        # frame (pick_monitor), and the result stays inside that frame
        self.assertEqual(
            wingeom.centerward_place((1500, 100, 760, 430), TWO, 15),
            (1600, 115, 760, 430))

    def test_garbage_in_no_crash_out(self):
        for bad in (None, "nope", (1, 2), {"x": 1}, object()):
            self.assertIsNone(wingeom.centerward_place(bad, WA, 15), bad)
        self.assertIsNone(wingeom.centerward_place((0, 0, 100, 100), None, 15))
        self.assertIsNone(wingeom.centerward_place((0, 0, 100, 100), (), 15))

    def test_cascade_place_still_exists_and_works(self):
        # §A1: cascade_place stays in the module (its unit tests stay green)
        # even though the window no longer calls it
        self.assertEqual(
            wingeom.cascade_place((140, 90, 760, 430), WA, 50),
            (190, 90, 760, 430))


class FakeMonitor:
    """A monitor with a bottom PANEL: its work area is 60 px shorter than
    its geometry — exactly the desktop in the operator recipe."""

    def __init__(self, geo, wa=None):
        self._geo = geo
        self._wa = wa if wa is not None else geo

    def get_geometry(self):
        return SimpleNamespace(x=self._geo[0], y=self._geo[1],
                               width=self._geo[2], height=self._geo[3])

    def get_workarea(self):
        return SimpleNamespace(x=self._wa[0], y=self._wa[1],
                               width=self._wa[2], height=self._wa[3])


class PlanStub:
    """The REAL plan_open_geometry (plus the monitor/offset helpers it uses)
    on a bare object — the CaptureStub pattern of tests/test_wingeom.py."""
    _geom_enabled = ReelseeWindow._geom_enabled
    _geom_offset = ReelseeWindow._geom_offset
    _geom_monitor_areas = ReelseeWindow._geom_monitor_areas
    _geom_monitor_geos = ReelseeWindow._geom_monitor_geos
    plan_open_geometry = ReelseeWindow.plan_open_geometry

    def __init__(self, store, monitors, remember=True, offset=None):
        self.app = SimpleNamespace(geometry_store=store)
        vals = {"ui.remember_geometry": remember}
        if offset is not None:
            vals["ui.cascade_offset_px"] = offset
        self.cfg = SimpleNamespace(get=lambda k, d=None: vals.get(k, d))
        self._monitors = monitors

    def get_display(self):
        return SimpleNamespace(get_monitors=lambda: list(self._monitors))


class SourceStub:
    def __init__(self, rect):
        self._rect = rect

    def rect_for_cascade(self):
        return self._rect


class PlanOpenGeometry(unittest.TestCase):
    """B3: the remembered-rect decision over the REAL method.  The screen
    has a 60 px bottom panel: work area (0,0,1600,940), geometry
    (0,0,1600,1000)."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="r20-plan-",
                                         dir=str(_bootstrap.TMP_ROOT)))
        self.path = self.dir / "window.json"
        self.mon = FakeMonitor((0, 0, 1600, 1000), (0, 0, 1600, 940))

    def plan(self, rect=None, monitor=0, **kw):
        store = config.GeometryStore(self.path)
        if rect is not None:
            store.remember(rect, monitor)
            store.flush()
        return PlanStub(config.GeometryStore(self.path), [self.mon],
                        **kw).plan_open_geometry(None)

    def test_one_corner_under_the_panel_restores_verbatim(self):
        # THE user-visible fix: closed with its bottom corners under the
        # panel (2 corners off the WORK AREA, 0 off the SCREEN) ⇒ the exact
        # coords+dims, no clamp.  Today clamp_into_workarea always pulls it
        # fully on-screen, so this gate fails without the change.
        stored = (100, 550, 760, 430)          # bottom 980: under the panel
        self.assertEqual(wingeom.corners_offscreen(stored, PANEL), 2,
                         "control: the rect really does hang off the work area")
        self.assertEqual(wingeom.corners_offscreen(stored, (0, 0, 1600, 1000)), 0,
                         "control: no corner is off the SCREEN")
        self.assertEqual(wingeom.clamp_into_workarea(stored, PANEL),
                         (100, 510, 760, 430),
                         "control: the old rule moved it")
        self.assertEqual(self.plan(stored), ("restore", stored))

    def test_two_corners_off_the_screen_repositions(self):
        # hangs off the right edge of the SCREEN ⇒ 2 corners off ⇒ today's
        # reposition (clamp_into_workarea) still runs
        stored = (1500, 100, 760, 430)
        self.assertEqual(wingeom.corners_offscreen(stored, (0, 0, 1600, 1000)), 2)
        self.assertEqual(self.plan(stored)[1],
                         wingeom.clamp_into_workarea(stored, PANEL))
        self.assertEqual(self.plan(stored), ("restore", (840, 100, 760, 430)))

    def test_three_and_four_corners_off_reposition(self):
        for stored in ((1500, 900, 760, 430), (3400, 50, 760, 430)):
            out = self.plan(stored)
            self.assertEqual(out[0], "restore")
            self.assertEqual(out[1], wingeom.clamp_into_workarea(stored, PANEL))

    def test_fully_on_screen_still_restores_verbatim(self):
        self.assertEqual(self.plan((140, 90, 760, 430)),
                         ("restore", (140, 90, 760, 430)))

    def test_cascade_branch_is_centerward_15(self):
        stub = PlanStub(config.GeometryStore(self.path), [self.mon])
        src = SourceStub((140, 90, 760, 430))
        self.assertEqual(stub.plan_open_geometry(src),
                         ("cascade", (155, 105, 760, 430)))

    def test_offset_setting_is_live_and_defaults_to_15(self):
        self.assertEqual(PlanStub(config.GeometryStore(self.path),
                                  [self.mon])._geom_offset(), 15)
        self.assertEqual(PlanStub(config.GeometryStore(self.path), [self.mon],
                                  offset=0)._geom_offset(), 0)
        self.assertEqual(PlanStub(config.GeometryStore(self.path), [self.mon],
                                  offset=999)._geom_offset(), 200)
        self.assertEqual(PlanStub(config.GeometryStore(self.path), [self.mon],
                                  offset="junk")._geom_offset(), 15)
        self.assertEqual(config.DEFAULTS["ui"]["cascade_offset_px"], 15)

    def test_no_source_no_memory_is_none(self):
        self.assertIsNone(self.plan(None))

    def test_remembering_off_disables_the_plan(self):
        self.assertIsNone(self.plan((140, 90, 760, 430), remember=False))

    def test_two_head_straddle_restores_verbatim(self):
        # a window across the seam of two monitors: every corner is on a
        # screen ⇒ verbatim, even though it hangs off monitor 0's right edge
        mons = [FakeMonitor((0, 0, 1600, 1000)), FakeMonitor((1600, 0, 1920, 1080))]
        store = config.GeometryStore(self.path)
        store.remember((1500, 100, 400, 300), 0)
        store.flush()
        stub = PlanStub(config.GeometryStore(self.path), mons)
        self.assertEqual(stub.plan_open_geometry(None),
                         ("restore", (1500, 100, 400, 300)))


class SettingsRow(unittest.TestCase):
    """§A2: the row stays; its subtitle now says the cascade is toward the
    screen centre."""

    def test_cascade_row_subtitle(self):
        import settings_ui as S
        row = next(b for b in S.BINDINGS if b["key"] == "ui.cascade_offset_px")
        self.assertEqual(row["kind"], "scale")
        self.assertEqual(row["minimum"], 0)
        self.assertEqual(row["maximum"], 200)
        self.assertIn("centre", row["subtitle"])


if __name__ == "__main__":
    unittest.main()
