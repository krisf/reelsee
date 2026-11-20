"""R29 (D2): a move-drag must abort the live R21 settle loop INSTANTLY
(references/ROUND29-BRIEF.md §B2 unit half).

The measured defect: `_geom_settle_start` re-issues `x11pos.set_rect` every
150 ms for a 2.0 s budget at app open (and again after fullscreen->windowed),
and NOTHING in the drag path aborted it — the app issued X moves INTO the
user's live WM move grab, so the title bar was not draggable for "a couple
seconds".  Its only user-yield was the observed-rect `user-grab` test, which
the loop's own set_rect masks (it reads back its own target).

The fix under test: `_on_move_drag_begin` (after the `_closing` guard,
before recording `_move_pending`) and `_begin_move` (its top, second seam)
abort via `_geom_settle_finish("user-drag", self._geom_live_rect())`.

Harness: the tests/test_window_placement_r21.py shape — the REAL window
methods bound onto a bare stub, FakeClock for GLib, x11pos patched; no
display, no X, no sleep.  The out-of-process / real-loop half is
scripts/smoke_drag_settle_r29.py.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

import _bootstrap  # noqa: F401  (sys.path + writable TMP_ROOT)
import window
from window import ReelseeWindow

TARGET = (100, 100, 1280, 720)          # where we want the window
WM_IGNORED = (500, 300, 1280, 720)      # a WM that ignores every move
USER = (1100, 640, 1280, 720)           # grabbed mid-settle and dragged


def _bind(name):
    return getattr(ReelseeWindow, name, lambda self, *a, **k: False)


class FakeClock:
    """GLib.timeout_add / source_remove that only move when the test says
    so (R21 harness): the abort must REMOVE the pending timer, and pumping
    the clock afterwards must fire nothing."""

    def __init__(self):
        self.now = 0
        self.removed = []
        self._next = 1
        self._timers = {}

    def timeout_add(self, ms, fn, *args):
        tid = self._next
        self._next += 1
        self._timers[tid] = (self.now + int(ms), int(ms), fn)
        return tid

    def source_remove(self, tid):
        self.removed.append(tid)
        return self._timers.pop(tid, None) is not None

    def advance(self, ms):
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
        return fired


class FakeX:
    """x11pos stand-in: `script` replays the rects the compositor applied,
    `then` answers afterwards (WM_IGNORED = ignores every move)."""

    def __init__(self, live=(), then=None):
        self.script = list(live)
        self.then = then
        self.moves = []

    def set_rect(self, win, rect):
        self.moves.append(tuple(rect))
        return True

    def get_rect(self, win):
        return self.script.pop(0) if self.script else self.then


class _Gesture:
    """A live-enough drag sequence: reports a device, so _begin_move never
    reaches the seat fallback (no display in a unit test)."""

    def get_current_event_device(self):
        return SimpleNamespace(name="pointer")

    def get_current_button(self):
        return 1

    def get_current_event_time(self):
        return 1234

    def set_state(self, _state):
        pass


class DragSettleStub:
    """The REAL placement + move-drag methods on a bare object."""
    _geom_settle_start = _bind("_geom_settle_start")
    _geom_settle_step = _bind("_geom_settle_step")
    _geom_settle_tick = _bind("_geom_settle_tick")
    _geom_settle_schedule = _bind("_geom_settle_schedule")
    _geom_settle_finish = _bind("_geom_settle_finish")
    _geom_settle_stop = _bind("_geom_settle_stop")
    _geom_settle_disarm = _bind("_geom_settle_disarm")
    _geom_settle_aborted = _bind("_geom_settle_aborted")
    _geom_live_rect = _bind("_geom_live_rect")
    _geom_windowed = _bind("_geom_windowed")
    _geom_arm_guard = _bind("_geom_arm_guard")
    _on_move_drag_begin = _bind("_on_move_drag_begin")
    _on_move_drag_update = _bind("_on_move_drag_update")
    _on_move_drag_end = _bind("_on_move_drag_end")
    _begin_move = _bind("_begin_move")

    def __init__(self, target=TARGET, surface=None):
        self._geom_open = None
        self._geom_rect = tuple(target)
        self._geom_settle = None
        self._geom_settle_moves = 0
        self._geom_settle_reason = None
        self._move_pending = {}
        self._move_threshold = 8.0
        self._move_initiated = 0
        self._closing = False
        self._fs = False
        self._aspect_size = None
        self._aspect_applying = False
        self._geom_guard_id = None
        self._geom_timer = None
        self._geom_id = None
        self._mapped = True
        self._surface = surface

    def get_mapped(self):
        return self._mapped

    def is_fullscreen(self):
        return self._fs

    def is_maximized(self):
        return False

    def get_width(self):
        return 0          # ≤ 1 ⇒ the size-only live-rect fallback declines

    def get_height(self):
        return 0

    def get_surface(self):
        return self._surface

    def get_display(self):
        raise AssertionError("no display in a unit test")


class DragAbortsSettle(unittest.TestCase):
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

    def armed(self, fx, target=TARGET):
        """Arm the loop against a WM that ignores us: it provably STAYS
        armed (the sanity leg), so any stop below can only come from us."""
        stub = DragSettleStub(target=target)
        self.assertTrue(stub._geom_settle_start(target))
        self.assertIsNotNone(stub._geom_settle)
        self.assertEqual(fx.moves, [target])       # the arm already moved once
        return stub

    # ---------------- the fix: abort at the two seams ------------------
    def test_drag_begin_aborts_live_loop_with_user_drag(self):
        fx = self.patch_x(FakeX(then=WM_IGNORED))
        stub = self.armed(fx)
        stub._on_move_drag_begin(_Gesture(), 10.0, 20.0, "top")
        self.assertIsNone(stub._geom_settle)                  # loop gone
        self.assertEqual(stub._geom_settle_reason, "user-drag")
        self.assertEqual(stub._geom_rect, tuple(WM_IGNORED))  # observed truth
        self.assertIn("top", stub._move_pending)              # press recorded
        # the pending 150 ms timer is DISARMED, and pumping the clock
        # past several intervals issues NOTHING:
        self.assertEqual(len(self.clock.removed), 1)
        self.clock.advance(150 * 4)
        self.assertEqual(fx.moves, [TARGET])

    def test_press_without_travel_still_aborts(self):
        # drag-begin fires on the PRESS: a press that never crosses the
        # threshold aborts the loop too (the user touched the handle).
        fx = self.patch_x(FakeX(then=WM_IGNORED))
        stub = self.armed(fx)
        stub._on_move_drag_begin(_Gesture(), 1.0, 2.0, "header")
        stub._on_move_drag_end(_Gesture())          # released without travel
        self.assertIsNone(stub._geom_settle)
        self.assertEqual(stub._geom_settle_reason, "user-drag")
        self.assertEqual(fx.moves, [TARGET])        # never moved again

    def test_begin_move_seam_aborts_even_when_the_move_is_refused(self):
        # Second seam, bare: get_surface() is None, so the real _begin_move
        # REFUSES the move — yet the loop was already yielded, before any
        # refusal path could return first.
        fx = self.patch_x(FakeX(then=WM_IGNORED))
        stub = self.armed(fx)
        ReelseeWindow._begin_move(stub, _Gesture(),
                                  {"x": 5.0, "y": 5.0, "thr": 8.0,
                                   "dev": object(), "button": 1,
                                   "time": 1234}, "video")
        self.assertIsNone(stub._geom_settle)
        self.assertEqual(stub._geom_settle_reason, "user-drag")
        self.assertEqual(fx.moves, [TARGET])

    def test_no_live_loop_is_a_clean_no_op(self):
        # The audit shape: drag handlers on a window with NO settle loop
        # must not touch the reason and must not raise.
        fx = self.patch_x(FakeX(then=WM_IGNORED))
        stub = DragSettleStub(target=TARGET)
        stub._on_move_drag_begin(_Gesture(), 3.0, 4.0, "top")
        self.assertIsNone(stub._geom_settle)
        self.assertIsNone(stub._geom_settle_reason)
        self.assertEqual(fx.moves, [])

    def test_close_guard_wins_first(self):
        # The abort sits AFTER the _closing guard: a closing window records
        # nothing and runs nothing (the close path stops the loop itself).
        fx = self.patch_x(FakeX(then=WM_IGNORED))
        stub = self.armed(fx)
        stub._closing = True
        stub._on_move_drag_begin(_Gesture(), 1.0, 1.0, "video")
        self.assertEqual(stub._move_pending, {})
        self.assertIsNotNone(stub._geom_settle)   # untouched by this call

    # ---------------- what must NOT change ----------------------------
    def test_user_grab_path_is_untouched(self):
        # R21 regression: observed-rect change still ends the loop with
        # reason "user-grab" (this is the seam the fix must not disturb).
        fx = self.patch_x(FakeX([WM_IGNORED, USER], then=USER))
        stub = DragSettleStub(target=TARGET)
        stub._geom_settle_start(TARGET)
        self.assertIsNotNone(stub._geom_settle)    # seen stable and wrong
        self.clock.advance(150)                    # one interval later: moved
        self.assertIsNone(stub._geom_settle)
        self.assertEqual(stub._geom_settle_reason, "user-grab")
        self.assertEqual(stub._geom_rect, tuple(USER))

    def test_threshold_crossing_still_starts_the_move(self):
        # The R17-C move itself must be unharmed: press → travel → real
        # begin_move on the toplevel, loop yielded exactly once.
        fx = self.patch_x(FakeX(then=WM_IGNORED))
        toplevel = mock.MagicMock(spec=window.Gdk.Toplevel)
        stub = self.armed(fx)
        g = _Gesture()
        stub._on_move_drag_begin(g, 40.0, 60.0, "top")
        stub._surface = toplevel                    # present only at the seam
        stub._on_move_drag_update(g, 30.0, 30.0, "top")   # past the 8 px thr
        toplevel.begin_move.assert_called_once()
        args = toplevel.begin_move.call_args[0]
        self.assertEqual((args[2], args[3], args[4]), (40.0, 60.0, 1234))
        self.assertIsNone(stub._geom_settle)
        self.assertEqual(stub._geom_settle_reason, "user-drag")
        self.assertEqual(fx.moves, [TARGET])        # no extra move anywhere


if __name__ == "__main__":
    unittest.main()
