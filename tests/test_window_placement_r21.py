"""ROUND 21 gates: the position-restore SETTLE LOOP
(references/ROUND21-BRIEF.md §1/§2).

The measured cause (§0, operator, live Mutter/XWayland): the save path is
fine, but restore was a ONE-SHOT race — a single post-map move can land
before the compositor finishes its own placement and is simply lost, and
Mutter ignores both create-position and PPosition/USPosition hints, so the
post-map move is the ONLY lever.  It must be RE-ASSERTED until the window
is OBSERVED at the target, within a bounded budget, and it must never fight
a user who grabs the window mid-settle.

Harness (the tests/test_wingeom.py pattern — the REAL window methods,
unbound on a bare stub, no display, no X, no sleep):

  * FakeClock replaces `window.GLib`: timers exist only in the test's own
    clock, so the ~150 ms retry cadence and the ~2 s / 8-attempt budget are
    asserted exactly instead of being slept through (and a runaway retry
    storm is caught, not waited out);
  * FakeX replaces `x11pos.set_rect` / `x11pos.get_rect`: set_rect records
    every move we issue, get_rect replays the rects the "compositor" has
    ACTUALLY applied — which is the whole point (a move we sent is not a
    position the window has).

B1 is the gate the brief demands first: a live rect that only matches the
target on the 3rd read ⇒ the loop re-issues and settles, with the exact
move-call count asserted.  Current code never re-issues, so B1–B4 are red
against it (that red is recorded in DSH-REPORT-R21.md).
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

import _bootstrap  # noqa: F401  (sys.path + writable TMP_ROOT)
import aspect
import window
from window import ReelseeWindow

# The brief's own live numbers: closed at (84,30,844,966); Mutter/XWayland
# placed the fresh window at its own centred spot instead.
TARGET = (84, 30, 844, 966)
CENTERED = (500, 400, 844, 966)
USER = (1100, 640, 844, 966)          # grabbed mid-settle and dragged

ATTEMPTS = 8                          # §1: up to 8 attempts
INTERVAL = 150                        # §1: ~150 ms apart
BUDGET_MS = 2000                      # §1: ~2 s total


def _bind(name):
    """Bind a real window method onto the stub.

    Pre-R21 code has no settle methods at all; binding a no-op for those
    keeps the RED an honest assertion about move counts ("current code
    never re-issues") instead of an AttributeError at import time."""
    return getattr(ReelseeWindow, name, lambda self, *a, **k: False)


class FakeClock:
    """GLib.timeout_add / source_remove on a clock that only moves when the
    test says so.  GLib's re-arm semantics are kept: a callback returning
    True is re-scheduled at the same interval, False drops it — so a loop
    that re-armed itself AND scheduled a fresh timer would double-fire and
    trip the storm guard below."""

    def __init__(self):
        self.now = 0
        self.scheduled = []           # every (interval_ms, source) ever added
        self.removed = []             # every source_remove ever called
        self._next = 1
        self._timers = {}             # id -> (due_ms, interval_ms, fn)

    def timeout_add(self, ms, fn, *args):
        tid = self._next
        self._next += 1
        self.scheduled.append(int(ms))
        self._timers[tid] = (self.now + int(ms), int(ms), fn)
        return tid

    def source_remove(self, tid):
        self.removed.append(tid)
        return self._timers.pop(tid, None) is not None

    def advance(self, ms):
        """Run every timer due within `ms`, in due order."""
        end = self.now + ms
        fired = 0
        while True:
            due = sorted((d, tid) for tid, (d, _iv, _fn) in self._timers.items()
                         if d <= end)
            if not due:
                break
            when, tid = due[0]
            _d, iv, fn = self._timers.pop(tid)
            self.now = when
            fired += 1
            if fired > 200:
                raise AssertionError("timer storm: >200 callbacks fired")
            if fn() is True:
                self._timers[tid] = (self.now + iv, iv, fn)
        self.now = end
        return self

    def pending(self):
        return len(self._timers)


class FakeX:
    """x11pos stand-in.  `live` is the scripted list of rects the compositor
    has applied, replayed one per get_rect read; `then` is what it reports
    once the script runs out (None = no X11 answer at all, the Wayland
    path).  `ok` is what set_rect reports (False = the seam degraded)."""

    def __init__(self, live=(), then=None, ok=True):
        self.script = list(live)
        self.then = then
        self.ok = ok
        self.moves = []
        self.reads = []

    def set_rect(self, win, rect):
        self.moves.append(tuple(rect))
        return self.ok

    def get_rect(self, win):
        r = self.script.pop(0) if self.script else self.then
        self.reads.append(r)
        return r


class FakeStore:
    """config.GeometryStore stand-in: records remember()/flush()."""

    def __init__(self):
        self.remembered = []
        self.flushes = 0

    def remember(self, rect, monitor=None, **kw):
        self.remembered.append((tuple(rect) if rect is not None else None,
                                monitor))
        return True

    def flush(self):
        self.flushes += 1


class SettleStub:
    """The REAL R10/R20/R21 placement methods on a bare object."""
    apply_open_position = ReelseeWindow.apply_open_position
    _geom_live_rect = ReelseeWindow._geom_live_rect
    _geom_windowed = ReelseeWindow._geom_windowed
    _geom_enabled = ReelseeWindow._geom_enabled
    _geom_monitor_index = ReelseeWindow._geom_monitor_index
    _geom_arm_guard = ReelseeWindow._geom_arm_guard
    _geom_sample = ReelseeWindow._geom_sample
    _geom_schedule_persist = ReelseeWindow._geom_schedule_persist
    _geom_persist_now = ReelseeWindow._geom_persist_now
    _geom_persist = ReelseeWindow._geom_persist
    # R24 §C.2: the persist path now asks this which monitor index to store
    # (harness mirror of the real class; no assertion in this file changed).
    _geom_store_monitor = _bind("_geom_store_monitor")
    _geom_settle_start = _bind("_geom_settle_start")
    _geom_settle_step = _bind("_geom_settle_step")
    _geom_settle_tick = _bind("_geom_settle_tick")
    _geom_settle_schedule = _bind("_geom_settle_schedule")
    _geom_settle_finish = _bind("_geom_settle_finish")
    _geom_settle_stop = _bind("_geom_settle_stop")
    _geom_settle_disarm = _bind("_geom_settle_disarm")
    _geom_settle_aborted = _bind("_geom_settle_aborted")

    def __init__(self, store, open_rect=TARGET, remember=True):
        self.app = SimpleNamespace(geometry_store=store)
        self.cfg = SimpleNamespace(
            get=lambda key, d=None: {
                "ui.remember_geometry": remember,
                "ui.cascade_offset_px": 15}.get(key, d))
        self._geom_open = tuple(open_rect) if open_rect else None
        self._geom_rect = tuple(open_rect) if open_rect else None
        self._geom_settle = None
        self._geom_settle_moves = 0
        self._geom_settle_reason = None
        self._closing = False
        self._fs = False
        self._aspect_size = None
        self._aspect_applying = False
        self._geom_guard_id = None
        self._geom_timer = None
        self._geom_id = None
        self._mapped = True

    def get_mapped(self):
        return self._mapped

    def is_fullscreen(self):
        return self._fs

    def is_maximized(self):
        return False

    def get_width(self):
        return 0          # ≤ 1 ⇒ the size-only fallback declines

    def get_height(self):
        return 0

    def get_display(self):
        raise AssertionError("no display in a unit test")


class SettleCase(unittest.TestCase):
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


class B1ReissuesUntilObserved(SettleCase):
    """§B1: set_rect reports success but the live rect only matches the
    target on the 3rd read ⇒ the loop RE-ISSUES and ends settled, with the
    exact move count.  Current code sends one move and gives up ⇒ red."""

    def test_three_reads_three_moves_then_settled(self):
        fx = self.patch_x(FakeX([CENTERED, CENTERED, TARGET], then=TARGET))
        stub = SettleStub(FakeStore())
        stub.apply_open_position()
        # attempt 1: the move was SENT, the window is still where the WM
        # put it — that is precisely the bug the brief describes.
        self.assertEqual(fx.moves, [TARGET])
        self.assertEqual(fx.reads, [CENTERED])
        self.assertIsNotNone(getattr(stub, "_geom_settle", None),
                             "the loop must still be asserting the target")
        # the retry cadence is ~150 ms, not "immediately" and not "never"
        self.clock.advance(INTERVAL - 1)
        self.assertEqual(len(fx.moves), 1, "no move before the interval")
        self.clock.advance(1)
        self.assertEqual(len(fx.moves), 2, "re-issued at the 150 ms mark")
        self.clock.advance(INTERVAL)
        self.assertEqual(len(fx.moves), 3, "re-issued again")
        # the read AFTER this move is the target ⇒ settled, no 4th move
        self.assertEqual(fx.reads, [CENTERED, CENTERED, TARGET])
        self.assertIsNone(getattr(stub, "_geom_settle", None))
        self.assertEqual(getattr(stub, "_geom_settle_reason", None), "settled")
        self.assertEqual(stub._geom_rect, TARGET)
        self.clock.advance(10 * BUDGET_MS)
        self.assertEqual(len(fx.moves), 3, "settled ⇒ the loop is done")
        self.assertEqual(self.clock.pending(), 0, "no timer left running")

    def test_guard_rearmed_on_every_reissued_move(self):
        """§1: each re-issued move re-arms _geom_arm_guard, so a settle
        re-apply never reads as a user resize."""
        fx = self.patch_x(FakeX([CENTERED, CENTERED, TARGET], then=TARGET))
        stub = SettleStub(FakeStore())
        stub.apply_open_position()
        guard_ms = aspect.APPLY_GUARD_MS + 60
        self.assertEqual(self.clock.scheduled.count(guard_ms), 1)
        self.clock.advance(INTERVAL)
        self.assertEqual(self.clock.scheduled.count(guard_ms), 2)
        self.clock.advance(INTERVAL)
        self.assertEqual(self.clock.scheduled.count(guard_ms), 3)
        self.assertTrue(stub._aspect_applying, "still guarding our own echo")

    def test_rearm_never_steals_the_aspect_locks_latch(self):
        """The guard is shared with round 8's aspect apply.  A settle move
        while THAT holds the latch must leave it alone — taking it over
        would release the aspect lock early."""
        fx = self.patch_x(FakeX([], then=CENTERED))
        stub = SettleStub(FakeStore())
        stub._aspect_applying = True              # the aspect apply owns it
        stub.apply_open_position()
        self.assertEqual(len(fx.moves), 1, "the move is still issued")
        self.assertIsNone(stub._geom_guard_id,
                          "we did not take over somebody else's guard timer")
        self.assertTrue(stub._aspect_applying, "their latch survives")
        self.assertNotIn(aspect.APPLY_GUARD_MS + 60, self.clock.scheduled,
                         "no guard timer of ours was armed")

    def test_tolerance_is_4px_position_2px_size(self):
        """§1: "does not match within 4 px on x/y (size 2 px)" — both
        sides of the boundary, so the tolerance is not a guess."""
        near = (TARGET[0] + 4, TARGET[1] - 4, TARGET[2] + 2, TARGET[3] - 2)
        fx = self.patch_x(FakeX([near], then=near))
        stub = SettleStub(FakeStore())
        stub.apply_open_position()
        self.assertEqual(len(fx.moves), 1, f"{near} is inside tolerance")
        self.assertEqual(getattr(stub, "_geom_settle_reason", None), "settled")
        self.assertEqual(stub._geom_rect, near, "the observed truth is kept")

        off = (TARGET[0] + 5, TARGET[1], TARGET[2], TARGET[3])
        fx2 = self.patch_x(FakeX([off, off, off, off], then=off))
        stub2 = SettleStub(FakeStore())
        stub2.apply_open_position()
        self.assertEqual(len(fx2.moves), 1)
        self.clock.advance(INTERVAL)
        self.assertEqual(len(fx2.moves), 2, f"{off} is outside tolerance")


class B2BudgetGivesUp(SettleCase):
    """§B2: the live rect NEVER reaches the target ⇒ the loop gives up
    after the budget (≤ 8 moves, ~2 s), logs ONE debug line, and leaves
    the window where the WM put it."""

    def test_bounded_attempts_one_debug_line(self):
        fx = self.patch_x(FakeX([], then=CENTERED))
        stub = SettleStub(FakeStore())
        with self.assertLogs("reelsee.window", level="DEBUG") as cm:
            stub.apply_open_position()
            self.clock.advance(10 * BUDGET_MS)
        self.assertEqual(len(fx.moves), ATTEMPTS,
                         f"the budget is {ATTEMPTS} moves: {fx.moves}")
        self.assertEqual(len(fx.reads), ATTEMPTS, "one observed read per move")
        self.assertEqual(getattr(stub, "_geom_settle_reason", None), "budget")
        self.assertIsNone(getattr(stub, "_geom_settle", None))
        self.assertEqual(len(cm.records), 1,
                         f"ONE debug line, no per-retry spam: {cm.records}")
        self.assertEqual(stub._geom_rect, CENTERED,
                         "accepted: the window stays where the WM put it")
        self.clock.advance(10 * BUDGET_MS)
        self.assertEqual(len(fx.moves), ATTEMPTS, "gave up for good")
        self.assertEqual(self.clock.pending(), 0)

    def test_wall_clock_deadline_also_ends_the_loop(self):
        """§1: ~2 s total wall time is a budget in its own right — a WM
        that answers slowly must not stretch the fight indefinitely."""
        self.assertLessEqual(ATTEMPTS * INTERVAL, BUDGET_MS + INTERVAL,
                             "the attempt budget and the wall budget agree")
        fx = self.patch_x(FakeX([], then=CENTERED))
        stub = SettleStub(FakeStore())
        clock_now = [0.0]
        fake_time = SimpleNamespace(monotonic=lambda: clock_now[0])
        with mock.patch.object(window, "time", fake_time):
            stub.apply_open_position()
            clock_now[0] = 3.0                     # past the 2 s deadline
            self.clock.advance(INTERVAL)
        self.assertEqual(len(fx.moves), 1, "deadline reached ⇒ no more moves")
        self.assertEqual(getattr(stub, "_geom_settle_reason", None), "budget")
        self.assertIsNone(getattr(stub, "_geom_settle", None))

    def test_no_x11_degrades_quietly_without_raising(self):
        """§3: x11pos degradation semantics are unchanged — no X11 (Wayland)
        means no move ever lands, but the open path still completes, logs
        once, and never raises.  The budget still bounds it, and a move the
        seam refused is not counted as an applied move."""
        fx = self.patch_x(FakeX([], then=None, ok=False))
        stub = SettleStub(FakeStore())
        with self.assertLogs("reelsee.window", level="DEBUG") as cm:
            stub.apply_open_position()
            self.clock.advance(10 * BUDGET_MS)
        self.assertEqual(len(fx.moves), ATTEMPTS,
                         "still attempted, still bounded")
        self.assertEqual(stub._geom_settle_moves, 0,
                         "a refused move is not an applied move")
        self.assertEqual(getattr(stub, "_geom_settle_reason", None), "budget")
        self.assertIsNone(getattr(stub, "_geom_settle", None))
        self.assertEqual(stub._geom_rect, TARGET,
                         "nothing was ever observed ⇒ keep the planned rect")
        self.assertEqual(len(cm.records), 1)
        self.assertEqual(self.clock.pending(), 0)


class B3UserGrabEscape(SettleCase):
    """§B3: the live rect reports a position that is neither the target nor
    where we last saw it ⇒ the user grabbed the window; stop immediately
    and adopt their position."""

    def test_grab_adopts_and_stops(self):
        fx = self.patch_x(FakeX([CENTERED, USER], then=USER))
        stub = SettleStub(FakeStore())
        stub.apply_open_position()
        self.assertEqual(len(fx.moves), 1)
        self.clock.advance(INTERVAL)
        self.assertEqual(fx.reads, [CENTERED, USER])
        self.assertEqual(getattr(stub, "_geom_settle_reason", None),
                         "user-grab")
        self.assertIsNone(getattr(stub, "_geom_settle", None))
        self.assertEqual(stub._geom_rect, USER, "the user's position is adopted")
        self.clock.advance(10 * BUDGET_MS)
        self.assertEqual(len(fx.moves), 2, "no further moves: we never fight "
                                           "the user")

    def test_a_stable_wrong_position_is_not_a_grab(self):
        """Non-vacuity for B3: the WM ignoring our move (the bug) is NOT a
        user grab — the loop keeps fighting."""
        fx = self.patch_x(FakeX([], then=CENTERED))
        stub = SettleStub(FakeStore())
        stub.apply_open_position()
        self.clock.advance(INTERVAL)
        self.assertEqual(getattr(stub, "_geom_settle", None) is not None, True)
        self.clock.advance(INTERVAL)
        self.assertEqual(len(fx.moves), 3)


class B4MidSettleSamplesDoNotPersist(SettleCase):
    """§1.2: while the settle loop is active the 1 s sampler must NOT
    persist (never save the WM's centred position mid-fight), but it still
    tracks the live rect.  After the loop ends, sampling resumes normally."""

    def test_no_persist_while_active_one_after(self):
        store = FakeStore()
        # EVERY get_rect() consumes one scripted read — the settle loop's
        # AND the sampler's — so the script is annotated per read.
        fx = self.patch_x(FakeX([CENTERED,          # read 1: settle move 1
                                 CENTERED,          # read 2: mid-fight sample
                                 CENTERED,          # read 3: settle move 2
                                 CENTERED],         # read 4: mid-fight sample
                                then=TARGET))       # read 5+: move 3 wins
        stub = SettleStub(store)
        stub.apply_open_position()                    # move 1, read 1
        self.assertIsNotNone(getattr(stub, "_geom_settle", None))

        stub._geom_sample()                           # read 2
        self.assertEqual(stub._geom_rect, CENTERED,
                         "tracking still follows the live truth")
        self.assertEqual(store.remembered, [],
                         "no persist while the loop is fighting")
        self.assertIsNone(stub._geom_timer,
                          "not even the debounce was scheduled")

        self.clock.advance(INTERVAL)                  # move 2, read 3
        stub._geom_sample()                           # read 4
        self.assertEqual(store.remembered, [])
        self.assertEqual(len(fx.moves), 2)
        self.assertIsNotNone(getattr(stub, "_geom_settle", None))

        self.clock.advance(INTERVAL)                  # move 3, read 5 ⇒ target
        self.assertEqual(getattr(stub, "_geom_settle", None), None)
        self.assertEqual(stub._geom_rect, TARGET, "settled on the observed truth")
        self.assertEqual(store.remembered, [],
                         "settling must not write the file by itself")

        fx.then = USER                                # the user drags the window
        stub._geom_sample()                           # read 6: loop over ⇒ normal life
        self.assertEqual(stub._geom_rect, USER)
        self.assertEqual(store.remembered, [], "still debounced")
        self.clock.advance(250)                       # the ~250 ms debounce
        self.assertEqual(store.remembered, [(USER, None)],
                         "exactly one persist once the loop is done")
        self.assertEqual(len(fx.moves), 3, "the loop is over for good")

    def test_closing_mid_settle_stops_the_loop(self):
        """A window that closes mid-fight must not keep issuing moves."""
        fx = self.patch_x(FakeX([], then=CENTERED))
        stub = SettleStub(FakeStore())
        stub.apply_open_position()
        stub._closing = True
        self.clock.advance(10 * BUDGET_MS)
        self.assertEqual(len(fx.moves), 1)
        self.assertIsNone(getattr(stub, "_geom_settle", None))
        self.assertEqual(self.clock.pending(), 0)


class PlacementConstants(unittest.TestCase):
    """§1: the budget is the one the brief names — 8 attempts, ~150 ms,
    ~2 s — and the tolerances are 4 px position / 2 px size."""

    def test_values(self):
        self.assertEqual(ATTEMPTS, 8)
        self.assertEqual(INTERVAL, 150)
        self.assertEqual(BUDGET_MS, 2000)
        self.assertEqual(window.GEOM_SETTLE_ATTEMPTS, ATTEMPTS)
        self.assertEqual(window.GEOM_SETTLE_INTERVAL_MS, INTERVAL)
        self.assertAlmostEqual(window.GEOM_SETTLE_BUDGET_S, BUDGET_MS / 1000)
        self.assertEqual(window.GEOM_SETTLE_POS_TOL, 4)
        self.assertEqual(window.GEOM_SETTLE_SIZE_TOL, 2)

    def test_rect_close_helper(self):
        self.assertTrue(window._rect_close(TARGET, TARGET))
        self.assertTrue(window._rect_close(TARGET, (88, 34, 846, 968)))
        self.assertFalse(window._rect_close(TARGET, (89, 30, 844, 966)))
        self.assertFalse(window._rect_close(TARGET, (84, 30, 847, 966)))
        self.assertFalse(window._rect_close(TARGET, None))
        self.assertFalse(window._rect_close(None, None))
        self.assertFalse(window._rect_close("junk", TARGET))


if __name__ == "__main__":
    unittest.main()
