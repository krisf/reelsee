"""R16-C gates: the CPU decode-thread budget is a pure function of the number
of live windows, and it is applied to CPU-decoded windows only.

`vd-lavc-threads` used to be `(cores - 2) // n_windows` for EVERY count, so a
single window got a number lavc would have picked anyway while N windows each
still got a slice of the whole machine.  The new rule (brief §C) is
`max(2, cores // (2*N))`, capped at 16, and 1 window means "auto" — no option
written at all.

Gates:
  * the truth table, every row of it, plus the degenerate inputs;
  * an explicit `engine.cpu_threads` still wins outright;
  * `mpv_options` writes the option only when the budget is real (N >= 2) and
    never for a GPU engine;
  * the window-side writer touches CPU-decoded windows ONLY (the observed
    `hwdec-current` decides, R9/R2-P2), never writes 0/auto, is idempotent,
    and does nothing to a closing window.
"""
import _bootstrap  # noqa: F401  (sys.path for src/)
import os
import unittest

import engine as E
from window import ReelseeWindow


class TruthTable(unittest.TestCase):
    """Every row the brief specifies, spelled out rather than re-derived by
    the test's own copy of the formula (that would test nothing)."""

    ROWS = [
        # (n_windows, cores) -> threads per window; 0 = leave lavc's default
        (1, 4, 0),
        (1, 8, 0),
        (1, 16, 0),
        (1, 64, 0),
        (0, 8, 0),          # nothing open yet
        (-3, 8, 0),         # nonsense input is not a budget
        (2, 8, 2),          # 8 // 4
        (2, 16, 4),         # 16 // 4
        (2, 32, 8),
        (2, 64, 16),        # exactly the cap
        (2, 128, 16),       # 32 would be asked for; the cap holds
        (3, 16, 2),         # 16 // 6
        (4, 16, 2),         # 16 // 8
        (4, 32, 4),
        (5, 32, 3),         # 32 // 10
        (8, 16, 2),         # 16 // 16 == 1 -> the floor of 2
        (16, 16, 2),        # 16 // 32 == 0 -> the floor of 2
        (2, 1, 2),          # a 1-core machine still gets a usable pair
        (2, 0, 0),          # no usable core count reported -> auto
    ]

    def test_rows(self):
        for n, cores, want in self.ROWS:
            with self.subTest(n_windows=n, cores=cores):
                self.assertEqual(E.threads_for(n, cores), want)

    def test_monotone_in_windows(self):
        """More windows never means more threads per window."""
        prev = None
        for n in range(2, 13):
            got = E.threads_for(n, 32)
            self.assertGreaterEqual(got, 2)
            self.assertLessEqual(got, E.THREADS_CAP)
            if prev is not None:
                self.assertLessEqual(got, prev)
            prev = got

    def test_never_below_two_above_the_floor(self):
        for n in range(2, 64):
            for cores in (1, 2, 4, 8, 16, 32, 64, 128):
                got = E.threads_for(n, cores)
                self.assertTrue(got == 0 or got >= 2, (n, cores, got))
                self.assertLessEqual(got, E.THREADS_CAP)

    def test_garbage_inputs_are_auto_not_a_crash(self):
        for bad in (None, "two", 2.7, [1]):
            with self.subTest(bad=repr(bad)):
                self.assertIn(E.threads_for(bad, 8), (0, 2, 3, 4))


class ConfigOverride(unittest.TestCase):
    class Cfg:
        def __init__(self, mapping):
            self.m = mapping

        def get(self, key, default=None):
            return self.m.get(key, default)

    def test_explicit_value_wins_over_the_budget(self):
        self.assertEqual(
            E.cpu_thread_count(self.Cfg({"engine.cpu_threads": 5}), 4), 5)
        self.assertEqual(
            E.cpu_thread_count(self.Cfg({"engine.cpu_threads": 40}), 2), 40)

    def test_unset_or_negative_defers_to_the_budget(self):
        for v in (0, -9, None, "junk"):
            with self.subTest(v=repr(v)):
                self.assertEqual(
                    E.cpu_thread_count(self.Cfg({"engine.cpu_threads": v}), 4),
                    E.threads_for(4, os.cpu_count() or 2))

    def test_no_config_object_still_budgets(self):
        self.assertEqual(E.cpu_thread_count(None, 3),
                         E.threads_for(3, os.cpu_count() or 2))


