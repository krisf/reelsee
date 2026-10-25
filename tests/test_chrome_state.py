"""ChromeState state machine (§4.6) — pure, injected clock, 14 cases.

Gate coverage (PLAN §8 P3): idle→hidden, motion→visible+re-arm, pause→visible
and stays, keyboard does not re-arm, bottom-hover keeps visible, clock jumps.
"""
import _bootstrap  # noqa: F401  (sys.path for src/)
import unittest

from chrome import BOTTOM_HOVER_PX, ChromeState


def fresh(timeout=3000.0, t0=1000.0):
    clock = lambda: t0  # noqa: E731 - fixed clock; ticks take explicit times
    st = ChromeState(timeout_ms=timeout, clock=clock)
    return st


class TestChromeState(unittest.TestCase):
    def test_idle_hides(self):
        st = fresh()
        st.on_pointer_motion(0.0)
        self.assertEqual(st.tick(2999.0), (True, "motion"))
        self.assertEqual(st.tick(3000.0)[0], False)   # exactly timeout hides

    def test_motion_shows_and_rearms(self):
        st = fresh()
        st.tick(5000.0)
        self.assertFalse(st.visible)
        st.on_pointer_motion(5100.0)
        self.assertTrue(st.visible)
        self.assertEqual(st.tick(7000.0)[0], True)    # re-armed at 5100
        self.assertEqual(st.tick(8101.0)[0], False)   # 3000ms after re-arm

    def test_click_and_wheel_like_motion(self):
        st = fresh()
        st.tick(5000.0)
        st.on_pointer_click(5100.0)
        self.assertTrue(st.visible)
        self.assertEqual(st.tick(8050.0)[0], True)

    def test_leave_rearms_once(self):
        st = fresh()
        st.on_pointer_leave(1000.0)
        self.assertEqual(st.tick(3999.0)[0], True)
        self.assertEqual(st.tick(4000.0)[0], False)

    def test_pause_visible_and_stays(self):
        st = fresh()
        st.on_playing_changed(False, 100.0)
        for t in (200.0, 5000.0, 100000.0):
            self.assertEqual(st.tick(t), (True, "not-playing"))

    def test_resume_hides_after_timeout(self):
        st = fresh()
        st.on_playing_changed(False, 100.0)
        st.tick(9999.0)                               # visible while paused
        st.on_playing_changed(True, 10000.0)          # fresh countdown
        self.assertEqual(st.tick(12999.0)[0], True)
        self.assertEqual(st.tick(13001.0)[0], False)

    def test_not_hidden_while_paused_even_if_idle(self):
        st = fresh()
        st.on_pointer_motion(0.0)
        st.on_playing_changed(False, 10.0)
        self.assertTrue(st.tick(999999.0)[0])

    def test_keyboard_does_not_rearm(self):
        st = fresh()
        st.on_pointer_motion(0.0)
        st.on_key(1000.0)
        st.on_key(2000.0)
        st.on_key(2900.0)
        self.assertEqual(st.tick(2999.0)[0], True)
        self.assertEqual(st.tick(3000.0)[0], False)   # deadline untouched

    def test_keyboard_does_not_reveal(self):
        st = fresh()
        st.tick(5000.0)
        self.assertFalse(st.visible)
        st.on_key(5100.0)
        self.assertFalse(st.visible)                  # keyboard keeps it hidden

    def test_bottom_hover_keeps_visible(self):
        st = fresh()
        st.on_pointer_motion(0.0)
        st.set_bottom_hover(True, 500.0)
        for t in (2000.0, 50000.0):
            self.assertEqual(st.tick(t), (True, "hover"))

    def test_hover_end_resumes_countdown_from_end(self):
        st = fresh()
        st.set_bottom_hover(True, 1000.0)
        st.tick(9000.0)                                # still visible: hover
        st.set_bottom_hover(False, 10000.0)
        self.assertEqual(st.tick(12999.0)[0], True)    # countdown from hover-end
        self.assertEqual(st.tick(13001.0)[0], False)

    def test_clock_jump_backward_rearms(self):
        st = fresh()
        st.on_pointer_motion(10000.0)
        # system clock/suspend resumes backwards
        self.assertEqual(st.tick(2000.0), (True, "clock-jump"))
        self.assertEqual(st.tick(4900.0)[0], True)
        self.assertEqual(st.tick(5100.0)[0], False)    # 3000ms after re-arm

    def test_clock_jump_forward_rearms(self):
        st = fresh()
        st.on_pointer_motion(0.0)
        self.assertEqual(st.tick(500000.0), (True, "clock-jump"))
        self.assertEqual(st.tick(502999.0)[0], True)
        self.assertEqual(st.tick(503100.0)[0], False)

    def test_default_timeout_is_3000ms(self):
        st = ChromeState(clock=lambda: 0.0)
        st.on_pointer_motion(0.0)
        self.assertTrue(st.tick(2999.0)[0])
        self.assertFalse(st.tick(3000.0)[0])
        self.assertEqual(st.timeout_ms, 3000.0)


class TestConstants(unittest.TestCase):
    def test_bottom_hover_band(self):
        self.assertEqual(BOTTOM_HOVER_PX, 80)


if __name__ == "__main__":
    unittest.main()
