"""R30 (D1-D4): the chrome cannot be PINNED as a class — pure, injected clock.

The shipped bug (user report on 0.2.13): "the menu still does not retract
in fullscreen when the cursor is not moving".  R29 closed the measured path
(pointer resting in the bottom band) but the operator's follow-up run
showed the chrome STILL up: three more re-arm paths kept extending the
deadline (a chronic >TICK_GAP_MAX_MS tick-gap re-arming every tick, band
edges flapping without any motion, and a leaked `_menu_open` counter whose
decrement event never arrived), and there was no structural backstop.

Round 30 closes the CLASS (references/ROUND30-BRIEF.md):

  D1  a hard retraction CEILING: while playing and no menu is genuinely
      open, the chrome cannot stay visible more than 2 x timeout without a
      GENUINE input edge.  The anchor (_visible_since_ms) re-bases on real
      input (motion/click/leave/play-resume edges — menu-close and
      fullscreen route through motion), on a hidden->visible reveal, and —
      the brief-internal tension, documented in chrome.py — on the FORGIVEN
      (streak==1) discontinuity re-arm, which the UNCHANGED R13 tests
      require to keep a full countdown.  Unforgiven re-arms (streak >= 2)
      never extend it.  Hide reason: "ceiling" (a NEW, distinct string).
  D2  the discontinuity streak rule: gap > TICK_GAP_MAX_MS forgives ONE
      isolated stall, chronic stalling does NOT re-arm (gap = real idle).
  D3  the popover hold self-heals through an optional predicate
      (`Chrome.menu_present`): a counter whose popover vanished is reset.
      predicate None (unit/headless) keeps the old behaviour.
  D4  set_bottom_hover() edges reveal (entering) but touch NEITHER
      countdown anchor on either edge; only real motion re-arms.

Gates 1-4 below MUST FAIL on the unfixed tree (RED proven in the R30
report).  The NegativeControls class must pass BEFORE and AFTER.
"""
import _bootstrap  # noqa: F401  (sys.path for src/)
import unittest

from chrome import TICK_GAP_MAX_MS, Chrome, ChromeState


class Clock:
    """Advancing injected clock: the test sets .t; ChromeState reads it."""

    def __init__(self, t=0.0):
        self.t = float(t)

    def __call__(self):
        return self.t


def fixed(t=0.0):
    return ChromeState(timeout_ms=3000.0, clock=lambda: t)


def menu_chrome(state):
    """A REAL Chrome applying its REAL tick()/_set_chrome_visible() with no
    widgets: object.__new__ skips the GTK __init__, the two widget-facing
    seams (_fade_to animation, _blank_cursor device work) are shadowed by
    instance attributes.  The visibility bookkeeping under test
    (_bars_visible, the menu heal, the cursor-blank EDGE) is the shipped
    code."""
    c = object.__new__(Chrome)
    c.state = state
    c.on_activity = None
    c._menu_open = 0
    c.menu_present = None
    c._bars_visible = True
    c._reveal_widgets = []
    c._fade_ids = {}
    c._fade_target = {}
    c._osd_deadline = 0.0
    c._toast_deadline = 0.0
    c._fade_to = lambda w, target: None      # instance shadow
    c.blanks = []
    c._blank_cursor = c.blanks.append        # instance shadow (seam witness)
    return c


