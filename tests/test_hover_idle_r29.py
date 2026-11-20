"""R29 (D1): the OPERATOR-VERIFIED 8-row truth table for the hover/idle
interaction, pinned verbatim from references/ROUND29-BRIEF.md.

The measured defect: `ChromeState.tick()`'s `elif self.bottom_hover:
_show("hover")` branch sat ABOVE the idle test and short-circuited it — a
pointer RESTING in the bottom 80 px band (exactly where the cursor parks in
fullscreen) pinned the chrome, and downstream the cursor, visible forever
(operator instrumented run: `DIAGCHROME vis=True reason=hover playing=True
hover=True idle_ms=13822 fs=True` with idle_ms climbing 5454 → 13822 and
`DIAGCURSOR` never firing; the centre-parked control hid with
`vis=False reason=idle playing=True hover=False idle_ms=3000 fs=True`).

The fix: idle expiry is tested FIRST and wins over hover; hover only holds
the chrome open while the countdown is still running, and entering the band
still reveals + re-arms.

Table conditions (verbatim): playing=True, timeout_ms=3000, no menu open;
"elapsed" = time since the last pointer motion / hover-edge re-arm.

| pointer position          | elapsed ms | visible | reason       |
|---------------------------|-----------|---------|--------------|
| inside band, just entered | 0         | True    | hover        |
| inside band, stationary   | 2999      | True    | hover        |
| inside band, stationary   | 3000      | False   | idle         |
| inside band, stationary   | 6000      | False   | idle (STAYS) |
| inside band, then moved   | 0         | True    | motion       |
| outside band, stationary  | 2999      | True    | (unchanged)  |
| outside band, stationary  | 3000      | False   | idle         |
| anywhere, playing=False   | any       | True    | not-playing  |

Every tick stream uses healthy gaps (≤ TICK_GAP_MAX_MS = 2000) so the R13
discontinuity re-arm never interferes: these rows measure the decision,
not the clock guard.
"""
import _bootstrap  # noqa: F401  (sys.path for src/)
import unittest

from chrome import ChromeState

TIMEOUT = 3000.0


def fresh():
    """playing=True, timeout 3000, visible at start — the table's base."""
    return ChromeState(timeout_ms=TIMEOUT, clock=lambda: 0.0)


def ticks(st, t_from, t_to, step=1000.0):
    """tick() every `step` ms (healthy: step < TICK_GAP_MAX_MS), yielding
    (t, visible, reason)."""
    t = t_from
    while t <= t_to + 1e-9:
        vis, reason = st.tick(t)
        yield (t, vis, reason)
        t += step


