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
      GENUINE input edge.  The anchor (_visible_since_ms) re-bases ONLY on
      real input (motion/click/leave/play-resume edges — menu-close and
      fullscreen route through motion) and on a hidden->visible reveal.
      R31: it must NOT re-base on a discontinuity re-arm.  R30 did, to keep
      R13's "full fresh timeout" promise, and that handed a PERIODICALLY
      stalling loop unlimited fresh ceilings (measured on the shipped
      0.2.14: pinned for 164 s with the pointer still — the user's second
      report).  R13's fresh timeout still applies to the IDLE countdown.
      Hide reason: "ceiling" (a NEW, distinct string).
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
        # R31: assert the RULE (the streak counter) rather than
        # un-hideability.  0.2.14 re-based the ceiling on every forgiven
        # stall, so this sequence pinned the chrome forever (measured: 164 s
        # of alternating 4000/100 ms ticks with the cursor still) — the old
        # `assertTrue(st.visible)` here encoded exactly that bug.
        self.assertGreater(TICK_GAP_MAX_MS, 2000.0 - 1e-9)
        st = fixed()
        st.on_pointer_motion(0.0)
        st.tick(100.0)                                # baseline tick
        st.tick(4100.0)                               # streak 1 (forgiven)
        self.assertEqual(st._gap_streak, 1)
        st.tick(4200.0)                               # healthy -> streak 0
        self.assertEqual(st._gap_streak, 0)
        st.tick(8200.0)                               # streak 1 again
        self.assertEqual(st._gap_streak, 1)
        self.assertFalse(st.visible)                  # R31: the ceiling is
        self.assertEqual(st.reason, "ceiling")        #     the final word


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
        # An isolated stall still re-arms the IDLE countdown, but R31: with
        # the stall past the midpoint of the ceiling window, the ceiling is
        # what ends visibility (last genuine edge = 0 -> ceiling at 6000).
        st = fixed()
        st.on_pointer_motion(0.0)
        st.tick(500.0)
        self.assertEqual(st.tick(4500.0)[0], True)    # ONE 4000ms stall re-arms
        self.assertEqual(st.tick(5000.0)[0], True)    # countdown still running
        self.assertEqual(st.tick(6000.0), (False, "ceiling"))  # backstop lands
        # Negative control for the OTHER half: a stall that leaves the fresh
        # countdown inside the window ends with the countdown, not the ceiling.
        # (A 2500 ms gap at t=3000 re-arms to a deadline of 6000, which lands
        # on or before the ceiling -- and the idle test is evaluated first.)
        st = fixed()
        st.on_pointer_motion(0.0)
        st.tick(500.0)
        self.assertEqual(st.tick(3000.0)[0], True)    # 2500ms stall re-arms
        self.assertEqual(st._last_motion_ms, 3000.0)  # re-armed
        self.assertEqual(st.tick(6000.0), (False, "idle"))     # fresh countdown

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


class TestPeriodicStalls(unittest.TestCase):
    """R31 GATE: a loop that stalls REPEATEDLY but not CONSECUTIVELY.

    0.2.14 forgave every ISOLATED big gap (streak == 1) and re-based the D1
    ceiling on it; one healthy tick between stalls resets the streak, so a
    periodically stalling loop was forgiven forever — chrome and cursor
    pinned with the pointer still.  That is the user's second report, on
    0.2.14 (which contained R30's ceiling).  Measured on the shipped state
    machine: alternating 4000/100 ms ticks stayed visible for 164 s; a
    2500/500 ms loop for 120 s.

    RED on 0.2.14 (never hides); GREEN once the ceiling is anchored on
    genuine input edges only and no re-arm can extend it.  The bound is the
    ceiling PLUS one tick gap: the hide lands on the first tick at or after
    6000 ms, so a stalling loop can delay detection by at most one gap.
    """

    SPAN = 180000.0

    def _run(self, pattern):
        st = fixed()
        st.on_pointer_motion(0.0)
        t = 100.0
        st.tick(t)                  # baseline: the first tick skips gap logic
        i = 0
        while t < self.SPAN:
            t += pattern[i % len(pattern)]
            i += 1
            vis, reason = st.tick(t)
            if not vis:
                return t, reason
        return None, None

    def _check(self, pattern, why=""):
        t, reason = self._run(pattern)
        self.assertIsNotNone(
            t, f"pattern {pattern} {why} never hid the chrome (PINNED)")
        self.assertEqual(reason, "ceiling", f"pattern {pattern} {why}")
        self.assertLessEqual(t, 6000.0 + max(pattern),
                             f"pattern {pattern} {why} hid late at {t} ms")

    def test_alternating_stall_ends_hidden(self):
        self._check([4000.0, 100.0], "alternating")

    def test_stall_with_two_healthy_ticks_ends_hidden(self):
        self._check([4000.0, 100.0, 100.0])

    def test_stall_with_four_healthy_ticks_ends_hidden(self):
        self._check([4000.0, 100.0, 100.0, 100.0, 100.0])

    def test_just_over_the_guard_ends_hidden(self):
        # 2500 ms is only 500 ms past TICK_GAP_MAX_MS: the hardest case for
        # a consecutiveness rule, because NOTHING here looks like a freeze.
        self._check([2500.0, 500.0], "just-over-guard")

    def test_healthy_loop_still_hides_by_idle(self):
        # Negative control: the ordinary path must keep its own reason and
        # its own deadline -- the ceiling must not take over a healthy loop.
        st = fixed()
        st.on_pointer_motion(0.0)
        t = 100.0
        while t < 5000.0:
            t += 100.0
            vis, reason = st.tick(t)
            if not vis:
                break
        self.assertFalse(st.visible)
        self.assertEqual(reason, "idle")
        self.assertEqual(t, 3000.0)


