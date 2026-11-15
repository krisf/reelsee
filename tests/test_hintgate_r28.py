"""ROUND 28 second-cause gate: aspect.hint_consistent — the publish gate.

Measured (scratch/diag/r28-diag4.log, real Mutter): a min==max PAspect
written onto a window whose CURRENT size violates it makes the WM resize
that window within the same millisecond — seed 1200x900 (4:3) became
1266x792 one millisecond after the hints write landed, with no app-side
resize anywhere in the log.  That clamp-then-fight is the walk the
operator measured (1200x900 -> 370x230 -> 420x258 -> 54x54), and it is
the reason fixing the ICCCM bit ALONE did not stop the collapse: the
correct 16:9 pair, published onto a violating live size, clamps just the
same.  The window-side rule (src/window.py _aspect_hints_apply) is
therefore "declare only while the live size agrees", and this file gates
that decision PURE.

Negative control: hint_consistent is new in R28 — against HEAD's
aspect.py this file fails on import; and the two operator numbers
(1200x900 seed, 1266x792 clamp answer) must come back False or the gate
that stops the collapse does not exist.  That is the point.
"""
from __future__ import annotations

import unittest

import _bootstrap  # noqa: F401  (sys.path)

import aspect


class HintConsistent(unittest.TestCase):
    def test_exact_sizes_publish(self):
        # the operator's clean-control family and every WM-safe size
        for wh in ((960, 540), (1600, 900), (1200, 675), (16, 9),
                   (1920, 1080), (3840, 2160)):
            self.assertTrue(aspect.hint_consistent(wh[0], wh[1], 16, 9), wh)

    def test_exact_rotated_pair(self):
        self.assertTrue(aspect.hint_consistent(540, 960, 9, 16))
        self.assertFalse(aspect.hint_consistent(540, 960, 16, 9))

    def test_operator_seed_is_refused(self):
        # THE regression: 1200x900 (4:3) vs a 16:9 pin — publishing this
        # is what clamped the operator's window within 1 ms.
        self.assertFalse(aspect.hint_consistent(1200, 900, 16, 9))
        # and the WM's own clamp answer is STILL a violation (ratio
        # 1.598): after any stray clamp we must fall silent, not re-
        # publish and restart the fight.
        self.assertFalse(aspect.hint_consistent(1266, 792, 16, 9))

    def test_one_pixel_rounding_publishes(self):
        # an integer fit of 16:9 cannot always be exact; a sub-pixel miss
        # must stay publishable or the lock is never declared at all.
        self.assertTrue(aspect.hint_consistent(1000, 563, 16, 9))
        self.assertTrue(aspect.hint_consistent(800, 450, 16, 9))

    def test_garbage_refused(self):
        for w, h, n, d in ((0, 540, 16, 9), (960, 0, 16, 9),
                           (-960, 540, 16, 9), (960, 540, 0, 9),
                           (960, 540, 16, 0), (None, 540, 16, 9),
                           (960, 540, None, 9)):
            self.assertFalse(aspect.hint_consistent(w, h, n, d), (w, h, n, d))

    def test_complement_of_mismatch_above_floor(self):
        # outside the tiny-size corner the gate and §R8-B.4's mismatch bar
        # are complements — one language for "off-shape" everywhere.
        for w, h in ((1200, 900), (1000, 563), (1600, 900), (1266, 792),
                     (1000, 1000), (1920, 1079)):
            self.assertEqual(
                aspect.hint_consistent(w, h, 16, 9),
                not aspect.mismatch(w, h, 16 / 9), (w, h))


if __name__ == "__main__":
    unittest.main()
