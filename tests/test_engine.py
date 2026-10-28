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
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

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
    """cards: list of dicts(card, slot, driver, pci_id, render).

    Topology is modelled because R16-D/E selects the decode card partly from
    it: `discrete` (default True) puts the card behind a PCIe root port +
    switch (3 levels below pci0000:00, like the two R9700s), otherwise off a
    chipset function (2 levels, like the Raphael iGPU); `vram` sets
    mem_info_vram_total, the second discrete signal."""
    sysroot = Path(root) / "sys"
    devroot = Path(root) / "dev"
    (sysroot / "class" / "drm").mkdir(parents=True)
    for c in cards:
        chain = ("0000:00:01.1", "0000:01:00.0", "0000:02:00.0") \
            if c.get("discrete", True) else ("0000:00:08.1",)
        dev = sysroot.joinpath("devices", "pci0000:00", *chain, c["slot"])
        dev.mkdir(parents=True)
        card = sysroot / "class" / "drm" / c["card"]
        card.mkdir()
        (card / "device").symlink_to(os.path.relpath(dev, card))
        uev = [f"DRIVER={c['driver']}", f"PCI_ID={c['pci_id']}",
               f"PCI_SLOT_NAME={c['slot']}"]
        (dev / "uevent").write_text("\n".join(uev) + "\n")
        (dev / "mem_info_vram_total").write_text(
            str(c.get("vram", 34208743424 if c.get("discrete", True)
                        else 2147483648)) + "\n")
        if c.get("boot_vga"):
            # the iGPU is the boot device on a real rig (realism only — no
            # production code reads it; the fixture mirrors /sys faithfully)
            (dev / "boot_vga").write_text("1\n")
        if c.get("vram_unreadable"):
            (dev / "mem_info_vram_total").unlink()
        if c.get("render"):
            (dev / "drm").mkdir()
            (dev / "drm" / c["render"]).mkdir()
    for conn_spec in connectors:
        card, conn, status = conn_spec[:3]
        cdir = sysroot / "class" / "drm" / f"{card}-{conn}"
        cdir.mkdir()
        (cdir / "status").write_text(status + "\n")
        # R16-E §E.1: `enabled` is a SEPARATE sysfs file from `status`; a
        # 3-element spec writes it so the union rule is testable.
        if len(conn_spec) > 3:
            (cdir / "enabled").write_text(conn_spec[3] + "\n")
    for slot, node in (bypath or {}).items():
        bp = devroot / "dri" / "by-path"
        bp.mkdir(parents=True, exist_ok=True)   # several nodes share the dir
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
    # card3 is the chip's integrated graphics: off a chipset function, 2 GiB
    # dedicated VRAM (the reference rig's Raphael, 1002:13c0).
    dict(card="card3", slot=SLOT_I, driver="amdgpu", pci_id="1002:13c0",
         render="renderD130", discrete=False),
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
        # R16-C superseded the old `(cores - 2) // n` split: the budget is
        # threads_for(n, cores) (its own truth table is tests/test_threads.py)
        # and a SINGLE window gets no option at all — lavc's own default.
        self.assertEqual(int(o["vd-lavc-threads"]), E.threads_for(3, nproc))
        self.assertGreater(int(o["vd-lavc-threads"]), 1)
        o1 = E.mpv_options(cpu, None, 1, self.sysroot, self.devroot)
        self.assertEqual(o1["hwdec"], "no")
        self.assertNotIn("vd-lavc-threads", o1)
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


