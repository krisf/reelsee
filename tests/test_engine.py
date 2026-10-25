"""P6 gate: engine enumeration + §5.3 mpv_options + renderer_override exec.

Everything filesystem-facing runs against FAKE sysfs / dev fixtures built
under scratch/ (the sandbox has no /dev/dri, so the real tree is never
assumed).  Covers Appendix B steps 1-5, the §5.3 mapping + invariants, the
engine selector resolver, the CPU-threads formula, the ProbeCache and the
guarded re-exec (in-process and one true subprocess exec).
"""
import _bootstrap  # noqa: F401  (REPO/SRC sys.path)

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import engine as E
from _bootstrap import REPO

SLOT_I = "0000:13:00.0"     # "iGPU" (display-connected in fixtures)
SLOT_A = "0000:03:00.0"
SLOT_B = "0000:07:00.0"

LSPCI = {SLOT_I: "Granite Ridge [Radeon Graphics]",
         SLOT_A: "Navi 48 [Radeon AI PRO R9700]",
         SLOT_B: "Navi 48 [Radeon AI PRO R9700]"}
VULKAN = {"1002:13c0": "RADV GFX10_3 (13c0)",
          "1002:7551": "RADV NAVI48 (7551)"}


def build_tree(root, cards, connectors=(), bypath=None):
    """cards: list of dicts(card, slot, driver, pci_id, render)."""
    sysroot = Path(root) / "sys"
    devroot = Path(root) / "dev"
    (sysroot / "class" / "drm").mkdir(parents=True)
    for c in cards:
        dev = sysroot / "devices" / c["slot"]
        dev.mkdir(parents=True)
        card = sysroot / "class" / "drm" / c["card"]
        card.mkdir()
        (card / "device").symlink_to(os.path.relpath(dev, card))
        uev = [f"DRIVER={c['driver']}", f"PCI_ID={c['pci_id']}",
               f"PCI_SLOT_NAME={c['slot']}"]
        (dev / "uevent").write_text("\n".join(uev) + "\n")
        if c.get("render"):
            (dev / "drm").mkdir()
            (dev / "drm" / c["render"]).mkdir()
    for card, conn, status in connectors:
        cdir = sysroot / "class" / "drm" / f"{card}-{conn}"
        cdir.mkdir()
        (cdir / "status").write_text(status + "\n")
    for slot, node in (bypath or {}).items():
        bp = devroot / "dri" / "by-path"
        bp.mkdir(parents=True)
        node_abs = os.path.join(devroot, "dri", os.path.basename(node))
        os.makedirs(os.path.dirname(node_abs), exist_ok=True)
        open(node_abs, "w").close()
        os.symlink(node_abs, bp / f"pci-{slot}-render")
    return str(sysroot), str(devroot)


THREE_CARDS = [
    dict(card="card1", slot=SLOT_A, driver="amdgpu", pci_id="1002:7551",
         render="renderD128"),
    dict(card="card2", slot=SLOT_B, driver="amdgpu", pci_id="1002:7551",
         render="renderD129"),
    dict(card="card3", slot=SLOT_I, driver="amdgpu", pci_id="1002:13c0",
         render="renderD130"),
]
CONNECTED = [("card3", "HDMI-A-1", "connected"),
             ("card1", "DP-1", "disconnected")]


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


class TreeCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(dir=str(REPO / "scratch"))
        self.sysroot, self.devroot = build_tree(
            self._tmp.name, THREE_CARDS, CONNECTED,
            {SLOT_A: "/dev/dri/renderD128"})
        self.addCleanup(self._tmp.cleanup)

    def engines(self, vulkan=None):
        return E.enumerate_engines(self.sysroot, self.devroot,
                                   lspci=LSPCI.get,
                                   vulkan=(vulkan or (lambda: {})))


