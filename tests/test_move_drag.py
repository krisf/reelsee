"""R17-C: the app-owned window move starts at the drag THRESHOLD, never on
press.

Why this exists: `Gtk.GestureDrag::drag-begin` is emitted on the PRESS on
this GTK build (measured in the round-17 investigation), so a handler that
called `Gdk.Toplevel.begin_move` there claimed every press+release in the
video area and click-to-pause stopped working altogether.  The unit tests
below pin the decision logic without a display; the live behaviour is gated
by scripts/smoke_notitlebar.py (drag_begin_alone_does_not_move),
scripts/smoke_headerdrag.py (a1b) and scripts/smoke_rclickwm.py
(left_click_resumes).
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "src"))

from window import MOVE_DRAG_MIN_PX, ReelseeWindow, _drag_threshold_px  # noqa: E402


class _Gesture:
    """Enough of a Gtk.GestureDrag for the three handlers."""

    def __init__(self, device=None, button=1, when=1234):
        self.device = device
        self.button = button
        self.when = when

    def get_current_event_device(self):
        return self.device

    def get_current_button(self):
        return self.button

    def get_current_event_time(self):
        return self.when

    def set_state(self, _state):
        pass


class _Win:
    """Stand-in for ReelseeWindow: the threshold logic under test needs
    nothing but the pending map and a recordable _begin_move."""

    _closing = False

    def __init__(self, threshold=8.0):
        self._move_pending = {}
        self._move_threshold = threshold
        self.moves = []

    def _begin_move(self, gesture, pend, which):
        self.moves.append((which, pend["x"], pend["y"]))


class Threshold(unittest.TestCase):
    def test_floor_without_a_display(self):
        """Headless (no default Gtk.Settings) the helper must still answer
        with a sane distance, never 0 — 0 would mean every click moves."""
        px = _drag_threshold_px()
        self.assertGreaterEqual(px, MOVE_DRAG_MIN_PX)
        self.assertGreaterEqual(px, 4.0)

    def test_floor_applies_to_a_stupid_setting(self):
        import window as W

        class _S:
            def __init__(self, v):
                self.v = v

            def get_property(self, _p):
                return self.v

        real = W.Gtk.Settings.get_default
        for stored, want in ((2, MOVE_DRAG_MIN_PX), (0, 8.0), (-5, 8.0),
                             (12, 12.0)):
            W.Gtk.Settings.get_default = staticmethod(lambda v=stored: _S(v))
            try:
                self.assertEqual(W._drag_threshold_px(), want, str(stored))
            finally:
                W.Gtk.Settings.get_default = real


class MoveStartsAtTheThreshold(unittest.TestCase):
    def test_drag_begin_alone_never_moves(self):
        """THE regression: press alone (drag-begin) must not start a move."""
        w = _Win()
        ReelseeWindow._on_move_drag_begin(w, _Gesture(), 10.0, 20.0, "video")
        self.assertEqual(w.moves, [])
        pend = w._move_pending["video"]
        self.assertEqual((pend["x"], pend["y"]), (10.0, 20.0))

    def test_motion_below_the_threshold_does_not_move(self):
        w = _Win(threshold=8.0)
        g = _Gesture()
        ReelseeWindow._on_move_drag_begin(w, g, 10.0, 20.0, "video")
        ReelseeWindow._on_move_drag_update(w, g, 3.0, 3.0, "video")
        self.assertEqual(w.moves, [])
        self.assertIn("video", w._move_pending)

    def test_crossing_the_threshold_moves_once(self):
        w = _Win(threshold=8.0)
        g = _Gesture()
        ReelseeWindow._on_move_drag_begin(w, g, 10.0, 20.0, "video")
        ReelseeWindow._on_move_drag_update(w, g, 5.0, 5.0, "video")
        ReelseeWindow._on_move_drag_update(w, g, 40.0, 30.0, "video")
        ReelseeWindow._on_move_drag_update(w, g, 90.0, 90.0, "video")
        self.assertEqual(w.moves, [("video", 10.0, 20.0)])
        self.assertNotIn("video", w._move_pending)

    def test_diagonal_travel_counts(self):
        """Distance is Euclidean: 6+6 px at threshold 8 must move."""
        w = _Win(threshold=8.0)
        g = _Gesture()
        ReelseeWindow._on_move_drag_begin(w, g, 0.0, 0.0, "header")
        ReelseeWindow._on_move_drag_update(w, g, 6.0, 6.0, "header")
        self.assertEqual(w.moves, [("header", 0.0, 0.0)])

    def test_release_before_the_threshold_forgets_the_press(self):
        w = _Win(threshold=8.0)
        g = _Gesture()
        ReelseeWindow._on_move_drag_begin(w, g, 1.0, 2.0, "video")
        ReelseeWindow._on_move_drag_end(w, g, 1.0, 1.0)
        ReelseeWindow._on_move_drag_update(w, g, 50.0, 50.0, "video")
        self.assertEqual(w.moves, [])
        self.assertEqual(w._move_pending, {})

    def test_a_closed_window_records_nothing(self):
        w = _Win()
        w._closing = True
        ReelseeWindow._on_move_drag_begin(w, _Gesture(), 1.0, 1.0, "video")
        self.assertEqual(w._move_pending, {})


if __name__ == "__main__":
    unittest.main()