class DecodePlan(unittest.TestCase):
    """R16-D/E (ROUND18-corrected): automatic decode selection is
    INTEGRATED-or-CPU, never a dGPU.

    The fixture is the reference rig: two R9700s that DRIVE THE DISPLAYS
    (they carry the compositor and the inference workloads) and the iGPU.
    The ONLY gate on eligibility is discrete-vs-integrated topology + VRAM
    (`discrete_gpu_slot`); display connectors are irrelevant — on the
    operator's rig the iGPU itself carries the monitors (see
    RealRigRound18 below, which reproduces that rig and is the regression
    test for the connector-clause bug)."""

    RIG_CONNECTED = [("card1", "DP-1", "connected"),
                     ("card1", "DP-2", "connected"),
                     ("card2", "DP-1", "connected"),
                     ("card3", "HDMI-A-1", "disconnected")]

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(dir=str(REPO / "scratch"))
        self.sysroot, self.devroot = build_tree(
            self._tmp.name, THREE_CARDS, self.RIG_CONNECTED,
            {SLOT_A: "/dev/dri/renderD128", SLOT_B: "/dev/dri/renderD129",
             SLOT_I: "/dev/dri/renderD130"})
        self.addCleanup(self._tmp.cleanup)
        self.engines = E.enumerate_engines(self.sysroot, self.devroot,
                                           lspci=LSPCI.get,
                                           vulkan=lambda: {})

    def test_integrated_slots_are_the_igpu_only(self):
        self.assertEqual(E.integrated_gpu_slots(self.sysroot), [SLOT_I])

    def test_pick_decode_node_is_the_integrated_render_node(self):
        self.assertEqual(E.pick_decode_node(self.sysroot, self.devroot),
                         os.path.join(self.devroot, "dri", "renderD130"))

    def test_last_pci_slot_wins_among_integrated_cards(self):
        """§E.1: among qualifying (integrated, non-discrete) nodes prefer the
        LAST PCI slot — the skill-verified position of the iGPU on this rig."""
        cards = THREE_CARDS + [dict(card="card4", slot="0000:1a:00.0",
                                    driver="amdgpu", pci_id="1002:13c0",
                                    render="renderD131", discrete=False)]
        conn = self.RIG_CONNECTED + [("card4", "DP-1", "disconnected")]
        root2 = self._tmp.name + "2"
        sysroot, devroot = build_tree(root2, cards, conn,
                                      {"0000:1a:00.0": "/dev/dri/renderD131"})
        self.addCleanup(shutil.rmtree, root2, True)
        self.assertEqual(E.integrated_gpu_slots(sysroot),
                         [SLOT_I, "0000:1a:00.0"])
        self.assertEqual(E.pick_decode_node(sysroot, devroot),
                         os.path.join(devroot, "dri", "renderD131"))

    def test_headless_dgpu_is_still_never_selected(self):
        """The connector rule ALONE inverted on a headless boot — and the rule
        is now gone, so the discrete guard is the ONLY thing standing between
        the plan and the R9700s.  Unplug both R9700s (zero connected
        connectors, exactly what a headless boot looks like) and the plan
        still names only the iGPU: topology depth and 32 GiB VRAM exclude the
        dGPUs regardless of connectors (§D.3)."""
        conn = [("card1", "DP-1", "disconnected"),
                ("card2", "DP-1", "disconnected"),
                ("card3", "HDMI-A-1", "connected")]
        root5 = self._tmp.name + "5"
        sysroot, devroot = build_tree(root5, THREE_CARDS, conn,
                                      {SLOT_A: "/dev/dri/renderD128",
                                       SLOT_B: "/dev/dri/renderD129",
                                       SLOT_I: "/dev/dri/renderD130"})
        self.addCleanup(shutil.rmtree, root5, True)
        self.assertEqual(E.integrated_gpu_slots(sysroot), [SLOT_I])  # not 03/07
        self.assertEqual(E.pick_decode_node(sysroot, devroot),
                         os.path.join(devroot, "dri", "renderD130"))
        engines = E.enumerate_engines(sysroot, devroot, lspci=LSPCI.get,
                                      vulkan=lambda: {})
        cpu = next(e for e in engines if e.kind == "cpu")
        gpu = next(e for e in engines if e.pci == SLOT_I and e.kind == "vaapi")
        self.assertEqual(E.decode_plan(4, 2, engines, sysroot),
                         [gpu, gpu, cpu, cpu])

    def test_discrete_guard_signals(self):
        self.assertTrue(E.discrete_gpu_slot(SLOT_A, self.sysroot))
        self.assertTrue(E.discrete_gpu_slot(SLOT_B, self.sysroot))
        self.assertFalse(E.discrete_gpu_slot(SLOT_I, self.sysroot))
        self.assertTrue(E.discrete_gpu_slot("0000:99:00.0", self.sysroot),
                        "an unknown slot must fail closed (discrete)")

    def test_discrete_guard_fails_closed_without_vram(self):
        """No mem_info_vram_total ⇒ assume discrete ⇒ never auto-selected."""
        p = Path(self.sysroot) / "devices" / "pci0000:00" / "0000:00:08.1" \
            / SLOT_I / "mem_info_vram_total"
        p.unlink()
        self.assertTrue(E.discrete_gpu_slot(SLOT_I, self.sysroot))
        self.assertEqual(E.integrated_gpu_slots(self.sysroot), [])

    def test_discrete_guard_fails_closed_on_big_vram(self):
        """An integrated-looking card that reports dGPU-sized dedicated memory
        is treated as discrete (2 GiB on the rig's Raphael; 8 GiB is not)."""
        p = Path(self.sysroot) / "devices" / "pci0000:00" / "0000:00:08.1" \
            / SLOT_I / "mem_info_vram_total"
        p.write_text(str(8 * 1024 ** 3) + "\n")
        self.assertTrue(E.discrete_gpu_slot(SLOT_I, self.sysroot))

    def test_plan_is_gpu_up_to_capacity_then_cpu(self):
        gpu = E.pick_decode_engine(self.engines, self.sysroot)
        cpu = next(e for e in self.engines if e.kind == "cpu")
        self.assertEqual(gpu.pci, SLOT_I)
        for n, want in [(1, [gpu]),
                        (2, [gpu, gpu]),
                        (3, [gpu, gpu, cpu]),
                        (5, [gpu, gpu, cpu, cpu, cpu])]:
            with self.subTest(n_windows=n):
                self.assertEqual(E.decode_plan(n, 2, self.engines,
                                               self.sysroot), want)

    def test_capacity_zero_is_cpu_for_everyone(self):
        cpu = next(e for e in self.engines if e.kind == "cpu")
        self.assertEqual(E.decode_plan(3, 0, self.engines, self.sysroot),
                         [cpu] * 3)

    def test_capacity_default_is_two_and_garbage_is_the_default(self):
        gpu = E.pick_decode_engine(self.engines, self.sysroot)
        self.assertEqual(E.DECODE_CAPACITY, 2)
        self.assertEqual(E.decode_plan(3, None, self.engines, self.sysroot),
                         [gpu, gpu, next(e for e in self.engines
                                         if e.kind == "cpu")])
        self.assertEqual(E.decode_plan(3, "junk", self.engines, self.sysroot),
                         E.decode_plan(3, E.DECODE_CAPACITY, self.engines,
                                        self.sysroot))

    def test_degenerate_window_counts_are_empty_not_a_crash(self):
        self.assertEqual(E.decode_plan(0, 2, self.engines, self.sysroot), [])
        self.assertEqual(E.decode_plan(-4, 2, self.engines, self.sysroot), [])
        self.assertEqual(E.decode_plan(None, 2, self.engines, self.sysroot), [])

    def test_never_a_discrete_cards_node(self):
        """The hard rule (§D.3): automatic selection may choose the iGPU or
        CPU — it may never choose a dGPU, even though both R9700s are
        enumerated as perfectly good VAAPI engines."""
        dgpu_nodes = {os.path.join(self.devroot, "dri", "renderD128"),
                      os.path.join(self.devroot, "dri", "renderD129")}
        dgpu_slots = {SLOT_A, SLOT_B}
        for n in (1, 2, 3, 8):
            plan = E.decode_plan(n, 2, self.engines, self.sysroot)
            for e in plan:
                self.assertNotIn(e.pci, dgpu_slots, f"{e} in plan")
                self.assertNotIn(e.render_node, dgpu_nodes, f"{e} in plan")

    def test_no_integrated_card_means_cpu_for_everyone(self):
        """Truth table (ROUND18 §B.3): two discrete cards, no iGPU — the plan
        is CPU for all windows — NOT the display GPU.  (The old version of
        this test used THREE_CARDS with every connector connected; under the
        corrected rule the connected iGPU is eligible, so that fixture no
        longer means "nothing qualifies" — the discrete-only fixture does.)"""
        cards = [c for c in THREE_CARDS if c["slot"] != SLOT_I]
        conn = [("card1", "DP-1", "connected"),
                ("card2", "DP-1", "connected")]
        root3 = self._tmp.name + "3"
        sysroot, devroot = build_tree(root3, cards, conn, {})
        self.addCleanup(shutil.rmtree, root3, True)
        engines = E.enumerate_engines(sysroot, devroot, lspci=LSPCI.get,
                                      vulkan=lambda: {})
        self.assertIsNone(E.pick_decode_engine(engines, sysroot))
        cpu = next(e for e in engines if e.kind == "cpu")
        self.assertEqual(E.decode_plan(4, 2, engines, sysroot), [cpu] * 4)

    def test_no_gpu_at_all_is_cpu_for_everyone(self):
        """A machine with no GPU enumerated: the plan still resolves (CPU)
        instead of falling back to the 'auto' engine."""
        root4 = self._tmp.name + "4"
        sysroot, devroot = build_tree(root4, [], [], {})
        self.addCleanup(shutil.rmtree, root4, True)
        engines = E.enumerate_engines(sysroot, devroot, lspci=lambda s: None,
                                      vulkan=lambda: {})
        plan = E.decode_plan(3, 2, engines, sysroot)
        self.assertEqual([e.kind for e in plan], ["cpu", "cpu", "cpu"])

    def test_registry_auto_choice_is_the_plan(self):
        """WindowRegistry.pick_engine("auto") used to round-robin over every
        GPU; it now asks the plan, so the second window still lands on the
        iGPU and the third on CPU."""
        import registry as R
        r = R.WindowRegistry()
        gpu = E.pick_decode_engine(self.engines, self.sysroot)
        cpu = next(e for e in self.engines if e.kind == "cpu")
        got = []
        for i in range(3):
            got.append(r.pick_engine("auto", self.engines, E.select_engine,
                                     capacity=2, sysroot=self.sysroot))
            # distinct targets: SimpleNamespace compares by value, and the
            # registry dedupes by equality — identical fakes would collapse
            # into one entry and hide the counting this test is about.
            r.add(SimpleNamespace(_closing=False, target=f"clip{i}.mp4"))
        self.assertEqual(got, [gpu, gpu, cpu])

    def test_registry_pinned_decoder_still_overrides(self):
        import registry as R
        r = R.WindowRegistry()
        e = r.pick_engine("cpu", self.engines, E.select_engine, capacity=2,
                          sysroot=self.sysroot)
        self.assertEqual(e.kind, "cpu")
        e2 = r.pick_engine(SLOT_A, self.engines, E.select_engine, capacity=2,
                           sysroot=self.sysroot)
        self.assertEqual(e2.pci, SLOT_A)      # an explicit choice is honoured


