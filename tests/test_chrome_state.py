"""ChromeState state machine (§4.6) — pure, injected clock, 18 cases.

Gate coverage (PLAN §8 P3): idle→hidden, motion→visible+re-arm, pause→visible
and stays, keyboard does not re-arm, bottom-hover keeps visible.

R13 clock-discontinuity contract (replaces the old "dt > timeout *
JUMP_FACTOR force-shows a clock-jump"): a discontinuity is measured from
the gap between SUCCESSIVE TICKS — negative, or larger than
TICK_GAP_MAX_MS (2000).  It RE-ARMS the idle countdown from the jump
instant WITHOUT touching visibility: a hidden chrome stays hidden through
a suspend/stall (idleness is NOT a jump — that was the endless 12 s
resurrection cycle), a visible one keeps its full countdown.  Direct
consequence for these tests: samples farther apart than TICK_GAP_MAX_MS
ARE a discontinuity by definition (a healthy app ticks every 10 ms), so
countdown assertions use the stream() helper (healthy 10 ms tick stream)
or samples closer than the threshold.
"""
import _bootstrap  # noqa: F401  (sys.path for src/)
import unittest

from chrome import BOTTOM_HOVER_PX, TICK_GAP_MAX_MS, ChromeState


def fresh(timeout=3000.0, t0=1000.0):
    clock = lambda: t0  # noqa: E731 - fixed clock; ticks take explicit times
    st = ChromeState(timeout_ms=timeout, clock=clock)
    return st


def stream(st, t_from, t_to, step=10.0):
    """A HEALTHY tick stream: tick() every `step` ms from t_from to t_to
    inclusive, yielding (t, visible, reason).  Gaps stay far below
    TICK_GAP_MAX_MS, so nothing here is ever a discontinuity."""
    t = t_from
    while t <= t_to + 1e-9:
        vis, r = st.tick(t)
        yield (t, vis, r)
        t += step


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
        self.assertEqual(st.tick(6100.0)[0], True)    # healthy 1000ms gap
        self.assertEqual(st.tick(8050.0)[0], True)    # 2950ms after click

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
        for _t, vis, _r in stream(st, 9000.0, 9990.0):
            self.assertTrue(vis)                      # visible while paused
        st.on_playing_changed(True, 10000.0)          # fresh countdown
        self.assertEqual(st.tick(11500.0)[0], True)   # 1510ms ticks: healthy
        self.assertEqual(st.tick(12999.0)[0], True)   # 2999ms after resume
        self.assertEqual(st.tick(13001.0)[0], False)  # 3000ms after resume

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
        for _t, vis, _r in stream(st, 9000.0, 9990.0):
            self.assertTrue(vis)                      # still visible: hover
        st.set_bottom_hover(False, 10000.0)
        self.assertEqual(st.tick(11500.0)[0], True)   # 1510ms ticks: healthy
        self.assertEqual(st.tick(12999.0)[0], True)   # countdown from end
        self.assertEqual(st.tick(13001.0)[0], False)  # 3000ms after hover-end

    # ------------------------------------------------- R13 discontinuities
    def test_stall_while_visible_rearms_countdown(self):
        # Corrected from test_clock_jump_forward_rearms (R13, sanctioned):
        # a genuine single-tick 30 s gap while VISIBLE re-arms from the
        # jump instant WITHOUT changing visibility or reason — the old
        # contract forced a (True, "clock-jump") reveal.
        st = fresh()
        st.on_pointer_motion(0.0)
        self.assertEqual(st.tick(10.0), (True, "motion"))   # healthy tick
        self.assertEqual(st.tick(30010.0), (True, "motion"))  # 30 s stall
        for t, vis, _r in stream(st, 30020.0, 33000.0):
            self.assertTrue(vis, f"t={t}")            # full countdown left
        self.assertEqual(st.tick(33010.0)[0], False)  # 3000ms after STALL

    def test_stall_while_hidden_stays_hidden(self):
        # THE R13 REGRESSION: plain idleness crossed the old
        # timeout*JUMP_FACTOR threshold and was read as a forward jump,
        # resurrecting hidden chrome on an endless 12 s cycle.  A real
        # 30 s stall while HIDDEN re-arms but must NOT show.
        st = fresh()
        st.on_pointer_motion(0.0)
        for _t, vis, _r in stream(st, 10.0, 2990.0):
            self.assertTrue(vis)
        self.assertEqual(st.tick(3000.0)[0], False)   # normal idle hide
        self.assertEqual(st.tick(33010.0), (False, "idle"))  # no resurrect
        for t, vis, _r in stream(st, 33020.0, 63020.0):
            self.assertFalse(vis, f"t={t}")           # stays buried 30 s
        st.on_pointer_motion(63100.0)                 # input still rules
        self.assertTrue(st.visible)

    def test_backward_clock_rearms_countdown(self):
        # Corrected from test_clock_jump_backward_rearms (R13, sanctioned):
        # backwards clock while VISIBLE re-arms without changing
        # visibility; countdown resumes from the jump instant.
        st = fresh()
        st.on_pointer_motion(10000.0)
        self.assertEqual(st.tick(10010.0), (True, "motion"))  # healthy tick
        self.assertEqual(st.tick(2000.0), (True, "motion"))   # backwards
        for t, vis, _r in stream(st, 2010.0, 4990.0):
            self.assertTrue(vis, f"t={t}")
        self.assertEqual(st.tick(5000.0)[0], False)   # 3000ms after JUMP

    def test_backward_clock_while_hidden_stays_hidden(self):
        st = fresh()
        st.on_pointer_motion(0.0)
        for _t, vis, _r in stream(st, 10.0, 2990.0):
            self.assertTrue(vis)
        self.assertEqual(st.tick(3000.0)[0], False)
        self.assertEqual(st.tick(1500.0), (False, "idle"))   # no resurrect
        for t, vis, _r in stream(st, 1510.0, 10510.0):
            self.assertFalse(vis, f"t={t}")

    def test_idle_stability_sixty_seconds(self):
        # R13 GATE: 60 s of healthy 10 ms ticks after the hide produce
        # exactly ONE visibility transition and hidden at EVERY sample.
        st = fresh()
        st.on_pointer_motion(1000.0)
        transitions = []
        prev = st.visible
        for t, vis, _r in stream(st, 1010.0, 64010.0):
            if vis != prev:
                transitions.append((t, vis))
                prev = vis
            if t >= 4000.0:
                self.assertFalse(vis, f"t={t}")
        self.assertEqual(transitions, [(4000.0, False)])
        self.assertEqual(st.reason, "idle")

    def test_default_timeout_is_3000ms(self):
        st = ChromeState(clock=lambda: 0.0)
        st.on_pointer_motion(0.0)
        self.assertTrue(st.tick(2999.0)[0])
        self.assertFalse(st.tick(3000.0)[0])
        self.assertEqual(st.timeout_ms, 3000.0)


class TestConstants(unittest.TestCase):
    def test_bottom_hover_band(self):
        self.assertEqual(BOTTOM_HOVER_PX, 80)

    def test_tick_gap_threshold(self):
        # R13: the threshold is named for the TICK GAP (not a multiple of
        # the idle timeout) and sits at ~2000 ms: an order of magnitude
        # above the 10 ms production tick, comfortably below the 3000 ms
        # timeout so a stall is re-armed before it could ever hide.
        self.assertEqual(TICK_GAP_MAX_MS, 2000.0)


if __name__ == "__main__":
    unittest.main()
