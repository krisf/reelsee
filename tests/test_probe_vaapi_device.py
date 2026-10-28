"""R16-E gate: scripts/probe_vaapi_device.py's resolution logic.

The probe is an OPERATOR tool for the real desktop (it reads `hwdec-current`
from a live mpv instance) and is deliberately NOT in the headless battery.
What IS testable headlessly is the logic it prints: the per-card table and
the §E.1/§E.2/§E.3 verdict.  Those must agree with `src/engine.py`, or the
operator is told one thing and the app does another.
"""
import _bootstrap  # noqa: F401

import contextlib
import importlib.util
import io
import os
import shutil
import tempfile
import unittest

import engine as E
from _bootstrap import REPO

from test_engine import SLOT_A, SLOT_B, SLOT_I, THREE_CARDS, build_tree


def _load_probe():
    path = REPO / "scripts" / "probe_vaapi_device.py"
    spec = importlib.util.spec_from_file_location("probe_vaapi_device", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


P = _load_probe()


class _Cfg:
    def __init__(self, d):
        self.d = d

    def get(self, key, default=None):
        node = self.d
        for part in key.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node


class ProbeResolution(unittest.TestCase):
    RIG = [("card1", "DP-1", "connected"), ("card1", "DP-2", "connected"),
           ("card2", "DP-1", "connected"), ("card3", "HDMI-A-1", "disconnected")]
    BYPATH = {SLOT_A: "/dev/dri/renderD128", SLOT_B: "/dev/dri/renderD129",
              SLOT_I: "/dev/dri/renderD130"}

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(dir=str(REPO / "scratch"))
        self.addCleanup(self._tmp.cleanup)

    def tree(self, conn, root="p"):
        return build_tree(self._tmp.name + root, THREE_CARDS, conn,
                          dict(self.BYPATH))

    def test_table_reports_the_raw_signals_per_card(self):
        sysroot, devroot = self.tree(self.RIG)
        lines = P.card_table(sysroot, devroot)
        self.assertEqual(len(lines), 3)
        by_slot = {ln.split("slot=")[1].split()[0]: ln for ln in lines}
        self.assertIn(f"node={os.path.join(devroot, 'dri', 'renderD130')}",
                      by_slot[SLOT_I])
        self.assertIn("display=0", by_slot[SLOT_I])
        self.assertIn("discrete=0", by_slot[SLOT_I])
        self.assertIn("eligible=1", by_slot[SLOT_I])
        for slot in (SLOT_A, SLOT_B):
            self.assertIn("display=1", by_slot[slot])
            self.assertIn("discrete=1", by_slot[slot])
            self.assertIn("eligible=0", by_slot[slot])

    def test_table_is_empty_but_honest_without_cards(self):
        sysroot, devroot = build_tree(self._tmp.name + "n", [], [], {})
        lines = P.card_table(sysroot, devroot)
        self.assertEqual(len(lines), 1)
        self.assertIn("no DRM card", lines[0])

    def test_verdict_is_the_integrated_node_with_copy_mode(self):
        sysroot, devroot = self.tree(self.RIG)
        cfg = _Cfg({"playback": {"vaapi_device": ""}})
        node, mode, reason = P.auto_verdict(cfg, sysroot, devroot)
        self.assertEqual(node, os.path.join(devroot, "dri", "renderD130"))
        self.assertEqual(mode, "vaapi-copy")
        self.assertIn("§E.1", reason)

    def test_verdict_follows_the_pin(self):
        sysroot, devroot = self.tree(self.RIG)
        pin = os.path.join(devroot, "dri", "renderD128")
        cfg = _Cfg({"playback": {"vaapi_device": pin}})
        node, mode, reason = P.auto_verdict(cfg, sysroot, devroot)
        self.assertEqual(node, pin)
        self.assertIn("pin", reason)

    def test_verdict_is_cpu_when_nothing_qualifies(self):
        """ROUND18: "nothing qualifies" means NO INTEGRATED CARD (two
        discrete cards, no iGPU) — not "every card drives a display", which
        is now the operator's rig and DOES qualify (its iGPU is eligible)."""
        cards = [c for c in THREE_CARDS if c["slot"] != SLOT_I]
        conn = [("card1", "DP-1", "connected"), ("card2", "DP-1", "connected")]
        sysroot, devroot = build_tree(self._tmp.name + "c", cards, conn,
                                      {SLOT_A: "/dev/dri/renderD128",
                                       SLOT_B: "/dev/dri/renderD129"})
        cfg = _Cfg({"playback": {"vaapi_device": ""}})
        node, mode, reason = P.auto_verdict(cfg, sysroot, devroot)
        self.assertIsNone(node)
        self.assertEqual(mode, "no")
        self.assertIn("CPU", reason)

    def test_real_rig_display_bearing_igpu_is_eligible(self):
        """ROUND18 regression: the operator's rig — card3 carries the
        displays (display=1) yet is integrated (discrete=0), so it must be
        reported ELIGIBLE and be the automatic verdict.  The old connector
        clause made the probe tell the operator the opposite of the app."""
        conn = [("card1", "DP-1", "connected"), ("card1", "DP-2", "connected"),
                ("card2", "DP-1", "connected"),
                ("card3", "HDMI-A-1", "connected")]
        sysroot, devroot = self.tree(conn, root="rr")
        lines = P.card_table(sysroot, devroot)
        by_slot = {ln.split("slot=")[1].split()[0]: ln for ln in lines}
        self.assertIn("display=1", by_slot[SLOT_I])
        self.assertIn("discrete=0", by_slot[SLOT_I])
        self.assertIn("eligible=1", by_slot[SLOT_I])
        cfg = _Cfg({"playback": {"vaapi_device": ""}})
        node, mode, reason = P.auto_verdict(cfg, sysroot, devroot)
        self.assertEqual(node, os.path.join(devroot, "dri", "renderD130"))
        self.assertEqual(mode, "vaapi-copy")
        self.assertIn("§E.1", reason)

    def test_main_reports_cpu_and_exits_zero_when_no_node_qualifies(self):
        """The headless-CI shape of the probe: no GPU is asked for, so the
        run must complete and say so (exit 0), never pretend to probe.
        Fixture: two discrete cards, no iGPU — nothing integrates, so §E.1
        answers CPU.

        REELSEE_CONFIG_DIR is pointed at the fixture so the probe's
        `config.Config()` never touches the real ~/.config/reelsee."""
        cards = [c for c in THREE_CARDS if c["slot"] != SLOT_I]
        conn = [("card1", "DP-1", "connected"), ("card2", "DP-1", "connected")]
        sysroot, devroot = build_tree(self._tmp.name + "m", cards, conn,
                                      {SLOT_A: "/dev/dri/renderD128",
                                       SLOT_B: "/dev/dri/renderD129"})
        cfgdir = os.path.join(self._tmp.name + "m", "cfg")
        os.makedirs(cfgdir, exist_ok=True)
        old = os.environ.get("REELSEE_CONFIG_DIR")
        os.environ["REELSEE_CONFIG_DIR"] = cfgdir
        self.addCleanup(lambda: os.environ.pop("REELSEE_CONFIG_DIR", None)
                        if old is None else
                        os.environ.__setitem__("REELSEE_CONFIG_DIR", old))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = P.main(["--sysroot", sysroot, "--devroot", devroot])
        text = out.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("PROBE_AUTO node=none", text)
        self.assertIn("PROBE_RESULT node=none requested=no", text)
        self.assertIn("engaged=1", text)
        shutil.rmtree(self._tmp.name + "m", True)


if __name__ == "__main__":
    unittest.main()
