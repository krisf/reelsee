"""ROUND 25 gates: the scale factor at the two window-geometry seams.

The measured cause (operator, live GNOME Wayland + XWayland, scale=2,
2026-11-05): the saved rect is raw-Xlib PHYSICAL, but GTK's
``set_default_size()`` takes LOGICAL px.  A window closed at 2180x1828
physical was handed to GTK as 2180x1828 LOGICAL = 4360x3656 physical,
taller than the 2880 px screen, so Mutter clamped the height to the
screen and the aspect fit widened the result to 4986x2800 — visually a
maximized window.  The user's words: "not only does it not keep size, it
always starts maximized now".

R22 fixed the MONITOR side of this (helpers return physical extents so
physical rects clamp against physical screens) but never converted the
WINDOW's own size.  Two seams remain, and they are mirror images:

  adopt_geometry   physical rect -> set_default_size (LOGICAL api)
                   must DIVIDE by the scale factor
  _geom_live_rect  the Wayland fallback reads GTK's LOGICAL w/h and
                   stores them into a file whose x/y are PHYSICAL
                   must MULTIPLY by the scale factor

The invariant these tests lock: ``window.json`` is always PHYSICAL.

Harness: the R22 pattern (tests/test_monitor_geom_r22.py) — the REAL
window methods unbound on a bare stub with a fake display, so a scale=2
desktop is testable with no HiDPI hardware, no display and no sleep.
B4 is the regression guard for the scale=1 world every Xvfb gate lives
in: at scale 1 both seams must behave byte-identically to today's code.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import _bootstrap  # noqa: F401  (sys.path + writable TMP_ROOT)
import config
import x11pos
from window import ReelseeWindow

# The operator's live desktop: GTK reports LOGICAL, x11pos moves PHYSICAL.
LOGICAL_GEO = (0, 0, 2560, 1440)
LOGICAL_WA = (0, 40, 2560, 1400)

# The rect the user actually closed at, measured live in window.json.
STORED = (724, 680, 2180, 1828)


class FakeMonitor:
    """A Gdk.Monitor stand-in reporting LOGICAL extents plus a scale factor."""

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


class ScaleStub:
    """The REAL adopt_geometry / _geom_live_rect on a bare object.

    ``set_default_size`` records what GTK was asked for; ``get_width`` /
    ``get_height`` report GTK's LOGICAL size, which is what the Wayland
    fallback reads.
    """
    _geom_enabled = ReelseeWindow._geom_enabled
    _geom_monitor_index = ReelseeWindow._geom_monitor_index
    _geom_monitor_scales = ReelseeWindow._geom_monitor_scales
    _geom_scale_for = ReelseeWindow._geom_scale_for
    adopt_geometry = ReelseeWindow.adopt_geometry
    _geom_live_rect = ReelseeWindow._geom_live_rect

    def __init__(self, monitors, logical_size=(1090, 914), remember=True):
        self.app = SimpleNamespace(geometry_store=config.GeometryStore(
            _tmp_path()))
        self.cfg = SimpleNamespace(
            get=lambda k, d=None: {"ui.remember_geometry": remember}.get(k, d))
        self._monitors = monitors
        self._logical = logical_size
        self._geom_rect = None
        self._geom_open = None
        self._geom_adopted = False
        self._closing = False
        self.default_size_calls = []

    def get_display(self):
        return SimpleNamespace(get_monitors=lambda: list(self._monitors))

    def set_default_size(self, w, h):
        self.default_size_calls.append((w, h))

    def get_width(self):
        return self._logical[0]

    def get_height(self):
        return self._logical[1]


def hi_dpi_stub(monitors=None, **kw):
    mons = monitors if monitors is not None else [
        FakeMonitor(LOGICAL_GEO, LOGICAL_WA, scale=2)]
    return ScaleStub(mons, **kw)


def _tmp_path():
    d = Path(tempfile.mkdtemp(prefix="r25-scale-", dir=str(_bootstrap.TMP_ROOT)))
    return d / "window.json"


class B1AdoptConvertsToLogical(unittest.TestCase):
    """B1: adopt_geometry must hand GTK LOGICAL px.  Red on current code,
    which passes the physical rect straight through."""

    def test_scale_two_divides_both_axes(self):
        stub = hi_dpi_stub()
        stub.adopt_geometry(STORED)
        # 2180x1828 physical at scale 2 -> 1090x914 logical
        self.assertEqual(stub.default_size_calls, [(1090, 914)])

    def test_position_is_left_untouched(self):
        # only the SIZE crosses the boundary; x/y stay physical for x11pos
        stub = hi_dpi_stub()
        stub.adopt_geometry(STORED)
        self.assertEqual(stub._geom_open, STORED)
        self.assertEqual(stub._geom_rect, STORED)

    def test_odd_size_rounds_not_truncates_down(self):
        stub = hi_dpi_stub()
        stub.adopt_geometry((100, 100, 2181, 1829))
        w, h = stub.default_size_calls[0]
        self.assertIn(w, (1090, 1091))
        self.assertIn(h, (914, 915))

    def test_adopted_flag_still_set(self):
        stub = hi_dpi_stub()
        stub.adopt_geometry(STORED)
        self.assertTrue(stub._geom_adopted)


class B2LiveRectStoresPhysical(unittest.TestCase):
    """B2: the Wayland size-only fallback must store PHYSICAL w/h, because
    window.json's x/y are physical.  Red on current code, which stores
    GTK's logical size and so writes a half-size rect on a scale=2 rig."""

    def setUp(self):
        self._real = x11pos.get_rect
        x11pos.get_rect = lambda win: None          # no xid: Wayland
        self.addCleanup(self._restore)

    def _restore(self):
        x11pos.get_rect = self._real

    def test_wayland_fallback_multiplies_by_scale(self):
        stub = hi_dpi_stub(logical_size=(1090, 914))
        stub._geom_rect = (724, 680, 1, 1)          # last known physical pos
        self.assertEqual(stub._geom_live_rect(), (724, 680, 2180, 1828))

    def test_position_component_survives_untouched(self):
        stub = hi_dpi_stub(logical_size=(500, 400))
        stub._geom_rect = (2700, 200, 1, 1)
        self.assertEqual(stub._geom_live_rect()[:2], (2700, 200))

    def test_no_position_yet_falls_back_to_origin(self):
        stub = hi_dpi_stub(logical_size=(640, 360))
        stub._geom_rect = None
        self.assertEqual(stub._geom_live_rect(), (0, 0, 1280, 720))


