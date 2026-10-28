"""P8 gate: WindowRegistry — decode-plan engine choice, thread math, focus
mute, and close-one-doesn't-disturb-others.  Pure logic; fake window tokens."""
import _bootstrap  # noqa: F401  (src on sys.path)

import os
import shutil
import tempfile
import unittest

import engine as E
import registry as R
from _bootstrap import REPO

from test_engine import SLOT_A, SLOT_B, SLOT_I, THREE_CARDS, build_tree


class FakeWin:
    def __init__(self, name, target=None, gpu=False, user=False, seq=0):
        self.name = name
        self.target = target
        self._closing = False
        self.muted = None
        # R16-D attributes the decode plan reads (see registry.demotion_target)
        self._gpu = gpu
        self._decode_user = user
        self._focus_seq = seq
        self.demoted = 0

    def transient_mute(self, on):
        self.muted = bool(on)

    def gpu_decoding(self):
        return self._gpu

    def demote_to_cpu(self, reason="plan"):
        self.demoted += 1
        self._gpu = False

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


class DecodeChoice(unittest.TestCase):
    """R16-D: `auto` asks the decode plan instead of round-robining over every
    GPU.  The old tests in this class asserted that windows spread over
    distinct GPUs — that behaviour is superseded by the user mandate that a
    dGPU is never chosen automatically, so they are replaced here (see the
    commit message) rather than deleted."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(dir=str(REPO / "scratch"))
        # rig: both R9700s drive displays, the iGPU has no connector
        self.sysroot, self.devroot = build_tree(
            self._tmp.name, THREE_CARDS,
            [("card1", "DP-1", "connected"), ("card2", "DP-1", "connected"),
             ("card3", "HDMI-A-1", "disconnected")],
            {SLOT_A: "/dev/dri/renderD128", SLOT_B: "/dev/dri/renderD129",
             SLOT_I: "/dev/dri/renderD130"})
        self.addCleanup(self._tmp.cleanup)

    def test_auto_is_the_igpu_up_to_capacity_then_cpu(self):
        r = R.WindowRegistry()
        ids = []
        for i in range(4):
            ids.append(r.pick_engine("auto", engines(), E.select_engine,
                                     capacity=2, sysroot=self.sysroot).id)
            r.add(FakeWin(f"w{i}", target=f"f{i}"))
        self.assertEqual(ids, [SLOT_I, SLOT_I, "cpu", "cpu"])

    def test_auto_never_names_a_dgpu_even_when_capacity_is_generous(self):
        r = R.WindowRegistry()
        for i in range(6):
            e = r.pick_engine("auto", engines(), E.select_engine,
                              capacity=99, sysroot=self.sysroot)
            self.assertNotIn(e.id, (SLOT_A, SLOT_B), f"{e.id} auto-selected")
            r.add(FakeWin(f"w{i}", target=f"f{i}"))

    def test_no_integrated_card_is_cpu_for_everyone(self):
        """ROUND18: "nothing qualifies" means no INTEGRATED card — two
        discrete cards, no iGPU — so the plan is CPU, not a dGPU.  (A rig
        where every card drives a monitor is now the operator's rig and the
        iGPU IS eligible; that case is covered by RealRigRound18.)"""
        root = self._tmp.name + "all"
        cards = [c for c in THREE_CARDS if c["slot"] != SLOT_I]
        sysroot, devroot = build_tree(
            root, cards,
            [("card1", "DP-1", "connected"), ("card2", "DP-1", "connected")],
            {})
        self.addCleanup(shutil.rmtree, root, True)
        r = R.WindowRegistry()
        for i in range(3):
            self.assertEqual(
                r.pick_engine("auto", engines(), E.select_engine,
                              capacity=2, sysroot=sysroot).id, "cpu")
            r.add(FakeWin(f"w{i}", target=f"f{i}"))

    def test_pinned_decoder_pins_every_window(self):
        r = R.WindowRegistry()
        picks = {r.pick_engine("cpu", engines(), E.select_engine).id
                 for _ in range(3)}
        self.assertEqual(picks, {"cpu"})
        # an explicit dGPU choice is honoured: the ban is on AUTOMATIC choice
        picks = {r.pick_engine(SLOT_B, engines(), E.select_engine).id
                 for _ in range(3)}
        self.assertEqual(picks, {SLOT_B})

    def test_pinned_node_decides_the_card_capacity_still_decides_the_count(
            self):
        """§E.2: `playback.vaapi_device` picks the CARD (even a dGPU, because a
        human chose it) but the plan still caps how many windows use it.  The
        fixture is discrete-only — no integrated card — so without the pin
        every window would be CPU."""
        root = self._tmp.name + "pin"
        cards = [c for c in THREE_CARDS if c["slot"] != SLOT_I]
        sysroot, devroot = build_tree(
            root, cards,
            [("card1", "DP-1", "connected"), ("card2", "DP-1", "connected")],
            {SLOT_A: "/dev/dri/renderD128", SLOT_B: "/dev/dri/renderD129"})
        self.addCleanup(shutil.rmtree, root, True)
        pin = os.path.join(devroot, "dri", "renderD128")
        r = R.WindowRegistry()
        ids = []
        for i in range(3):
            e = r.pick_engine("auto", engines(), E.select_engine, capacity=2,
                              sysroot=sysroot, pin=pin)
            ids.append((e.kind, e.render_node))
            r.add(FakeWin(f"w{i}", target=f"f{i}"))
        self.assertEqual(ids, [("vaapi", pin), ("vaapi", pin), ("cpu", None)])

    def test_pin_that_enumeration_does_not_offer_still_works(self):
        """The escape hatch must not depend on the node being enumerable."""
        r = R.WindowRegistry()
        e = r.pick_engine("auto", engines(), E.select_engine, capacity=2,
                          sysroot=self.sysroot, pin="/dev/dri/renderD999")
        self.assertEqual(e.kind, "vaapi")
        self.assertEqual(e.render_node, "/dev/dri/renderD999")

    def test_empty_pin_leaves_the_integrated_rule_in_charge(self):
        r = R.WindowRegistry()
        e = r.pick_engine("auto", engines(), E.select_engine, capacity=2,
                          sysroot=self.sysroot, pin="")
        self.assertEqual(e.id, SLOT_I)

    def test_no_gpus_falls_back_to_cpu(self):
        r = R.WindowRegistry()
        only = [engines()[0], engines()[-1]]
        self.assertEqual(r.pick_engine("auto", only,
                                       sysroot=self.sysroot).id, "cpu")


class DemotionTarget(unittest.TestCase):
    """§D.2: over capacity, the window that gained focus LAST is demoted —
    and a window the user pinned to a decoder is never a candidate."""

    def test_within_capacity_nobody_is_demoted(self):
        ws = [FakeWin("a", gpu=True, seq=1), FakeWin("b", gpu=True, seq=2)]
        self.assertIsNone(R.demotion_target(ws, 2))

    def test_over_capacity_targets_the_last_focused(self):
        ws = [FakeWin("a", gpu=True, seq=1),
              FakeWin("b", gpu=True, seq=3),
              FakeWin("c", gpu=True, seq=2)]
        self.assertIs(R.demotion_target(ws, 2), ws[1])

    def test_never_a_user_pinned_window(self):
        # three GPU windows over a capacity of two; b is the last focused but
        # the user chose its decoder by hand, so c — the next-most-recent —
        # is the one the plan demotes.
        ws = [FakeWin("a", gpu=True, seq=1),
              FakeWin("b", gpu=True, seq=9, user=True),
              FakeWin("c", gpu=True, seq=5),
              FakeWin("d", gpu=True, seq=3)]
        self.assertIs(R.demotion_target(ws, 2), ws[2])
        # and if EVERY over-capacity candidate is pinned, nobody is demoted
        for w in ws:
            w._decode_user = True
        self.assertIsNone(R.demotion_target(ws, 2))

    def test_cpu_windows_are_not_counted_against_capacity(self):
        ws = [FakeWin("a", gpu=True, seq=1), FakeWin("b", gpu=False, seq=2),
              FakeWin("c", gpu=False, seq=3), FakeWin("d", gpu=False, seq=4)]
        self.assertIsNone(R.demotion_target(ws, 2))

    def test_all_user_pinned_is_nobody(self):
        ws = [FakeWin("a", gpu=True, seq=1, user=True),
              FakeWin("b", gpu=True, seq=2, user=True),
              FakeWin("c", gpu=True, seq=3, user=True)]
        self.assertIsNone(R.demotion_target(ws, 2))

    def test_garbage_capacity_is_zero_not_a_crash(self):
        ws = [FakeWin("a", gpu=True, seq=1), FakeWin("b", gpu=True, seq=2)]
        self.assertIs(R.demotion_target(ws, "junk"), ws[1])
        self.assertIs(R.demotion_target(ws, None), ws[1])

    def test_windows_without_the_protocol_are_ignored(self):
        """A window that has not grown the R16-D attributes (an older fake, a
        half-built window) must not be demoted behind its own back."""
        plain = object()
        a = FakeWin("a", gpu=True, seq=1)
        # alone, a is within a capacity of 1 -> nobody is touched
        self.assertIsNone(R.demotion_target([a, plain], 1))
        # with two GPU windows and capacity 0, the plain object is not a
        # candidate even though it sorts after a by sequence
        b = FakeWin("b", gpu=True, seq=2)
        self.assertIs(R.demotion_target([a, plain, b], 0), b)


class ThreadBudget(unittest.TestCase):
    """R16-C: the budget is a pure function of the live window count.  The
    full truth table lives in tests/test_threads.py; these are the
    registry-side expectations the old `(cores - 2) // n` tests encoded."""

    def test_formula_splits_across_windows(self):
        n = 32
        cpu = E.cpu_thread_count(FakeCfg({}), n_windows=4)
        self.assertEqual(cpu, min(E.THREADS_CAP, max(2, n // (2 * 4))))

    def test_config_override_and_floor(self):
        self.assertEqual(
            E.cpu_thread_count(FakeCfg({"engine": {"cpu_threads": 5}}), 4), 5)
        # a negative override means "not set", so the budget applies — and its
        # floor is 2, not the old 1 (lavc needs a reference-frame thread too)
        self.assertEqual(
            E.cpu_thread_count(FakeCfg({"engine": {"cpu_threads": -9}}), 40),
            2)

    def test_one_window_is_auto_two_is_not(self):
        one = E.cpu_thread_count(FakeCfg({}), 1)
        four = E.cpu_thread_count(FakeCfg({}), 4)
        self.assertEqual(one, 0)              # 0 == leave lavc's default alone
        self.assertGreaterEqual(four, 2)
        self.assertLessEqual(four, E.THREADS_CAP)


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
        self.assertEqual(r.count, 3)
        r.remove(b)
        self.assertEqual(r.count, 2)
        self.assertEqual([w.name for w in r.others(a)], ["c"])
        self.assertNotIn(b, r._live)
        # closing must not corrupt the roster for the survivors
        self.assertEqual([w.name for w in r.live()], ["a", "c"])
        r.remove(b)                             # idempotent
        self.assertEqual(r.count, 2)

    def test_closing_windows_excluded(self):
        self.ws[1]._closing = True
        self.assertNotIn(self.ws[1], self.r.live())
        self.assertNotIn(self.ws[1], self.r.others(self.ws[2]))


if __name__ == "__main__":
    unittest.main()