class Enumeration(TreeCase):
    def test_by_path_mapping(self):
        m = E.by_path_render_nodes(self.devroot)
        self.assertEqual(m, {SLOT_A: os.path.join(self.devroot, "dri",
                                                  "renderD128")})

    def test_sysfs_card_fields(self):
        cards = E.sysfs_cards(self.sysroot)
        self.assertEqual(set(cards), {SLOT_A, SLOT_B, SLOT_I})
        self.assertEqual(cards[SLOT_I]["driver"], "amdgpu")
        self.assertEqual(cards[SLOT_I]["pci_id"], "1002:13c0")
        self.assertEqual(cards[SLOT_I]["render"], "renderD130")

    def test_display_gpu_detection(self):
        self.assertEqual(E.display_gpu_slot(self.sysroot), SLOT_I)

    def test_display_gpu_none_when_all_disconnected(self):
        with tempfile.TemporaryDirectory(dir=str(REPO / "scratch")) as d:
            sysroot, devroot = build_tree(d, THREE_CARDS,
                                          [("card1", "DP-1", "disconnected")])
            self.assertIsNone(E.display_gpu_slot(sysroot))

    def test_order_auto_display_others_cpu(self):
        ids = [e.id for e in self.engines()]
        self.assertEqual(ids[0], "auto")
        self.assertEqual(ids[1], SLOT_I)          # display GPU right after
        self.assertEqual(ids[-1], "cpu")          # R-8: CPU always last
        self.assertEqual(set(ids), {"auto", SLOT_I, SLOT_A, SLOT_B, "cpu"})

    def test_labels_igpu_dgpu_duplicates(self):
        by_id = {e.id: e for e in self.engines()}
        self.assertTrue(by_id[SLOT_I].label.startswith(
            "iGPU — Granite Ridge [Radeon Graphics]"))
        self.assertTrue(by_id[SLOT_A].label.startswith(
            "dGPU — Navi 48 [Radeon AI PRO R9700] #1"))
        self.assertTrue(by_id[SLOT_B].label.startswith(
            "dGPU — Navi 48 [Radeon AI PRO R9700] #2"))

    def test_vulkan_variant_appended(self):
        es = self.engines(vulkan=lambda: VULKAN)
        ids = [e.id for e in es]
        self.assertIn(f"{SLOT_I}:vulkan", ids)
        self.assertIn(f"{SLOT_A}:vulkan", ids)
        vk = next(e for e in es if e.id.endswith(":vulkan"))
        self.assertEqual(vk.kind, "vulkan")
        self.assertIn("(Vulkan)", vk.label)
        self.assertLess(ids.index(f"{SLOT_A}:vulkan"), ids.index("cpu"))

    def test_select_engine(self):
        es = self.engines(vulkan=lambda: VULKAN)
        self.assertEqual(E.select_engine("cpu", es).kind, "cpu")
        self.assertEqual(E.select_engine(SLOT_B, es).pci, SLOT_B)
        self.assertEqual(E.select_engine(f"{SLOT_B}:vulkan", es).kind,
                         "vulkan")
        self.assertEqual(E.select_engine("nonsense:9", es).id, "auto")
        self.assertEqual(E.select_engine("", es).id, "auto")