class TestChronicDiscontinuities(unittest.TestCase):
    """GATE 1 / D2: ticks spaced every 4000 ms (> TICK_GAP_MAX_MS) for 20 s
    of state time — a main loop stalling forever.  The FIRST big gap is
    forgiven (R13: an isolated freeze must not hide the chrome the instant
    it resumes); the SECOND consecutive big gap is the stall pattern, not a
    freeze — it counts as real elapsed idle and the chrome MUST end hidden.
    RED today: every big gap re-arms unconditionally, never hides."""

    def test_chronic_big_gaps_end_hidden(self):
        st = fixed()
        st.on_pointer_motion(0.0)
        st.tick(100.0)                                # baseline tick (R13
        self.assertEqual(st.tick(4100.0)[0], True)    # ONE stall forgiven
        self.assertEqual(st.tick(8100.0), (False, "idle"))  # streak >= 2
        t = 12100.0
        while t <= 20100.0:
            self.assertEqual(st.tick(t)[0], False, f"t={t}")
            t += 4000.0
        self.assertFalse(st.visible)                  # ends hidden

    def test_healthy_gaps_reset_the_streak(self):
        # The streak counts CONSECUTIVE big gaps (brief D2: "a normal gap
        # resets the streak"); the rule must not fire across healthy ticks.
        self.assertGreater(TICK_GAP_MAX_MS, 2000.0 - 1e-9)
        st = fixed()
        st.on_pointer_motion(0.0)
        st.tick(100.0)                                # baseline tick
        st.tick(4100.0)                               # streak 1 (forgiven)
        st.tick(4200.0)                               # healthy -> streak 0
        st.tick(8200.0)                               # streak 1 again
        self.assertTrue(st.visible)                   # second isolated stall
        for t in (8300.0, 10100.0):                   # isolated: last big gap
            st.tick(t)                                #   1900ms of idle left
        self.assertTrue(st.visible)


class TestBandFlapping(unittest.TestCase):
    """GATE 2 / D4: alternate set_bottom_hover(True/False) every 100 ms for
    10 s with NO motion.  The edges alone must not extend the countdown:
    the idle deadline from the one real motion expires and the chrome MUST
    end hidden.  RED today: every edge re-arms, hover pins forever."""

    def test_flapping_edges_end_hidden(self):
        st = fixed()
        st.on_pointer_motion(0.0)
        inside = True
        t = 100.0
        while t <= 10000.0:
            st.set_bottom_hover(inside, t)
            vis, _r = st.tick(t)
            if t <= 2900.0:
                self.assertTrue(vis, f"t={t}")        # countdown still runs
            inside = not inside
            t += 100.0
        self.assertFalse(st.visible)                  # ends hidden

    def test_entering_band_still_reveals_immediately(self):
        # D4 keeps the REVEAL: the entering edge shows without waiting for
        # a tick (negative control — passes before AND after).
        st = fixed()
        st.on_pointer_motion(0.0)
        self.assertEqual(st.tick(3100.0)[0], False)   # idle-hidden
        st.set_bottom_hover(True, 3200.0)
        self.assertTrue(st.visible)                   # revealed AT the edge
        self.assertEqual(st.reason, "hover")


class TestMenuLeak(unittest.TestCase):
    """GATE 3 / D3: `_menu_open` stuck at 1 while every popover reports
    get_visible() False (the decrement event never arrived — the shipped
    pin).  The hold must SELF-HEAL and the chrome MUST end hidden; the
    cursor-blank seam must fire on that hide edge.  RED today: the hold
    override keeps Chrome.tick() True forever."""

    def _drive(self, menu_present, until=5000.0):
        clk = Clock(0.0)
        st = ChromeState(timeout_ms=3000.0, clock=clk)
        st.on_pointer_motion(0.0)
        c = menu_chrome(st)
        c._menu_open = 1
        c.menu_present = menu_present
        last = True
        t = 100.0
        while t <= until:
            clk.t = t
            last = c.tick()
            t += 100.0
        return c, last

    def test_leaked_counter_heals_and_hides(self):
        c, last = self._drive(menu_present=lambda: False)
        self.assertEqual(c._menu_open, 0)             # the heal ran
        self.assertFalse(last)                        # Chrome.tick() ended hidden
        self.assertFalse(c.bars_visible)
        self.assertTrue(c.blanks and c.blanks[-1])    # cursor blanked at seam

    def test_genuine_menu_still_holds(self):
        # Negative control: a VISIBLE popover keeps the hold — passes
        # before and after; the heal must not eat real menus.
        c, last = self._drive(menu_present=lambda: True)
        self.assertEqual(c._menu_open, 1)
        self.assertTrue(last)
        self.assertTrue(c.bars_visible)
        self.assertEqual(c.blanks, [])

    def test_no_predicate_keeps_old_behaviour(self):
        # menu_present = None (unit/headless): behaviour UNCHANGED per brief
        # — no predicate, no heal, the counter still holds.  Passes both.
        c, last = self._drive(menu_present=None)
        self.assertEqual(c._menu_open, 1)
        self.assertTrue(last)