class Options(unittest.TestCase):
    CPU = E.Engine(id="cpu", label="CPU", kind="cpu")
    GPU = E.Engine(id="0000:07:00.0", label="GPU", kind="vaapi",
                   pci="0000:07:00.0")

    def test_single_window_writes_nothing(self):
        o = E.mpv_options(self.CPU, None, 1, "/nonexistent", "/nonexistent")
        self.assertEqual(o["hwdec"], "no")
        self.assertNotIn("vd-lavc-threads", o)

    def test_two_windows_get_their_share(self):
        import os
        o = E.mpv_options(self.CPU, None, 2, "/nonexistent", "/nonexistent")
        self.assertEqual(o["vd-lavc-threads"],
                         str(E.threads_for(2, os.cpu_count() or 2)))
        self.assertGreater(int(o["vd-lavc-threads"]), 0)

    def test_gpu_engine_is_never_given_a_lavc_count(self):
        for n in (1, 2, 5):
            o = E.mpv_options(self.GPU, None, n, "/nonexistent",
                              "/nonexistent")
            self.assertNotIn("vd-lavc-threads", o)
            self.assertEqual(o["hwdec"], "vaapi-copy")


class _Core:
    def __init__(self, hwdec="no"):
        self.props = []
        self.snapshot = {"hwdec-current": hwdec}

    def set_property(self, name, value):
        self.props.append((name, value))


class _Win:
    """Fake `self`: the REAL budget writer, bound unbound."""
    _cpu_decoding = ReelseeWindow._cpu_decoding
    apply_thread_budget = ReelseeWindow.apply_thread_budget

    def __init__(self, hwdec="no", closing=False):
        self.core = _Core(hwdec)
        self._closing = closing
        self._lavc_threads = None


class WindowSide(unittest.TestCase):
    def test_cpu_window_is_given_its_share(self):
        w = _Win(hwdec="no")
        w.apply_thread_budget(4)
        self.assertEqual(w.core.props, [("vd-lavc-threads", "4")])
        self.assertEqual(w._lavc_threads, "4")

    def test_gpu_decoded_window_is_untouched(self):
        for hwdec in ("vaapi", "vaapi-copy", "vdpau", "nvdec"):
            with self.subTest(hwdec=hwdec):
                w = _Win(hwdec=hwdec)
                w.apply_thread_budget(4)
                self.assertEqual(w.core.props, [])
                self.assertIsNone(w._lavc_threads)

    def test_unknown_state_is_not_yet_cpu(self):
        """Before the first frame there is nothing to budget — and a
        configured GPU engine that silently failed must read CPU, not GPU
        (R2-P2), which is the same classifier's other half."""
        w = _Win(hwdec=None)
        w.apply_thread_budget(4)
        self.assertEqual(w.core.props, [])
        w = _Win(hwdec="")
        w.apply_thread_budget(4)
        self.assertEqual(w.core.props, [])

    def test_auto_writes_nothing(self):
        w = _Win()
        w.apply_thread_budget(0)
        self.assertEqual(w.core.props, [])

    def test_idempotent(self):
        w = _Win()
        w.apply_thread_budget(4)
        w.apply_thread_budget(4)
        self.assertEqual(w.core.props, [("vd-lavc-threads", "4")])
        w.apply_thread_budget(2)
        self.assertEqual([v for _, v in w.core.props], ["4", "2"])

    def test_closing_window_is_left_alone(self):
        w = _Win(closing=True)
        w.apply_thread_budget(4)
        self.assertEqual(w.core.props, [])


if __name__ == "__main__":
    unittest.main()