class OptionMapping(TreeCase):
    def test_auto_block(self):
        # R2-P1/B1: auto no longer requests auto-safe — GPU engines request
        # the copy mode that actually engages under the embedded GL path.
        es = self.engines()
        o = E.mpv_options(es[0], None, 1, self.sysroot, self.devroot)
        self.assertEqual(o["hwdec"], "vaapi-copy")
        self.assertEqual(o["hwdec-codecs"], "all")
        self.assertTrue(o["vaapi-device"].startswith("/"))   # absolute path
        self.assertTrue(o["vaapi-device"].endswith("renderD130"))

    def test_copy_default_zero_copy_opt_in(self):
        # R2-P1/B1: EVERY GPU engine defaults to vaapi-copy (zero-copy
        # "vaapi" silently never engages in this render path); engine.
        # zero_copy=true opts back into it.
        es = {e.id: e for e in self.engines()}
        disp = E.mpv_options(es[SLOT_I], None, 1, self.sysroot, self.devroot)
        self.assertEqual(disp["hwdec"], "vaapi-copy")
        self.assertEqual(disp["vaapi-device"],
                         os.path.join(self.devroot, "dri", "renderD130"))
        other = E.mpv_options(es[SLOT_B], None, 1, self.sysroot, self.devroot)
        self.assertEqual(other["hwdec"], "vaapi-copy")
        self.assertTrue(other["vaapi-device"].endswith("renderD129"))
        zcfg = FakeCfg({"engine": {"zero_copy": True}})
        zd = E.mpv_options(es[SLOT_I], zcfg, 1, self.sysroot, self.devroot)
        self.assertEqual(zd["hwdec"], "vaapi")          # explicit opt-in
        self.assertEqual(zd["vaapi-device"], disp["vaapi-device"])
        zo = E.mpv_options(es[SLOT_B], zcfg, 1, self.sysroot, self.devroot)
        self.assertEqual(zo["hwdec"], "vaapi")

    def test_every_gpu_engine_id_maps_to_vaapi_copy(self):
        # R2-P1 gate: every GPU engine id (auto, pinned slots, vulkan
        # variants) maps to hwdec='vaapi-copy' by default and to 'vaapi'
        # with engine.zero_copy=true; cpu stays 'no'.
        es = self.engines(vulkan=lambda: VULKAN)
        zcfg = FakeCfg({"engine": {"zero_copy": True}})
        gpu_seen = set()
        for e in es:
            if e.kind == "cpu":
                self.assertEqual(E.engine_hwdec_mode(e), "no")
                self.assertEqual(E.engine_hwdec_mode(e, zcfg), "no")
                continue
            gpu_seen.add(e.kind)
            self.assertEqual(E.engine_hwdec_mode(e), "vaapi-copy", e.id)
            self.assertEqual(E.engine_hwdec_mode(e, zcfg), "vaapi", e.id)
        self.assertEqual(gpu_seen, {"auto", "vaapi", "vulkan"})

    def test_vulkan_exact_device_name(self):
        es = self.engines(vulkan=lambda: VULKAN)
        vk = next(e for e in es if e.id == f"{SLOT_A}:vulkan")
        o = E.mpv_options(vk, None, 1, self.sysroot, self.devroot,
                          vulkan=lambda: VULKAN)
        # R2-P1: decode requests the copy mode that engages (§5.3's exact
        # vulkan-device selection string must still be emitted verbatim).
        self.assertEqual(o["hwdec"], "vaapi-copy")
        self.assertEqual(o["vulkan-device"], "RADV NAVI48 (7551)")
        zcfg = FakeCfg({"engine": {"zero_copy": True}})
        oz = E.mpv_options(vk, zcfg, 1, self.sysroot, self.devroot,
                           vulkan=lambda: VULKAN)
        self.assertEqual(oz["hwdec"], "vaapi")
        self.assertEqual(oz["vulkan-device"], "RADV NAVI48 (7551)")

    def test_cpu_hwdec_and_thread_formula(self):
        cpu = next(e for e in self.engines() if e.id == "cpu")
        nproc = os.cpu_count()
        o = E.mpv_options(cpu, None, 3, self.sysroot, self.devroot)
        self.assertEqual(o["hwdec"], "no")
        self.assertEqual(int(o["vd-lavc-threads"]),
                         max(1, (nproc - 2) // 3))
        cfg = FakeCfg({"engine": {"cpu_threads": 5}})
        o2 = E.mpv_options(cpu, cfg, 3, self.sysroot, self.devroot)
        self.assertEqual(o2["vd-lavc-threads"], "5")

    def test_init_options_headless_strips_devices(self):
        cfg = FakeCfg({"engine": {"decoder": SLOT_A},
                       "playback": {"keep_open": True},
                       "mpv": {"options": {"hwdec": "no",
                                           "script": "evil.lua"}}})
        o = E.mpv_init_options(cfg, True, 1, self.sysroot, self.devroot,
                               engines=self.engines())
        self.assertEqual(o["vo"], "libmpv")          # pinned §0.2
        self.assertEqual(o["gpu-api"], "opengl")
        self.assertEqual(o["hwdec"], "no")           # R13 headless
        self.assertNotIn("vaapi-device", o)
        self.assertNotIn("script", o)                # R12 sanitizer

    def test_init_options_cpu_engine(self):
        cfg = FakeCfg({"engine": {"decoder": "cpu", "cpu_threads": 0}})
        o = E.mpv_init_options(cfg, False, 2, self.sysroot, self.devroot,
                               engines=self.engines())
        self.assertEqual(o["hwdec"], "no")
        self.assertIn("vd-lavc-threads", o)


class RendererOverride(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.get(k)
                       for k in ("DRI_PRIME", "REELSEE_PRIME_APPLIED")}
        for k in self._saved:
            os.environ.pop(k, None)
        self.addCleanup(self._restore)

    def _restore(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    @staticmethod
    def _cfg(val):
        return FakeCfg({"engine": {"renderer_override": val}})

    def test_auto_is_noop(self):
        calls = []
        orig = os.execv
        os.execv = lambda *a: calls.append(a)        # type: ignore
        try:
            E.apply_renderer_override(self._cfg("auto"))
        finally:
            os.execv = orig                          # type: ignore
        self.assertEqual(calls, [])

    def test_invalid_ignored(self):
        calls = []
        orig = os.execv
        os.execv = lambda *a: calls.append(a)        # type: ignore
        try:
            E.apply_renderer_override(self._cfg("banana"))
        finally:
            os.execv = orig                          # type: ignore
        self.assertEqual(calls, [])
        self.assertNotIn("DRI_PRIME", os.environ)

    def test_mismatch_execs_once(self):
        calls = []
        orig = os.execv
        os.execv = lambda *a: calls.append(a)        # type: ignore
        try:
            E.apply_renderer_override(self._cfg("2"))
        finally:
            os.execv = orig                          # type: ignore
            for k in ("DRI_PRIME", "REELSEE_PRIME_APPLIED"):
                os.environ.pop(k, None)
        self.assertEqual(calls, [(sys.executable,
                                  [sys.executable] + sys.argv)])
        # env already matches (guard set before exec) → a second call is a
        # no-op — the REELSEE_PRIME_APPLIED loop guard
        calls2 = []
        os.execv = lambda *a: calls2.append(a)       # type: ignore
        try:
            E.apply_renderer_override(self._cfg("2"))
        finally:
            os.execv = orig                          # type: ignore

    def test_true_subprocess_reexec(self):
        with tempfile.TemporaryDirectory(dir=str(REPO / "scratch")) as d:
            os.makedirs(os.path.join(d, "cfg"))
            with open(os.path.join(d, "cfg", "config.json"), "w") as fh:
                json.dump({"engine": {"renderer_override": "1"}}, fh)
            env = dict(os.environ)
            env["REELSEE_CONFIG_DIR"] = os.path.join(d, "cfg")
            env["PYTHONPATH"] = str(_bootstrap.SRC)
            env.pop("DRI_PRIME", None)
            env.pop("REELSEE_PRIME_APPLIED", None)
            code = ("import config, engine\n"
                    "cfg = config.Config()\n"
                    "print('MARK', flush=True)\n"
                    "engine.apply_renderer_override(cfg)\n"
                    "print('AFTER', flush=True)\n")
            probe = os.path.join(d, "probe.py")
            with open(probe, "w") as fh:
                fh.write(code)
            r = subprocess.run([sys.executable, probe], env=env,
                               capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr)
            # parent prints MARK, re-execs; child (env already matches)
            # prints MARK + AFTER exactly once more
            self.assertEqual(r.stdout.split(), ["MARK", "MARK", "AFTER"])


class ProbeAndReadout(TreeCase):
    def test_probe_cache_roundtrip_and_ttl(self):
        path = os.path.join(self._tmp.name, "engines.json")
        now = 1_700_000_000.0
        c = E.ProbeCache(path)
        key = E.probe_key(SLOT_A, "vaapi", "h264")
        self.assertEqual(key, f"{SLOT_A}|vaapi|h264")
        c.set(key, True, now=now)
        self.assertTrue(E.ProbeCache(path).get(key, now=now + 3600))
        self.assertIsNone(E.ProbeCache(path).get(key, now=now + 8 * 86400))

    def test_probe_cache_absent_is_unverified_not_failed(self):
        c = E.ProbeCache(os.path.join(self._tmp.name, "missing.json"))
        self.assertIsNone(c.get("0000:00:00.0|vaapi|h264"))

    def test_readout_uses_snapshot_only(self):
        es = self.engines()
        txt = E.readout_text(es, SLOT_I, {"hwdec-current": "vaapi"})
        self.assertIn("Renderer: auto", txt)
        self.assertIn("Decoder: vaapi (renderD130)", txt)
        self.assertIn("hwdec-current: vaapi", txt)
        cpu = next(e for e in es if e.id == "cpu")
        txt2 = E.readout_text(es, "cpu", {}, "1")
        self.assertIn("Renderer: DRI_PRIME=1", txt2)
        self.assertIn("Decoder: — (sw)", txt2)
        self.assertIsInstance(cpu, E.Engine)


if __name__ == "__main__":
    unittest.main()
