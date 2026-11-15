"""ROUND 24 §C.2 gates: never invent a monitor index from a position we
never measured.

On Wayland (or any surface without an xid) `x11pos.get_rect()` answers None,
so `_geom_live_rect()` keeps the last KNOWN x/y and refreshes only the size
— the size is observable, the position is not.  Persisting a monitor index
recomputed from that carried-forward x/y writes an index we did not measure
into `window.json`, and the next window's restore/clamp trusts it
(`plan_open_geometry` reads `last_monitor()` first).  The honest answer when
the position is unobservable is the last index we ever MEASURED —
`GeometryStore.last_monitor()` — and on X11, where the position really is
measured, the index keeps being recomputed exactly as before (the R22
physical-pixel math is untouched).

Harness: the tests/test_window_placement_r21.py pattern — the REAL window
methods unbound onto a bare stub, no display, no X, no sleep.  `FakeX`
replays what the compositor has actually applied; `then=None` is the Wayland
path.  RED against pre-R24 code, which always recomputes.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

import _bootstrap  # noqa: F401  (sys.path + writable TMP_ROOT)
import window
from window import ReelseeWindow

# Two physical monitors side by side (R22: the index is computed in PHYSICAL
# pixels, which is raw-Xlib space — the same space the stored rect lives in).
MON0 = (0, 0, 1920, 1080)
MON1 = (1920, 0, 1920, 1080)
LEFT = (100, 100, 900, 700)            # sits on monitor 0
RIGHT = (2000, 120, 900, 700)          # sits on monitor 1


def _bind(name):
    """Bind a real window method onto the stub; a pre-R24 tree has no
    _geom_store_monitor, so binding a no-op keeps the RED an honest
    assertion about the stored index instead of an AttributeError."""
    return getattr(ReelseeWindow, name, lambda self, rect: None)


class FakeRect:
    def __init__(self, geo):
        self.x, self.y, self.width, self.height = geo


class FakeMonitor:
    def __init__(self, geo, scale=1):
        self._geo = geo
        self._scale = scale

    def get_geometry(self):
        return FakeRect(self._geo)

    def get_workarea(self):
        return FakeRect(self._geo)

    def get_scale_factor(self):
        return self._scale


class FakeDisplay:
    """mons: [(geo, scale), ...] — scale 1 unless a test says otherwise."""

    def __init__(self, mons):
        self._mons = [FakeMonitor(g, s) for g, s in mons]

    def get_monitors(self):
        return self._mons


class FakeStore:
    """config.GeometryStore stand-in: records remember()/flush() and answers
    last_monitor() with the index the last MEASURED write stored."""

    def __init__(self, last_monitor=None):
        self.remembered = []
        self.flushes = 0
        self._last_monitor = last_monitor

    def remember(self, rect, monitor=None, **kw):
        self.remembered.append((tuple(rect) if rect is not None else None,
                                monitor, kw.get("windowed", True)))
        if monitor is not None:
            self._last_monitor = monitor
        return True

    def last_monitor(self):
        return self._last_monitor

    def flush(self):
        self.flushes += 1


class FakeX:
    """x11pos stand-in: `then` is what get_rect reports (None = no X11 answer
    at all, the Wayland path).  Records every read."""

    def __init__(self, then=None):
        self.then = then
        self.reads = []

    def get_rect(self, win):
        self.reads.append(self.then)
        return self.then

    def set_rect(self, win, rect):
        return True


class FakeClock:
    """GLib.timeout_add / source_remove that only fires when told — so the
    250 ms debounce is driven, not slept (and nothing is left registered)."""

    def __init__(self):
        self.scheduled = []
        self._timers = {}
        self._next = 1

    def timeout_add(self, ms, fn, *args):
        tid = self._next
        self._next += 1
        self.scheduled.append(int(ms))
        self._timers[tid] = fn
        return tid

    def source_remove(self, tid):
        return self._timers.pop(tid, None) is not None

    def fire_all(self):
        for tid, fn in list(self._timers.items()):
            self._timers.pop(tid)
            fn()


class GeomStub:
    """The REAL R10/R20/R22/R24 persist methods on a bare object."""
    _geom_enabled = ReelseeWindow._geom_enabled
    _geom_windowed = ReelseeWindow._geom_windowed
    _geom_live_rect = ReelseeWindow._geom_live_rect
    _geom_monitor_index = ReelseeWindow._geom_monitor_index
    _geom_monitor_scales = ReelseeWindow._geom_monitor_scales
    _geom_scale_for = ReelseeWindow._geom_scale_for
    _geom_sample = ReelseeWindow._geom_sample
    _geom_schedule_persist = ReelseeWindow._geom_schedule_persist
    _geom_persist_now = ReelseeWindow._geom_persist_now
    _geom_persist = ReelseeWindow._geom_persist
    capture_geometry = ReelseeWindow.capture_geometry
    _geom_store_monitor = _bind("_geom_store_monitor")

    def __init__(self, store, rect=LEFT, size=(900, 700),
                 mons=((MON0, 1), (MON1, 1))):
        self.app = SimpleNamespace(geometry_store=store)
        self.cfg = SimpleNamespace(
            get=lambda key, d=None: {
                "ui.remember_geometry": True,
                "ui.cascade_offset_px": 15}.get(key, d))
        self._geom_rect = tuple(rect) if rect else None
        self._geom_timer = None
        self._geom_settle = None
        self._closing = False
        self._fs = False
        self._size = size
        self._mons = mons
        self._mapped = True

    def get_mapped(self):
        return self._mapped

    def is_fullscreen(self):
        return self._fs

    def is_maximized(self):
        return False

    def get_width(self):
        return self._size[0]

    def get_height(self):
        return self._size[1]

    def get_display(self):
        return FakeDisplay(self._mons)


class PersistCase(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        p = mock.patch.object(window, "GLib", self.clock)
        p.start()
        self.addCleanup(p.stop)

    def patch_x(self, fx):
        for name in ("set_rect", "get_rect"):
            p = mock.patch.object(window.x11pos, name, getattr(fx, name))
            p.start()
            self.addCleanup(p.stop)
        return fx


class PositionUnobservable(PersistCase):
    """§C.2: get_rect answers None (no xid — a Wayland toplevel), so the x/y
    in the rect we are about to store is carried forward, not measured."""

    def test_persist_carries_forward_the_last_measured_index(self):
        """The window was MEASURED onto monitor 1 earlier; the rect still
        carries monitor 0's coordinates only because that is the last
        position we knew — so recomputing would say 0."""
        store = FakeStore(last_monitor=1)
        self.patch_x(FakeX(then=None))
        stub = GeomStub(store, rect=LEFT)
        stub._geom_persist()
        self.assertEqual(store.remembered, [(LEFT, 1, True)],
                         "the index must be the last MEASURED one, not one "
                         "recomputed from an unmeasured position")

    def test_capture_geometry_carries_it_forward_too(self):
        store = FakeStore(last_monitor=1)
        self.patch_x(FakeX(then=None))
        stub = GeomStub(store, rect=LEFT, size=(1200, 800))
        stub.capture_geometry()
        self.assertEqual(len(store.remembered), 1)
        rect, monitor, windowed = store.remembered[0]
        self.assertEqual(monitor, 1)
        self.assertEqual(rect, (LEFT[0], LEFT[1], 1200, 800),
                         "the observable SIZE is still refreshed")
        self.assertTrue(windowed)
        self.assertEqual(store.flushes, 1, "capture_geometry still flushes")

    def test_no_stored_index_is_none_not_a_guess(self):
        """Nothing was ever measured ⇒ store None.  Recording a computed 0
        here would claim the window is on monitor 0 as a FACT."""
        store = FakeStore(last_monitor=None)
        self.patch_x(FakeX(then=None))
        stub = GeomStub(store, rect=LEFT)
        stub._geom_persist()
        self.assertEqual(store.remembered, [(LEFT, None, True)])

    def test_never_raises_when_the_store_cannot_answer(self):
        """§C.2 is a defensive guard: a store that cannot answer must not
        take the save path (and therefore the whole close) down."""
        class Broken(FakeStore):
            def last_monitor(self):
                raise RuntimeError("state file unreadable")

        store = Broken(last_monitor=1)
        self.patch_x(FakeX(then=None))
        stub = GeomStub(store, rect=LEFT)
        stub._geom_persist()                    # must not raise
        self.assertEqual(store.remembered, [(LEFT, None, True)])

    def test_sampler_path_uses_the_same_rule(self):
        """The 1 s sampler is the code that actually writes during a resize
        storm, so its persist must carry the index forward too."""
        store = FakeStore(last_monitor=1)
        fx = self.patch_x(FakeX(then=None))
        stub = GeomStub(store, rect=LEFT, size=(1000, 720))
        self.assertTrue(stub._geom_sample())
        self.assertEqual(stub._geom_rect, (LEFT[0], LEFT[1], 1000, 720))
        self.assertEqual(store.remembered, [], "still debounced")
        self.clock.fire_all()                   # the ~250 ms debounce fires
        self.assertEqual(store.remembered, [((LEFT[0], LEFT[1], 1000, 720),
                                             1, True)],
                         "sampler-driven persist follows the same rule")
        self.assertEqual(len(fx.reads), 2,
                         "one read for the sample, one for the index")


class PositionObservable(PersistCase):
    """Non-vacuity: when the position IS measured, keep recomputing the index
    (R22) — the guard must not freeze a stale value onto a real move."""

    def test_real_move_recomputes_the_index(self):
        store = FakeStore(last_monitor=0)      # stale: the window has MOVED
        self.patch_x(FakeX(then=RIGHT))
        stub = GeomStub(store, rect=RIGHT)
        stub._geom_persist()
        self.assertEqual(store.remembered, [(RIGHT, 1, True)],
                         "an observed position wins over the stored index")

    def test_capture_geometry_recomputes_on_x11(self):
        store = FakeStore(last_monitor=0)
        self.patch_x(FakeX(then=RIGHT))
        stub = GeomStub(store, rect=LEFT, size=(900, 700))
        stub.capture_geometry()
        self.assertEqual(store.remembered, [(RIGHT, 1, True)],
                         "the live rect is stored AND re-indexed")

    def test_index_math_is_physical_pixels(self):
        """R22 regression, re-asserted here so the R24 guard cannot be
        blamed for changing the math: monitors reported at scale 2 must be
        scaled up to physical before the index is picked."""
        self.patch_x(FakeX(then=RIGHT))
        stub = GeomStub(None, rect=RIGHT)
        stub._mons = [((0, 0, 960, 540), 2), ((960, 0, 960, 540), 2)]
        self.assertEqual(stub._geom_monitor_index(RIGHT), 1)
        self.assertEqual(stub._geom_monitor_index(LEFT), 0)


class NoDisplayStillSafe(PersistCase):
    """A display that cannot be queried (mid-teardown) must not raise, and
    must not fabricate an index."""

    def test_unobservable_and_no_display(self):
        store = FakeStore(last_monitor=1)
        self.patch_x(FakeX(then=None))
        stub = GeomStub(store, rect=LEFT)
        stub.get_display = lambda: (_ for _ in ()).throw(
            AssertionError("no display"))
        stub._geom_persist()
        self.assertEqual(store.remembered, [(LEFT, 1, True)])

    def test_observable_but_no_display_keeps_todays_answer(self):
        store = FakeStore(last_monitor=1)
        self.patch_x(FakeX(then=RIGHT))
        stub = GeomStub(store, rect=LEFT)
        stub.get_display = lambda: (_ for _ in ()).throw(
            AssertionError("no display"))
        stub._geom_persist()
        self.assertEqual(store.remembered, [(LEFT, None, True)])


class NoXidNoticeIsVisible(unittest.TestCase):
    """§C.1: the one line that explains why a remembered position was
    ignored must be visible at the DEFAULT level, and still only once.

    Pre-R24 this was `log.debug`, so a normal launch logged nothing and the
    symptom read as "the feature is broken" rather than "this backend cannot
    position windows"."""

    class NoXidSurface:
        """Exactly what a Wayland toplevel surface is: no get_xid at all."""

        def __init__(self):
            self.n = 0

        def connect(self, *_a, **_k):
            self.n += 1
            return 0

    def test_no_xid_logs_one_INFO_line(self):
        import logging

        import x11pos
        x11pos.reset_logging_for_tests()
        self.addCleanup(x11pos.reset_logging_for_tests)
        win = SimpleNamespace(get_surface=lambda: self.NoXidSurface())
        with self.assertLogs("reelsee.x11pos", level="INFO") as cm:
            for _ in range(5):                   # the sampler calls it every
                self.assertIsNone(x11pos.surface_xid(win))
        self.assertEqual(len(cm.records), 1,
                         f"ONE line, not per-tick spam: {cm.records}")
        rec = cm.records[0]
        self.assertEqual(rec.levelno, logging.INFO,
                         "visible at the default level (was DEBUG)")
        self.assertIn("no xid", rec.getMessage())

    def test_other_degradations_stay_debug(self):
        """Only the no-xid notice was promoted; the rest of the seam keeps
        its quiet DEBUG degradation (the log must not fill up)."""
        import logging

        import x11pos
        x11pos.reset_logging_for_tests()
        self.addCleanup(x11pos.reset_logging_for_tests)
        with self.assertLogs("reelsee.x11pos", level="DEBUG") as cm:
            x11pos._debug_once("xlib", "no Xlib display (%s)", "OSError")
        self.assertEqual(len(cm.records), 1)
        self.assertEqual(cm.records[0].levelno, logging.DEBUG)


if __name__ == "__main__":
    unittest.main()