class VaapiDevice(unittest.TestCase):
    """R16-E §E.1/§E.2/§E.3 (ROUND18-corrected): the decode NODE is an
    independent knob.

    Automatic selection may name the INTEGRATED (iGPU-class) node — display-
    bearing or not — or nothing at all, never a discrete dGPU;
    `playback.vaapi_device` pins any node as the documented escape hatch.
    Cross-card decode always copies frames; direct `vaapi` needs same-card
    AND the zero_copy opt-in (R2-P1 measured zero-copy never engaging under
    the embedded GL path)."""

    RIG = DecodePlan.RIG_CONNECTED
    BYPATH = {SLOT_A: "/dev/dri/renderD128", SLOT_B: "/dev/dri/renderD129",
              SLOT_I: "/dev/dri/renderD130"}

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(dir=str(REPO / "scratch"))
        self.sysroot, self.devroot = build_tree(
            self._tmp.name, THREE_CARDS, self.RIG, dict(self.BYPATH))
        self.addCleanup(self._tmp.cleanup)
        self.engines = E.enumerate_engines(self.sysroot, self.devroot,
                                           lspci=LSPCI.get,
                                           vulkan=lambda: {})
        self.node = lambda n: os.path.join(self.devroot, "dri", n)

    def cfg(self, **playback):
        d = {"engine": {"decoder": "auto"}, "playback": dict(playback)}
        return FakeCfg(d)

    def init_opts(self, cfg=None, **kw):
        return E.mpv_init_options(cfg or self.cfg(), False, 1,
                                  self.sysroot, self.devroot,
                                  engines=self.engines, **kw)

    # ------------------------------------------------------- §E.1 auto rule
    def test_auto_resolves_to_the_integrated_node(self):
        self.assertEqual(E.resolve_decode_node(self.cfg(), self.sysroot,
                                               self.devroot),
                         self.node("renderD130"))

    def test_connectors_do_not_gate_eligibility(self):
        """ROUND18: display connectors are NOT part of the eligibility test.
        A card with `status=disconnected` but `enabled` is still
        display-bearing (the union rule keeps `display_bearing_cards()`
        honest for labels/probes) — and it is still ELIGIBLE for automatic
        decode when it is the iGPU, because on the operator's rig the iGPU is
        exactly that: the display-bearing card."""
        conn = [("card1", "DP-1", "disconnected", "disabled"),
                ("card2", "DP-1", "disconnected", "disabled"),
                ("card3", "HDMI-A-1", "disconnected", "enabled")]
        rootE = self._tmp.name + "e"
        sysroot, devroot = build_tree(rootE, THREE_CARDS, conn,
                                      dict(self.BYPATH))
        self.addCleanup(shutil.rmtree, rootE, True)
        self.assertEqual(E.display_bearing_cards(sysroot), {"card3"})
        self.assertEqual(E.integrated_gpu_slots(sysroot), [SLOT_I])
        self.assertEqual(E.pick_decode_node(sysroot, devroot),
                         os.path.join(devroot, "dri", "renderD130"))

    def test_auto_is_none_when_no_card_qualifies(self):
        """§E.1's escape: no iGPU-class node ⇒ None ⇒ CPU for everyone, never
        a discrete dGPU.  Fixture: two discrete cards only (the iGPU absent),
        both driving displays."""
        cards = [c for c in THREE_CARDS if c["slot"] != SLOT_I]
        conn = [("card1", "DP-1", "connected"), ("card2", "DP-1", "connected")]
        rootE2 = self._tmp.name + "e2"
        sysroot, devroot = build_tree(rootE2, cards, conn,
                                      {SLOT_A: "/dev/dri/renderD128",
                                       SLOT_B: "/dev/dri/renderD129"})
        self.addCleanup(shutil.rmtree, rootE2, True)
        self.assertIsNone(E.resolve_decode_node(self.cfg(), sysroot, devroot))

    # ------------------------------------------------------- §E.2 pin rule
    def test_pin_wins_and_may_name_a_dgpu(self):
        """The escape hatch is explicit: a human naming a dGPU node gets it."""
        pin = self.node("renderD128")
        self.assertEqual(E.resolve_decode_node(self.cfg(vaapi_device=pin),
                                               self.sysroot, self.devroot),
                         pin)
        self.assertEqual(E.vaapi_device_pin(self.cfg(vaapi_device=" ")), "")
        self.assertEqual(E.vaapi_device_pin(self.cfg(vaapi_device=pin)), pin)

    # -------------------------------------------------- §E.3 copy-vs-direct
    def test_copy_is_mandatory_when_decode_is_not_the_render_gpu(self):
        self.assertEqual(E.render_gpu_node(self.sysroot, self.devroot),
                         self.node("renderD128"))      # card1 drives DP-1/DP-2
        self.assertEqual(
            E.hwdec_mode_for_node(self.node("renderD130"), self.cfg(),
                                  self.sysroot, self.devroot),
            "vaapi-copy")
        z = self.cfg()
        z.d["engine"]["zero_copy"] = True
        self.assertEqual(
            E.hwdec_mode_for_node(self.node("renderD130"), z,
                                  self.sysroot, self.devroot),
            "vaapi-copy", "cross-GPU copy is not opt-in")

    def test_direct_vaapi_needs_same_node_and_the_opt_in(self):
        z = self.cfg()
        z.d["engine"]["zero_copy"] = True
        self.assertEqual(
            E.hwdec_mode_for_node(self.node("renderD128"), z,
                                  self.sysroot, self.devroot),
            "vaapi", "same card + opt-in may go zero-copy")
        self.assertEqual(
            E.hwdec_mode_for_node(self.node("renderD128"), self.cfg(),
                                  self.sysroot, self.devroot),
            "vaapi-copy",
            "R2-P1: zero-copy never engages here, so it stays opt-in")
        self.assertEqual(E.hwdec_mode_for_node(None, z, self.sysroot,
                                               self.devroot), "no")

    # ------------------------------------------------- plumbing into mpvcore
    def test_init_options_auto_pins_the_headless_node(self):
        o = self.init_opts()
        self.assertEqual(o["vaapi-device"], self.node("renderD130"))
        self.assertEqual(o["hwdec"], "vaapi-copy")

    def test_init_options_auto_is_cpu_when_nothing_qualifies(self):
        """Truth table (ROUND18 §B.3): discrete cards only, no iGPU ⇒
        mpv_init_options emits hwdec="no" and DROPS vaapi-device."""
        cards = [c for c in THREE_CARDS if c["slot"] != SLOT_I]
        conn = [("card1", "DP-1", "connected"), ("card2", "DP-1", "connected")]
        rootE3 = self._tmp.name + "e3"
        sysroot, devroot = build_tree(rootE3, cards, conn,
                                      {SLOT_A: "/dev/dri/renderD128",
                                       SLOT_B: "/dev/dri/renderD129"})
        self.addCleanup(shutil.rmtree, rootE3, True)
        engines = E.enumerate_engines(sysroot, devroot, lspci=LSPCI.get,
                                      vulkan=lambda: {})
        o = E.mpv_init_options(self.cfg(), False, 2, sysroot, devroot,
                               engines=engines)
        self.assertEqual(o["hwdec"], "no")
        self.assertNotIn("vaapi-device", o)
        self.assertNotIn("vulkan-device", o)

    def test_init_options_never_auto_names_a_discrete_gpu_node(self):
        """The §D.3 hard rule, re-asserted at the mpv option boundary: with
        every plausible fixture, an automatic run must not pass a discrete
        card's node to --vaapi-device."""
        banned = {self.node("renderD128"), self.node("renderD129")}
        fixtures = [
            self.RIG,
            [("card1", "DP-1", "disconnected"),
             ("card2", "DP-1", "disconnected"),
             ("card3", "HDMI-A-1", "connected")],      # headless boot
            [("card1", "DP-1", "connected"), ("card2", "DP-1", "connected"),
             ("card3", "HDMI-A-1", "connected")],      # the operator's rig
        ]
        for i, conn in enumerate(fixtures):
            rootN = self._tmp.name + f"n{i}"
            sysroot, devroot = build_tree(rootN, THREE_CARDS, conn,
                                          dict(self.BYPATH))
            self.addCleanup(shutil.rmtree, rootN, True)
            engines = E.enumerate_engines(sysroot, devroot, lspci=LSPCI.get,
                                          vulkan=lambda: {})
            o = E.mpv_init_options(self.cfg(), False, 1, sysroot, devroot,
                                   engines=engines)
            self.assertNotIn(o.get("vaapi-device"), banned, conn)

    def test_init_options_pin_sets_node_and_copy_mode(self):
        pin = self.node("renderD129")
        o = self.init_opts(self.cfg(vaapi_device=pin))
        self.assertEqual(o["vaapi-device"], pin)
        self.assertEqual(o["hwdec"], "vaapi-copy")

    def test_pinned_engine_keeps_its_own_node(self):
        """An engine pinned to a PCI slot is an explicit human choice; §E must
        not silently rewrite its node to the iGPU."""
        vk = next(e for e in self.engines if e.pci == SLOT_B
                  and e.kind == "vaapi")
        o = self.init_opts(engine_override=vk)
        self.assertEqual(o["vaapi-device"], self.node("renderD129"))

    def test_pinned_node_overrides_which_engine_the_plan_calls_the_gpu(self):
        """§E.2 through the plan: the pin picks the CARD, capacity still picks
        how many windows decode on it.  Without the pin this fixture — two
        discrete cards, no iGPU — is CPU for everyone."""
        cards = [c for c in THREE_CARDS if c["slot"] != SLOT_I]
        conn = [("card1", "DP-1", "connected"), ("card2", "DP-1", "connected")]
        rootP = self._tmp.name + "pin"
        sysroot, devroot = build_tree(rootP, cards, conn,
                                      {SLOT_A: "/dev/dri/renderD128",
                                       SLOT_B: "/dev/dri/renderD129"})
        self.addCleanup(shutil.rmtree, rootP, True)
        engines = E.enumerate_engines(sysroot, devroot, lspci=LSPCI.get,
                                      vulkan=lambda: {})
        cpu = next(e for e in engines if e.kind == "cpu")
        self.assertEqual(E.decode_plan(3, 2, engines, sysroot), [cpu] * 3)
        pin = os.path.join(devroot, "dri", "renderD129")
        gpu = E.engine_for_node(pin, engines)
        self.assertEqual(gpu.pci, SLOT_B)          # the enumerated card
        self.assertEqual(E.decode_plan(3, 2, engines, sysroot,
                                       gpu_engine=gpu),
                         [gpu, gpu, cpu])

    def test_engine_for_node_synthesises_an_unenumerated_node(self):
        e = E.engine_for_node("/dev/dri/renderD999", self.engines)
        self.assertEqual((e.kind, e.render_node), ("vaapi",
                                                   "/dev/dri/renderD999"))
        self.assertEqual(E.engine_for_node(self.node("renderD130"),
                                           self.engines).pci, SLOT_I)

    def test_headless_still_forces_cpu_decode(self):
        """R13's CI invariant outranks §E: even a pinned node is stripped."""
        o = E.mpv_init_options(self.cfg(vaapi_device=self.node("renderD130")),
                               True, 1, self.sysroot, self.devroot,
                               engines=self.engines)
        self.assertEqual(o["hwdec"], "no")
        self.assertNotIn("vaapi-device", o)


