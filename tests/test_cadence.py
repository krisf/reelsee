"""R16-A unit gates for the tick ladder (src/cadence.py — pure, no gi).

These pin the CONTRACT the brief asks for: 10 ms during fades/reveal and
pointer activity plus ~400 ms after a state change, 100 ms while visible and
stable, NO SOURCE while hidden, and the gap guard that keeps R13's
fast-forwarded idle-chrome clock inside ChromeState's tick-gap contract.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "src"))

import cadence as CD      # noqa: E402


class LadderTable(unittest.TestCase):
    """chrome_interval_ms(now, hold_until, animating, visible)."""

    def test_truth_table(self):
        rows = [
            # (now, hold_until, animating, visible, expected)
            (1000.0, 1400.0, True, True, 10),     # fade + hold
            (1000.0, 1400.0, True, False, 10),    # fading OUT: still ticking
            (1000.0, 1400.0, False, True, 10),    # inside the 400 ms hold
            (1800.0, 1400.0, True, False, 10),    # OSD/toast deadline live
            (1800.0, 1400.0, False, True, 100),   # visible, stable
            (1800.0, 1400.0, False, False, None),  # hidden: NO SOURCE
        ]
        for now, hold, anim, vis, want in rows:
            with self.subTest(now=now, hold=hold, anim=anim, vis=vis):
                self.assertEqual(
                    CD.chrome_interval_ms(now, hold, anim, vis), want)

    def test_hold_boundary_is_exclusive(self):
        # exactly at hold_until: the hold is over, visible -> slow
        self.assertEqual(CD.chrome_interval_ms(1400.0, 1400.0, False, True),
                         100)
        # one ms inside it: fast
        self.assertEqual(CD.chrome_interval_ms(1399.0, 1400.0, False, True),
                         10)

    def test_hidden_still_ticks_for_a_pending_transient(self):
        """A keyboard OSD while the bars are hidden (chrome.py §4.6) is
        retired ONLY by tick(), so suspending with one live would strand a
        visible overlay forever."""
        self.assertEqual(CD.chrome_interval_ms(1800.0, 1000.0, True, False),
                         10)

    def test_pin_restores_the_pre_round_cadence(self):
        """The baseline knob overrides EVERY rung, including suspension —
        that is what makes it the honest 'before' number in the gate."""
        for vis, anim in ((True, False), (False, False), (False, True)):
            self.assertEqual(
                CD.chrome_interval_ms(9e9, 0.0, anim, vis, pin_ms=10), 10)

    def test_pump_is_never_suspended(self):
        self.assertEqual(CD.pump_interval_ms(None, 0.0, 0.0), 100)
        self.assertEqual(CD.pump_interval_ms(5000.0, 0.0, 0.0), 100)
        self.assertEqual(CD.pump_interval_ms(None, 0.0, 0.0, pin_ms=10), 10)

    def test_pump_fast_while_pictures_arrive(self):
        self.assertEqual(CD.pump_interval_ms(33.0, 1000.0, 0.0), 10)
        self.assertEqual(CD.pump_interval_ms(None, 1000.0, 1400.0), 10)
        self.assertEqual(CD.pump_interval_ms(None, 1000.0, 0.0,
                                             engagement_armed=True), 10)
        # never saw a frame (no media / video suppressed) -> slow
        self.assertEqual(CD.pump_interval_ms(None, 1000.0, 0.0), 100)
        # stale frame (paused long ago) -> slow
        self.assertEqual(CD.pump_interval_ms(1000.0, 1000.0, 0.0), 100)


class GapGuard(unittest.TestCase):
    """The clamp that keeps a fast-forwarded ChromeState clock inside
    ChromeState.TICK_GAP_MAX_MS (R13's contract)."""

    def test_clock_rate(self):
        self.assertIsNone(CD.clock_rate(20.0, 1.0))      # too short to measure
        self.assertIsNone(CD.clock_rate(-5.0, 20.0))     # backwards clock
        self.assertIsNone(CD.clock_rate(None, 20.0))
        self.assertAlmostEqual(CD.clock_rate(20.0, 20.0), 1.0)
        self.assertAlmostEqual(CD.clock_rate(2000.0, 100.0), 20.0)
        # out-of-band samples (the garbage pair a harness produces when it
        # swaps the clock mid-run) are ignored, not clamped into the ladder
        self.assertIsNone(CD.clock_rate(1e9, 10.0))
        self.assertAlmostEqual(CD.clock_rate(19900.0, 100.0), 199.0)
        self.assertIsNone(CD.clock_rate(2000.0 * 199, 100.0))  # reject band

    def test_inert_for_a_wall_clock(self):
        self.assertEqual(CD.clamp_gap(100, 1.0), 100)
        self.assertEqual(CD.clamp_gap(100, None), 100)
        self.assertIsNone(CD.clamp_gap(None, 20.0))

    def test_fast_forwarded_clock_scales_the_ladder(self):
        """20x (scripts/smoke_idlechrome.py) must land back on ~the pre-R16
        cadence: 250 sim ms / 20 = 12 ms, so the slow rung becomes 12 ms and
        the fast rung stays 10 ms.  A 100 ms rung at 20x was MEASURED firing
        100-150 ms apart on the starved software-decode loop -> 2000-3000
        simulated ms -> the R13 re-arm ate the countdown and the gate's
        chrome never hid."""
        self.assertEqual(CD.clamp_gap(100, 20.0), 12)
        self.assertEqual(CD.clamp_gap(10, 20.0), 10)     # 200 ms: fine
        self.assertEqual(CD.clamp_gap(100, 1000.0), 1)
        self.assertEqual(CD.chrome_interval_ms(0.0, 0.0, False, True,
                                               rate=20.0), 12)
        self.assertEqual(CD.chrome_interval_ms(0.0, 9e9, False, True,
                                               rate=20.0), 10)
        # suspension is NOT a cadence change: hidden is hidden at any rate
        self.assertIsNone(CD.chrome_interval_ms(0.0, 0.0, False, False,
                                                rate=20.0))

    def test_ladder_never_exceeds_the_gap_guard(self):
        """THE idlechrome non-regression property: whatever rate the state
        clock runs at, the gap ChromeState sees stays under its threshold
        (chrome.TICK_GAP_MAX_MS — the real constant, not a copy) with
        headroom."""
        from chrome import TICK_GAP_MAX_MS
        self.assertLess(CD.GAP_GUARD_SIM_MS, TICK_GAP_MAX_MS)
        for rate in (1.0, 2.0, 4.0, 10.0, 19.7, 20.0, 50.0, 1000.0):
            iv = CD.chrome_interval_ms(0.0, 0.0, False, True, rate=rate)
            self.assertLess(iv * rate, TICK_GAP_MAX_MS, f"rate={rate}")
            iv = CD.chrome_interval_ms(0.0, 9e9, False, True, rate=rate)
            self.assertLess(iv * rate, TICK_GAP_MAX_MS,
                            f"fast rung rate={rate}")


class PinFromEnv(unittest.TestCase):
    def test_parses_and_ignores_junk(self):
        self.assertEqual(CD.pin_from_env({"REELSEE_TICK_PIN_MS": "10"}), 10)
        self.assertEqual(CD.pin_from_env({"REELSEE_TICK_PIN_MS": " 10.0 "}),
                         10)
        for junk in ("", "0", "-5", "auto", None):
            with self.subTest(v=junk):
                self.assertIsNone(CD.pin_from_env(
                    {"REELSEE_TICK_PIN_MS": junk}))
        self.assertIsNone(CD.pin_from_env({}))


class LadderConstants(unittest.TestCase):
    def test_ladder_values(self):
        self.assertEqual(CD.TICK_FAST_MS, 10)
        self.assertEqual(CD.TICK_SLOW_MS, 100)
        self.assertGreaterEqual(CD.TICK_HOLD_MS, 400.0)
        self.assertLess(CD.GAP_GUARD_SIM_MS, 2000.0)


if __name__ == "__main__":
    unittest.main()
