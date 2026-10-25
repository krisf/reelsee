"""P5 gate: A/B pure contract (set/validate/clear/count/persist) ≥12 cases."""
import _bootstrap  # noqa: F401

import os
import tempfile
import unittest

import ab
import config as C

REPO = _bootstrap.REPO


class FakeCore:
    def __init__(self):
        self.calls: list[tuple[str, object]] = []

    def set_property(self, name, value):
        self.calls.append((name, value))


class SetValidate(unittest.TestCase):
    def test_set_a_sets_a(self):
        st = ab.ABState()
        self.assertEqual(st.set_a(1.0), (True, ""))
        self.assertEqual(st.a, 1.0)
        self.assertFalse(st.active)

    def test_set_a_clears_too_close_b(self):
        st = ab.ABState()
        st.set_a(1.0)
        st.set_b(2.0)
        st.set_a(1.9)
        self.assertIsNone(st.b)          # b <= a + 0.15

    def test_set_a_keeps_far_b(self):
        st = ab.ABState()
        st.set_a(1.0)
        st.set_b(2.0)
        st.set_a(1.2)
        self.assertEqual(st.b, 2.0)

    def test_set_b_requires_a(self):
        st = ab.ABState()
        ok, reason = st.set_b(5.0)
        self.assertFalse(ok)
        self.assertEqual(reason, ab.REASON_NO_A)

    def test_set_b_gap_too_small_rejected(self):
        st = ab.ABState()
        st.set_a(2.0)
        ok, reason = st.set_b(2.1)
        self.assertFalse(ok)
        self.assertEqual(reason, "B must be at least 0.15s after A")
        self.assertIsNone(st.b)

    def test_set_b_exactly_015_accepted(self):
        st = ab.ABState()
        st.set_a(2.0)
        self.assertTrue(st.set_b(2.15)[0])
        self.assertTrue(st.active)

    def test_set_a_rejects_junk(self):
        st = ab.ABState()
        for junk in (-1.0, None, "x", True, float("nan")):
            ok, _ = st.set_a(junk)
            self.assertFalse(ok, repr(junk))
        self.assertIsNone(st.a)

    def test_clear(self):
        st = ab.ABState()
        st.set_a(1.0)
        st.set_b(2.0)
        st.clear()
        self.assertEqual((st.a, st.b), (None, None))
        self.assertFalse(st.active)


class Count(unittest.TestCase):
    def test_count_inf_default(self):
        self.assertEqual(ab.ABState().count, "inf")

    def test_count_valid_values(self):
        st = ab.ABState()
        self.assertTrue(st.set_count(3)[0])
        self.assertEqual(st.count, 3)
        self.assertTrue(st.set_count("7")[0])
        self.assertEqual(st.count, 7)
        self.assertTrue(st.set_count("inf")[0])
        self.assertEqual(st.count, "inf")

    def test_count_invalid_values(self):
        st = ab.ABState()
        st.set_count(5)
        for junk in (0, -2, "x", True, 2.5):
            self.assertFalse(st.set_count(junk)[0], repr(junk))
            self.assertEqual(st.count, 5)          # unchanged on rejection


class Apply(unittest.TestCase):
    def test_apply_pushes_all_three(self):
        core = FakeCore()
        st = ab.ABState()
        st.set_a(1.0)
        st.set_b(2.0)
        st.apply(core)
        self.assertEqual(core.calls, [("ab-loop-a", 1.0),
                                      ("ab-loop-b", 2.0),
                                      ("ab-loop-count", "no")])

    def test_apply_clears_with_no(self):
        core = FakeCore()
        ab.ABState().apply(core)
        self.assertEqual([v for _n, v in core.calls][:2], ["no", "no"])

    def test_apply_numeric_count(self):
        core = FakeCore()
        st = ab.ABState(count=4)
        st.apply(core)
        self.assertEqual(core.calls[-1], ("ab-loop-count", 4))


class Persist(unittest.TestCase):
    def test_roundtrip_dict(self):
        st = ab.ABState()
        st.set_a(3.5)
        st.set_b(9.25)
        st.set_count(2)
        st2 = ab.ABState.from_dict(st.to_dict())
        self.assertEqual((st2.a, st2.b, st2.count), (3.5, 9.25, 2))

    def test_from_dict_repairs_stale_pair(self):
        st = ab.ABState.from_dict({"a": 5.0, "b": 5.05})
        self.assertEqual(st.a, 5.0)
        self.assertIsNone(st.b)

    def test_ab_store_json_roundtrip(self):
        with tempfile.TemporaryDirectory(dir=str(REPO / "scratch")) as d:
            path = os.path.join(d, "ab.json")
            store = C.AbStore(path)
            st = ab.ABState()
            st.set_a(1.0)
            st.set_b(2.0)
            store.set("/media/clip.mp4", st.to_dict())
            store2 = C.AbStore(path)
            got = ab.ABState.from_dict(store2.get("/media/clip.mp4"))
            self.assertEqual((got.a, got.b), (1.0, 2.0))
            store2.remove("/media/clip.mp4")
            self.assertIsNone(C.AbStore(path).get("/media/clip.mp4"))


if __name__ == "__main__":
    unittest.main()


class PressClearFirst(unittest.TestCase):
    """R5-E: pressing A/B while a loop is ACTIVE clears the WHOLE loop
    first, then applies the pressed mark.  Four gate scenarios."""

    def _active(self):
        st = ab.ABState()
        st.set_a(5.0)
        st.set_b(10.0)
        self.assertTrue(st.active)
        return st

    def test_active_a_yields_exactly_one_mark_inactive(self):
        st = self._active()
        ok, cleared, reason = ab.press_ab(st, "a", 20.0)
        self.assertTrue(ok)
        self.assertTrue(cleared)
        self.assertEqual(reason, "")
        marks = [x for x in (st.a, st.b) if x is not None]
        self.assertEqual(marks, [20.0])          # exactly 1 mark
        self.assertFalse(st.active)              # active=False

    def test_active_b_no_marks_set_a_first(self):
        st = self._active()
        ok, cleared, reason = ab.press_ab(st, "b", 20.0)
        self.assertFalse(ok)
        self.assertTrue(cleared)
        self.assertEqual(reason, ab.REASON_NO_A)
        self.assertIsNone(st.a)                  # NO partial state
        self.assertIsNone(st.b)
        self.assertFalse(st.active)

    def test_active_a_then_b_fresh_loop(self):
        st = self._active()
        ab.press_ab(st, "a", 20.0)
        ok, cleared, _r = ab.press_ab(st, "b", 25.0)   # not active -> plain
        self.assertTrue(ok)
        self.assertFalse(cleared)
        self.assertEqual((st.a, st.b), (20.0, 25.0))   # new timestamps
        self.assertTrue(st.active)

    def test_off_then_a_then_b_unchanged(self):
        st = ab.ABState()
        ok, cleared, _r = ab.press_ab(st, "a", 3.0)
        self.assertTrue(ok and not cleared)
        ok, cleared, _r = ab.press_ab(st, "b", 8.0)
        self.assertTrue(ok and not cleared)
        self.assertEqual((st.a, st.b), (3.0, 8.0))

    def test_invalid_time_after_clear_leaves_only_clear(self):
        st = self._active()
        ok, cleared, reason = ab.press_ab(st, "a", None)
        self.assertFalse(ok)
        self.assertTrue(cleared)                 # clear happened regardless
        self.assertIsNone(st.a)
        self.assertIsNone(st.b)
        self.assertEqual(reason, ab.REASON_TIME)