class B3ScaleSelection(unittest.TestCase):
    """B3: the scale must come from the monitor the rect is ON, so a
    mixed-DPI multi-monitor rig uses the right factor."""

    def test_picks_the_monitor_under_the_rect(self):
        # A coherent mixed-DPI layout: the second monitor's LOGICAL origin
        # 960 x its scale 2 lands it at physical x=1920, flush against the
        # first.  Rects are physical, so the right-hand one must get 2.
        mons = [FakeMonitor((0, 0, 1920, 1080), scale=1),
                FakeMonitor((960, 0, 1920, 1080), scale=2)]
        stub = hi_dpi_stub(monitors=mons)
        self.assertEqual(stub._geom_scale_for((2500, 100, 800, 600)), 2)
        self.assertEqual(stub._geom_scale_for((100, 100, 800, 600)), 1)

    def test_scale_below_one_clamps_to_one(self):
        stub = hi_dpi_stub(monitors=[FakeMonitor(LOGICAL_GEO, scale=0)])
        self.assertEqual(stub._geom_scale_for((0, 0, 800, 600)), 1)

    def test_missing_scale_factor_treated_as_one(self):
        stub = hi_dpi_stub(monitors=[FakeMonitor(LOGICAL_GEO, scale=None)])
        self.assertEqual(stub._geom_scale_for((0, 0, 800, 600)), 1)

    def test_no_display_never_raises(self):
        stub = hi_dpi_stub()
        stub.get_display = lambda: (_ for _ in ()).throw(RuntimeError("gone"))
        self.assertEqual(stub._geom_scale_for((0, 0, 800, 600)), 1)


class B4ScaleOneIsByteIdentical(unittest.TestCase):
    """B4: the regression guard.  Every Xvfb gate in the battery runs at
    scale 1 — at that scale this round must change NOTHING, or we would
    have traded the HiDPI bug for a breakage on every other machine."""

    def test_adopt_geometry_unchanged_at_scale_one(self):
        stub = hi_dpi_stub(monitors=[FakeMonitor((0, 0, 2560, 1440), scale=1)])
        stub.adopt_geometry(STORED)
        self.assertEqual(stub.default_size_calls, [(2180, 1828)])

    def test_live_rect_unchanged_at_scale_one(self):
        real = x11pos.get_rect
        x11pos.get_rect = lambda win: None
        try:
            stub = hi_dpi_stub(monitors=[FakeMonitor((0, 0, 2560, 1440),
                                                     scale=1)],
                               logical_size=(900, 700))
            stub._geom_rect = (100, 120, 1, 1)
            self.assertEqual(stub._geom_live_rect(), (100, 120, 900, 700))
        finally:
            x11pos.get_rect = real


if __name__ == "__main__":
    unittest.main()