class TestStillPointerFlood(unittest.TestCase):
    """R32 GATE: a motion event at UNCHANGED coordinates is not input.

    Measured on the user's desktop with the instrumented build: in
    FULLSCREEN, XWayland re-delivers motion events continuously (~9-15/s)
    with the pointer physically still, and every one carried the identical
    position (1477.0, 801.5).  Each event called on_pointer_motion(), which
    re-armed the idle deadline — so the chrome and the cursor never
    retracted, and no backstop could help because the ceiling's anchor is
    only moved by what it believes are genuine input edges.  Windowed, the
    same session logged ZERO such events, which is why every Xvfb gate
    (free of Mutter/XWayland) stayed green through three rounds of this.

    RED on 0.2.15 (never hides under the flood); GREEN once a degenerate
    event is dropped at the input seam.  The moving-pointer and wake()
    controls must keep passing: this must not deaden real input.
    """

    def test_still_pointer_flood_ends_hidden(self):
        st = fixed()
        st.on_pointer_motion(0.0, pos=(1477.0, 801.5))
        t = 100.0
        while t <= 10000.0:
            st.on_pointer_motion(t, pos=(1477.0, 801.5))   # same coords
            st.tick(t)                                     # the app also ticks
            t += 100.0
        self.assertFalse(st.visible, "a still-pointer flood pinned the chrome")
        self.assertEqual(st.reason, "idle")

    def test_hidden_by_the_idle_deadline_not_late(self):
        st = fixed()
        st.on_pointer_motion(0.0, pos=(10.0, 10.0))
        t = 0.0
        while t < 5000.0:
            t += 100.0
            st.on_pointer_motion(t, pos=(10.0, 10.0))
            st.tick(t)
            if not st.visible:
                break
        self.assertFalse(st.visible)
        self.assertEqual(t, 3000.0)      # exactly the timeout, no drift

    def test_moving_pointer_still_rearms(self):
        # Negative control: REAL motion must keep the chrome up, or this
        # fix would deaden the player.
        st = fixed()
        st.on_pointer_motion(0.0, pos=(10.0, 10.0))
        t = 100.0
        while t <= 2900.0:
            st.on_pointer_motion(t, pos=(10.0 + t / 100.0, 10.0))
            st.tick(t)
            t += 100.0
        self.assertTrue(st.visible, "real pointer movement stopped re-arming")

    def test_wake_without_coordinates_still_rearms(self):
        # wake()/menu-close pass no position: they are deliberate edges.
        st = fixed()
        st.on_pointer_motion(0.0, pos=(5.0, 5.0))
        st.tick(2900.0)
        self.assertTrue(st.visible)
        st.on_pointer_motion(2900.0)          # pos=None -> a real edge
        self.assertEqual(st.tick(4000.0)[0], True)   # countdown restarted
        self.assertEqual(st.tick(5900.0)[0], False)

    def test_note_the_flood_does_not_reset_the_ceiling(self):
        # Even under a still-pointer flood the backstop anchor must stay
        # where the last REAL edge put it.
        st = fixed()
        st.on_pointer_motion(0.0, pos=(1.0, 2.0))
        t = 100.0
        while t <= 7000.0:
            st.on_pointer_motion(t, pos=(1.0, 2.0))
            st.tick(t)
            t += 100.0
        self.assertFalse(st.visible)
        self.assertIn(st.reason, ("idle", "ceiling"))