class RealRigRound18(unittest.TestCase):
    """ROUND18 truth table: automatic decode lands ONLY on the integrated GPU
    and falls back to CPU — never on a discrete GPU.  Connectors are not part
    of the mandate; the operator's rig (the Granite Ridge iGPU carrying the
    displays) is case 1 and the regression test for the connector-clause bug.

    Every case materialises a tempdir sysroot with the real path layout:
    cards, connector status/enabled files, mem_info_vram_total, PCIe topology
    depth (root-port/switch chains vs chipset function), render nodes."""

    # The operator's rig, exactly as reported: card1/card2 = Navi 48 dGPUs
    # (PCIe depth >= 3, 32 GiB dedicated VRAM); card3 = Granite Ridge iGPU
    # (depth 2, ~2 GiB) which carries the monitors — display_bearing_cards()
    # == ['card3'] on the live rig — and is the boot VGA.
    REAL_RIG_CARDS = [
        dict(card="card1", slot=SLOT_A, driver="amdgpu", pci_id="1002:7551",
             render="renderD128", discrete=True, vram=32 * 1024 ** 3),
        dict(card="card2", slot=SLOT_B, driver="amdgpu", pci_id="1002:7551",
             render="renderD129", discrete=True, vram=32 * 1024 ** 3),
        dict(card="card3", slot=SLOT_I, driver="amdgpu", pci_id="1002:13c0",
             render="renderD130", discrete=False, vram=2 * 1024 ** 3,
             boot_vga=True),
    ]
    REAL_RIG_CONN = [("card1", "DP-1", "disconnected", "disabled"),
                     ("card1", "DP-2", "disconnected", "disabled"),
                     ("card2", "DP-1", "disconnected", "disabled"),
                     ("card3", "HDMI-A-1", "connected", "enabled")]
    REAL_RIG_BYPATH = {SLOT_A: "/dev/dri/renderD128",
                       SLOT_B: "/dev/dri/renderD129",
                       SLOT_I: "/dev/dri/renderD130"}

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(dir=str(REPO / "scratch"))
        self.addCleanup(self._tmp.cleanup)

    def tree(self, cards, conn, bypath, root="r"):
        return build_tree(self._tmp.name + root, cards, conn, bypath)

    def cfg(self, **playback):
        return FakeCfg({"engine": {"decoder": "auto"},
                        "playback": dict(playback)})

    def engines(self, sysroot, devroot):
        return E.enumerate_engines(sysroot, devroot, lspci=LSPCI.get,
                                   vulkan=lambda: {})

    # ---- case 1: THE REAL-RIG FIXTURE — regression test for this bug ------
    def test_real_rig_display_bearing_igpu_is_selected(self):
        """Before ROUND18 this fixture returned [] / None / hwdec="no": the
        connector clause disqualified the display-bearing iGPU and every
        window decoded on CPU.  After: card3's node, vaapi-copy."""
        sysroot, devroot = self.tree(self.REAL_RIG_CARDS, self.REAL_RIG_CONN,
                                     dict(self.REAL_RIG_BYPATH))
        # the operator's sysfs reading, reproduced: the iGPU carries the
        # displays...
        self.assertEqual(E.display_bearing_cards(sysroot), {"card3"})
        # ...and it is still the only eligible card (it is not discrete),
        # while the R9700s are excluded by topology + VRAM, not connectors.
        self.assertEqual(E.integrated_gpu_slots(sysroot), [SLOT_I])
        want = os.path.join(devroot, "dri", "renderD130")
        self.assertEqual(E.pick_decode_node(sysroot, devroot), want)
        self.assertEqual(E.hwdec_mode_for_node(want, self.cfg(),
                                               sysroot, devroot),
                         "vaapi-copy")
        # ...and the plan names it for the first `capacity` windows.
        engines = self.engines(sysroot, devroot)
        gpu = next(e for e in engines if e.pci == SLOT_I and e.kind == "vaapi")
        cpu = next(e for e in engines if e.kind == "cpu")
        self.assertEqual(E.decode_plan(3, 2, engines, sysroot),
                         [gpu, gpu, cpu])

    # ---- case 2: headless iGPU + two discrete (old rule's case preserved) -
    def test_headless_igpu_still_picked(self):
        conn = [("card1", "DP-1", "connected"), ("card1", "DP-2", "connected"),
                ("card2", "DP-1", "connected"),
                ("card3", "HDMI-A-1", "disconnected")]
        sysroot, devroot = self.tree(self.REAL_RIG_CARDS, conn,
                                     dict(self.REAL_RIG_BYPATH), root="h")
        self.assertEqual(E.integrated_gpu_slots(sysroot), [SLOT_I])
        self.assertEqual(E.pick_decode_node(sysroot, devroot),
                         os.path.join(devroot, "dri", "renderD130"))

    # ---- case 3: two discrete, no iGPU -> None -> CPU plan ----------------
    def test_two_discrete_no_igpu_is_cpu_plan(self):
        cards = [c for c in self.REAL_RIG_CARDS if c["slot"] != SLOT_I]
        conn = [("card1", "DP-1", "connected"), ("card2", "DP-1", "connected")]
        sysroot, devroot = self.tree(
            cards, conn,
            {SLOT_A: "/dev/dri/renderD128", SLOT_B: "/dev/dri/renderD129"},
            root="d")
        self.assertEqual(E.integrated_gpu_slots(sysroot), [])
        self.assertIsNone(E.pick_decode_node(sysroot, devroot))
        o = E.mpv_init_options(self.cfg(), False, 2, sysroot, devroot,
                               engines=self.engines(sysroot, devroot))
        self.assertEqual(o["hwdec"], "no")
        self.assertNotIn("vaapi-device", o)

    # ---- case 4: iGPU hidden in firmware (not enumerated) -> CPU ----------
    def test_igpu_hidden_in_firmware_is_cpu(self):
        """The iGPU is present in the rig but disabled in firmware: it is not
        enumerated under /sys at all.  Nothing to select — CPU, and never one
        of the dGPUs."""
        cards = [c for c in self.REAL_RIG_CARDS if c["slot"] != SLOT_I]
        conn = [("card1", "DP-1", "connected"), ("card2", "DP-1", "connected")]
        sysroot, devroot = self.tree(
            cards, conn,
            {SLOT_A: "/dev/dri/renderD128", SLOT_B: "/dev/dri/renderD129"},
            root="f")
        self.assertNotIn(SLOT_I, E.sysfs_cards(sysroot))
        self.assertIsNone(E.resolve_decode_node(self.cfg(), sysroot, devroot))
        o = E.mpv_init_options(self.cfg(), False, 1, sysroot, devroot,
                               engines=self.engines(sysroot, devroot))
        self.assertEqual(o["hwdec"], "no")
        self.assertNotIn("vaapi-device", o)

    # ---- case 5: fail-closed — unreadable VRAM on the iGPU -> CPU ----------
    def test_unreadable_vram_on_igpu_fails_closed_to_cpu(self):
        cards = [dict(c) for c in self.REAL_RIG_CARDS]
        cards[2]["vram_unreadable"] = True
        sysroot, devroot = self.tree(cards, self.REAL_RIG_CONN,
                                     dict(self.REAL_RIG_BYPATH), root="u")
        self.assertTrue(E.discrete_gpu_slot(SLOT_I, sysroot),
                        "unreadable mem_info_vram_total must count as discrete")
        self.assertEqual(E.integrated_gpu_slots(sysroot), [])
        self.assertIsNone(E.pick_decode_node(sysroot, devroot))
        o = E.mpv_init_options(self.cfg(), False, 1, sysroot, devroot,
                               engines=self.engines(sysroot, devroot))
        self.assertEqual(o["hwdec"], "no")
        self.assertNotIn("vaapi-device", o)

    # ---- case 6: two integrated cards -> last PCI slot wins ----------------
    def test_two_integrated_cards_last_slot_wins(self):
        cards = self.REAL_RIG_CARDS + [
            dict(card="card4", slot="0000:1a:00.0", driver="amdgpu",
                 pci_id="1002:13c0", render="renderD131", discrete=False,
                 vram=2 * 1024 ** 3)]
        conn = self.REAL_RIG_CONN + [("card4", "DP-1", "connected", "enabled")]
        sysroot, devroot = self.tree(cards, conn,
                                     dict(self.REAL_RIG_BYPATH,
                                          **{"0000:1a:00.0":
                                             "/dev/dri/renderD131"}),
                                     root="t")
        self.assertEqual(E.integrated_gpu_slots(sysroot),
                         [SLOT_I, "0000:1a:00.0"])
        self.assertEqual(E.pick_decode_node(sysroot, devroot),
                         os.path.join(devroot, "dri", "renderD131"))

    # ---- case 7: pin overrides; blank pin vs no-node log line --------------
    def test_pin_overrides_everything(self):
        sysroot, devroot = self.tree(self.REAL_RIG_CARDS, self.REAL_RIG_CONN,
                                     dict(self.REAL_RIG_BYPATH), root="p")
        pin = os.path.join(devroot, "dri", "renderD128")   # a dGPU: allowed
        self.assertEqual(E.resolve_decode_node(self.cfg(vaapi_device=pin),
                                               sysroot, devroot), pin)
        o = E.mpv_init_options(self.cfg(vaapi_device=pin), False, 1,
                               sysroot, devroot,
                               engines=self.engines(sysroot, devroot))
        self.assertEqual(o["vaapi-device"], pin)
        self.assertEqual(o["hwdec"], "vaapi-copy")

    def test_blank_pin_is_auto_and_no_node_keeps_the_log_line(self):
        """Blank pin ("" / whitespace) means AUTO, which on the real rig
        resolves to the iGPU; a fixture with NO node at all must log the
        existing fallback line — the two cases must not collapse."""
        sysroot, devroot = self.tree(self.REAL_RIG_CARDS, self.REAL_RIG_CONN,
                                     dict(self.REAL_RIG_BYPATH), root="b")
        want = os.path.join(devroot, "dri", "renderD130")
        for blank in ("", "   "):
            self.assertEqual(E.vaapi_device_pin(self.cfg(vaapi_device=blank)),
                             "")
            o = E.mpv_init_options(self.cfg(vaapi_device=blank), False, 1,
                                   sysroot, devroot,
                                   engines=self.engines(sysroot, devroot))
            self.assertEqual(o.get("vaapi-device"), want, repr(blank))
        cards = [c for c in self.REAL_RIG_CARDS if c["slot"] != SLOT_I]
        conn = [("card1", "DP-1", "connected"), ("card2", "DP-1", "connected")]
        sysroot2, devroot2 = self.tree(
            cards, conn,
            {SLOT_A: "/dev/dri/renderD128", SLOT_B: "/dev/dri/renderD129"},
            root="b2")
        with self.assertLogs("reelsee.engine", level="INFO") as cm:
            o = E.mpv_init_options(self.cfg(vaapi_device=""), False, 1,
                                   sysroot2, devroot2,
                                   engines=self.engines(sysroot2, devroot2))
        self.assertEqual(o["hwdec"], "no")
        self.assertNotIn("vaapi-device", o)
        self.assertTrue(any("no headless decode GPU" in line
                            for line in cm.output), cm.output)

    # ---- case 8: wiring — mpv_init_options on the real rig -----------------
    def test_mpv_init_options_wiring_on_real_rig(self):
        sysroot, devroot = self.tree(self.REAL_RIG_CARDS, self.REAL_RIG_CONN,
                                     dict(self.REAL_RIG_BYPATH), root="w")
        o = E.mpv_init_options(self.cfg(), False, 1, sysroot, devroot,
                               engines=self.engines(sysroot, devroot))
        self.assertEqual(o.get("vaapi-device"),
                         os.path.join(devroot, "dri", "renderD130"))
        self.assertEqual(o.get("hwdec"), "vaapi-copy")


if __name__ == "__main__":
    unittest.main()