class TestCeiling(unittest.TestCase):
    """GATE 4 / D1: the backstop.  A pin path that is NONE of the above:
    small (healthy) tick gaps plus a stuck-fresh `_last_motion_ms` — a
    leak that re-arms the countdown directly without a reveal, motion or
    hover.  Only the CEILING can retract that chrome: it MUST hide at
    exactly 2 x timeout since the last genuine edge, reason "ceiling".
    This test fails if D1 is missing even with D2/D3/D4 present."""

    def test_ceiling_hides_at_two_timeouts(self):
        st = fixed()                                  # visible since t=0
        t = 500.0
        while t < 6000.0:
            st._last_motion_ms = t                    # leaked countdown re-arm
            vis, _r = st.tick(t)                      # (no motion, no reveal)
            self.assertTrue(vis, f"t={t}")            # idle can never fire
            t += 500.0
        st._last_motion_ms = 6000.0
        self.assertEqual(st.tick(6000.0), (False, "ceiling"))  # 2 * 3000
        st._last_motion_ms = 6500.0
        self.assertEqual(st.tick(6500.0)[0], False)   # stays hidden

    def test_genuine_input_resets_the_ceiling(self):
        st = fixed()
        st.on_pointer_motion(5000.0)                  # genuine edge at 5000
        t = 5500.0
        while t < 11000.0:
            st._last_motion_ms = t                    # pin continues
            vis, _r = st.tick(t)
            self.assertTrue(vis, f"t={t}")            # ceiling re-based to
            t += 500.0                                #   5000 -> survives 6000
        st._last_motion_ms = 11000.0
        self.assertEqual(st.tick(11000.0), (False, "ceiling"))  # 5000+2*3000


class NegativeControls(unittest.TestCase):
    """Brief gate 5 — regression guards that pass BEFORE and AFTER: an
    isolated 4000 ms stall re-arms and the chrome stays visible ~3 s past
    the resume, then hides; paused stays visible indefinitely; hidden
    chrome never resurrects on input-free ticks; the band enter still
    reveals immediately (also in TestBandFlapping above)."""

    def test_isolated_stall_visible_three_seconds_then_hides(self):
        st = fixed()
        st.on_pointer_motion(0.0)
        st.tick(500.0)
        self.assertEqual(st.tick(4500.0)[0], True)    # ONE 4000ms stall re-arms
        for t in (5000.0, 6000.0, 7400.0):
            self.assertEqual(st.tick(t)[0], True, f"t={t}")   # ~3 s past resume
        self.assertEqual(st.tick(7500.0), (False, "idle"))    # then hides

    def test_paused_survives_the_ceiling(self):
        st = fixed()
        st.on_playing_changed(False, 100.0)
        for t in (200.0, 5000.0, 60000.0):            # > 2 * timeout paused
            self.assertEqual(st.tick(t), (True, "not-playing"))

    def test_hidden_never_resurrects_without_input(self):
        st = fixed()
        st.on_pointer_motion(0.0)
        st.tick(3100.0)
        self.assertFalse(st.visible)
        t = 3600.0
        while t <= 30000.0:
            self.assertEqual(st.tick(t)[0], False, f"t={t}")
            t += 500.0


if __name__ == "__main__":
    unittest.main()