class HoverIdleTruthTable(unittest.TestCase):
    # row 1: inside band, just entered, elapsed 0  ⇒ (True, "hover")
    def test_row1_just_entered_band_hovers(self):
        st = fresh()
        st.on_pointer_motion(0.0)
        st.set_bottom_hover(True, 0.0)          # travel INTO the band
        self.assertEqual(st.tick(0.0), (True, "hover"))

    # row 2: inside band, stationary, elapsed 2999 ⇒ (True, "hover")
    def test_row2_stationary_in_band_2999_holds(self):
        st = fresh()
        st.on_pointer_motion(0.0)
        st.set_bottom_hover(True, 0.0)
        for t, vis, reason in ticks(st, 999.0, 2999.0):   # 999, 1999, 2999
            self.assertEqual((vis, reason), (True, "hover"), f"t={t}")

    # row 3: inside band, stationary, elapsed 3000 ⇒ (False, "idle")
    # THE pre-fix RED row: pre-fix this returned (True, "hover").
    def test_row3_stationary_in_band_3000_hides(self):
        st = fresh()
        st.on_pointer_motion(0.0)
        st.set_bottom_hover(True, 0.0)
        list(ticks(st, 1000.0, 2000.0))
        self.assertEqual(st.tick(3000.0), (False, "idle"))

    # row 4: inside band, stationary, elapsed 6000 ⇒ (False, "idle") STAYS.
    # Load-bearing: the chrome must NOT oscillate back up with the pointer
    # still in the band — a hide/show loop would be a crossing-event
    # feedback path, not something to paper over with a longer timeout.
    def test_row4_stays_hidden_in_band_6000(self):
        st = fresh()
        st.on_pointer_motion(0.0)
        st.set_bottom_hover(True, 0.0)
        list(ticks(st, 1000.0, 3000.0))         # hides at 3000 IN the band
        self.assertFalse(st.visible)
        for t, vis, reason in ticks(st, 4000.0, 6000.0):
            self.assertEqual((vis, reason), (False, "idle"), f"t={t}")

    # row 5: inside band, then moved, elapsed 0 ⇒ (True, "motion")
    def test_row5_motion_in_band_reshows_as_motion(self):
        st = fresh()
        st.on_pointer_motion(0.0)
        st.set_bottom_hover(True, 0.0)
        list(ticks(st, 1000.0, 6000.0))         # hidden, pointer still in band
        st.on_pointer_motion(6100.0)            # a REAL move inside the band
        # The REVEAL output at the motion edge (the table's snapshot):
        self.assertEqual((st.visible, st.reason), (True, "motion"))
        # …the next healthy tick legitimately re-labels the in-band hold as
        # "hover" (pre-fix and post-fix alike), and the fresh countdown
        # re-armed: hides again 3000 ms after the MOVE, not the entry.
        self.assertEqual(st.tick(6110.0), (True, "hover"))
        for t, vis, reason in ticks(st, 7110.0, 9099.0, step=663.0):
            self.assertEqual((vis, reason), (True, "hover"), f"t={t}")
        self.assertEqual(st.tick(9100.0), (False, "idle"))   # 3000 after MOVE

    # row 6: outside band, stationary, elapsed 2999 ⇒ (True, unchanged)
    def test_row6_outside_band_2999_unchanged(self):
        st = fresh()
        st.on_pointer_motion(0.0)               # reveal; pointer OUTSIDE band
        for t, vis, reason in ticks(st, 999.0, 2999.0):   # 999, 1999, 2999
            self.assertTrue(vis, f"t={t}")
            self.assertEqual(reason, "motion",  # UNCHANGED: still the reveal
                             f"t={t}")          # reason, not idle/hover

    # row 7: outside band, stationary, elapsed 3000 ⇒ (False, "idle")
    def test_row7_outside_band_3000_hides(self):
        st = fresh()
        st.on_pointer_motion(0.0)
        list(ticks(st, 1000.0, 2000.0))
        self.assertEqual(st.tick(3000.0), (False, "idle"))

    # row 8: anywhere, playing=False, elapsed any ⇒ (True, "not-playing")
    def test_row8_not_playing_wins_anywhere_anywhen(self):
        # …IN the band, mid-countdown:
        st = fresh()
        st.on_pointer_motion(0.0)
        st.set_bottom_hover(True, 0.0)
        st.on_playing_changed(False, 100.0)
        self.assertEqual(st.tick(200.0), (True, "not-playing"))
        # …OUTSIDE the band, far past the timeout:
        st = fresh()
        st.on_pointer_motion(0.0)
        st.on_playing_changed(False, 10.0)
        for t, vis, reason in ticks(st, 1010.0, 9990.0, step=1000.0):
            self.assertEqual((vis, reason), (True, "not-playing"), f"t={t}")
        # …and AFTER an idle hide: pause re-reveals, forever.
        st = fresh()
        st.on_pointer_motion(0.0)
        list(ticks(st, 1000.0, 3000.0))
        self.assertFalse(st.visible)
        st.on_playing_changed(False, 3100.0)
        self.assertEqual(st.tick(3100.0), (True, "not-playing"))
        for t, vis, reason in ticks(st, 4100.0, 6100.0):
            self.assertEqual((vis, reason), (True, "not-playing"), f"t={t}")


class HoverStillRevealsAndRearms(unittest.TestCase):
    """The fix must NOT disable hover: the reveal + re-arm on entering the
    band stays exactly as it was (brief §D1 'Required fix')."""

    def test_entering_band_reveals_before_the_timeout(self):
        st = fresh()
        st.on_pointer_motion(0.0)
        list(ticks(st, 1000.0, 5000.0))         # hidden, pointer outside band
        st.set_bottom_hover(True, 5100.0)       # travel INTO the band
        self.assertEqual((st.visible, st.reason), (True, "hover"))
        self.assertEqual(st.tick(6100.0), (True, "hover"))   # fresh countdown
        self.assertEqual(st.tick(8099.0), (True, "hover"))   # 2999ms after edge
        self.assertEqual(st.tick(8100.0), (False, "idle"))   # 3000ms after edge

    def test_rearm_on_entry_survives_a_later_expiry_without_oscillation(self):
        # enter → held → expire → stay: ONE hide transition the whole way.
        st = fresh()
        st.on_pointer_motion(0.0)
        st.set_bottom_hover(True, 0.0)
        prev, transitions = st.visible, []
        for t, vis, _r in ticks(st, 100.0, 10000.0, step=100.0):
            if vis != prev:
                transitions.append((t, vis))
                prev = vis
        self.assertEqual([tr for tr in transitions if tr[1] is False],
                         [(3000.0, False)])     # exactly ONE hide, in-band
        self.assertFalse(st.visible)            # and it stays buried


if __name__ == "__main__":
    unittest.main()
