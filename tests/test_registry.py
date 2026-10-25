"""P8 gate: WindowRegistry — round-robin, thread math, focus mute, and
close-one-doesn't-disturb-others.  Pure logic; fake window tokens."""
import _bootstrap  # noqa: F401  (src on sys.path)

import unittest

import engine as E
import registry as R


class FakeWin:
    def __init__(self, name, target=None):
        self.name = name
        self.target = target
        self._closing = False
        self.muted = None

    def transient_mute(self, on):
        self.muted = bool(on)

    def __repr__(self):
        return f"<{self.name}>"


class FakeCfg:
    def __init__(self, d):
        self.d = d

    def get(self, key, default=None):
        node = self.d
        for part in key.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node


def engines():
    return [
        E.Engine("auto", "Auto (display GPU)", "auto"),
        E.Engine("0000:13:00.0", "iGPU", "vaapi", pci="0000:13:00.0"),
        E.Engine("0000:03:00.0", "dGPU1", "vaapi", pci="0000:03:00.0"),
        E.Engine("0000:07:00.0", "dGPU2", "vaapi", pci="0000:07:00.0"),
        E.Engine("0000:03:00.0:vulkan", "dGPU1 (Vulkan)", "vulkan",
                 pci="0000:03:00.0"),
        E.Engine("cpu", "CPU decode (software)", "cpu"),
    ]


class RoundRobin(unittest.TestCase):
    def test_spreads_over_distinct_gpus_in_order(self):
        r = R.WindowRegistry()
        picks = [r.pick_engine("auto", engines()).id for _ in range(2)]
        self.assertEqual(picks, ["0000:13:00.0", "0000:03:00.0"])

    def test_wraps_around(self):
        r = R.WindowRegistry()
        pool = len(R.WindowRegistry.gpu_pool(engines()))
        self.assertEqual(pool, 4)              # 3 VAAPI + 1 Vulkan
        ids = [r.pick_engine("auto", engines()).id for _ in range(6)]
        self.assertEqual(ids[4], ids[0])
        self.assertEqual(ids[5], ids[1])

    def test_pinned_decoder_pins_every_window(self):
        r = R.WindowRegistry()
        picks = {r.pick_engine("cpu", engines(), E.select_engine).id
                 for _ in range(3)}
        self.assertEqual(picks, {"cpu"})
        picks = {r.pick_engine("0000:07:00.0", engines(),
                               E.select_engine).id for _ in range(3)}
        self.assertEqual(picks, {"0000:07:00.0"})

    def test_no_gpus_falls_back_to_cpu(self):
        r = R.WindowRegistry()
        only = [engines()[0], engines()[-1]]
        self.assertEqual(r.pick_engine("auto", only).id, "cpu")


class ThreadBudget(unittest.TestCase):
    def test_formula_splits_across_windows(self):
        n = 32
        cpu = E.cpu_thread_count(FakeCfg({}), n_windows=4)
        self.assertEqual(cpu, max(1, (n - 2) // 4))

    def test_config_override_and_floor(self):
        self.assertEqual(
            E.cpu_thread_count(FakeCfg({"engine": {"cpu_threads": 5}}), 4), 5)
        self.assertEqual(
            E.cpu_thread_count(FakeCfg({"engine": {"cpu_threads": -9}}), 40),
            1)                                # never below 1

    def test_more_windows_fewer_threads(self):
        one = E.cpu_thread_count(FakeCfg({}), 1)
        four = E.cpu_thread_count(FakeCfg({}), 4)
        self.assertGreater(one, four)


class FocusAudio(unittest.TestCase):
    def setUp(self):
        self.r = R.WindowRegistry()
        self.ws = [FakeWin(f"w{i}") for i in range(3)]
        for w in self.ws:
            self.r.add(w)

    def test_only_focused_keeps_audio(self):
        plan = self.r.focus_plan(self.ws[1], True)
        self.assertEqual(plan, [self.ws[0], self.ws[2]])
        for other in plan:
            other.transient_mute(True)
        self.ws[1].transient_mute(False)
        self.assertEqual([w.muted for w in self.ws], [True, False, True])

    def test_disabled_switch_mutes_nobody(self):
        self.assertEqual(self.r.focus_plan(self.ws[0], False), [])

    def test_no_focus_no_plan(self):
        self.assertEqual(self.r.focus_plan(None, True), [])


class Roster(unittest.TestCase):
    def setUp(self):
        self.r = R.WindowRegistry()
        self.ws = [FakeWin(f"w{i}", target=f"f{i}") for i in range(4)]
        self.ws[0].target = None              # one empty window
        for w in self.ws:
            self.r.add(w)

    def test_empty_window_reuse(self):
        self.assertIs(self.r.empty_window(), self.ws[0])
        self.ws[0].target = "x"
        self.assertIsNone(self.r.empty_window())

    def test_close_one_does_not_disturb_others(self):
        r = R.WindowRegistry()
        a, b, c = FakeWin("a"), FakeWin("b"), FakeWin("c")
        for w in (a, b, c):
            r.add(w)
        first = r.pick_engine("auto", engines()).id
        r.remove(b)
        self.assertEqual(r.count, 2)
        self.assertEqual([w.name for w in r.others(a)], ["c"])
        self.assertNotIn(b, r._live)
        # closing mid-stream: the counter keeps advancing for the rest
        self.assertNotEqual(r.pick_engine("auto", engines()).id, first)
        r.remove(b)                             # idempotent
        self.assertEqual(r.count, 2)

    def test_closing_windows_excluded(self):
        self.ws[1]._closing = True
        self.assertNotIn(self.ws[1], self.r.live())
        self.assertNotIn(self.ws[1], self.r.others(self.ws[2]))


if __name__ == "__main__":
    unittest.main()
