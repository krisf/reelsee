"""Engine selection: GPU enumeration, hwdec chains, mpv option blocks.

P2 scope: the fixed embedded-render option block + runtime hwdec mapping +
renderer_override DRI_PRIME re-exec.  P6 adds the full Engine model and the
Appendix-B enumeration (by-path → sysfs → lspci → vulkan names → labels),
the §5.3 `mpv_options` mapping with its invariants, the engagement helper
and the engines.json capability cache.

EVERY filesystem-facing function takes a sysroot/devroot prefix so unit
tests run against fake fixtures (the sandbox has no /dev/dri).
"""
from __future__ import annotations

import glob
import json
import logging
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass

log = logging.getLogger("reelsee.engine")

#: options the embedded render mechanism REQUIRES — user config may not
#: override them (vo=gpu next SEGFAULTS the render API, §0.2)
PINNED_INIT_KEYS = frozenset({"vo", "gpu-api", "wid", "term", "terminal",
                              "config", "scripts", "script", "input-conf",
                              "include"})

#: R12 blocklist in its mpv-options form (palette blocklist lives in the
#: palette module; both are enforced).
BLOCKED_INIT_KEYS = frozenset({"script", "script-file", "load-script",
                               "input-conf", "config", "include"})

ENGINE_HWDEC = {   # documentation of the §5.3 decode mapping (P6 extends)
    # R2-P1 (VERIFY-FINDINGS B1): zero-copy vaapi does NOT engage in the
    # embedded EGL/GL path (gpu-api=opengl) — pinning a GPU silently decoded
    # on CPU.  EVERY GPU engine therefore defaults to `vaapi-copy`, which
    # the operator verified engages on all GPUs of this box; engine.zero_copy
    # (config, default false) opts back into zero-copy `vaapi` on render
    # paths where it can engage (may fall back — the R2-P2 engagement
    # watcher self-corrects if it does not).
    "cpu": "no",
    "vaapi": "vaapi-copy",      # engine.zero_copy=true → "vaapi"
    "vulkan": "vaapi-copy",     # ditto (vulkan-device selection still exact)
    "auto": "vaapi-copy",       # display-GPU node pinned when resolvable
}


def fixed_init_options() -> dict[str, str]:
    """The §0/P0-proven embedding block (spike_render.py).  vo=libmpv +
    gpu-api=opengl are mandatory; vo=gpu-next SEGFAULTS (§0.2)."""
    return {
        "vo": "libmpv",
        "gpu-api": "opengl",
        "config": "no",            # never read ~/.config/mpv
        "terminal": "no",
        "msg-level": "all=warn",
        "osc": "no",
        "osd-level": "0",
    }


def mpv_init_options(cfg, headless: bool, n_windows: int = 1,
                     sysroot: str = "/sys", devroot: str = "/dev",
                     engines: "list[Engine] | None" = None,
                     engine_override: "Engine | None" = None) -> dict[str, str]:
    """Full init option dict for MpvCore: fixed block + engine block
    (§5.3) + config mpv.options (sanitized: PINNED + R12 keys dropped).
    engine_override (P8 round-robin) replaces the config-selected engine."""
    opts = fixed_init_options()
    # engine.decoder: "auto" | "cpu" | "<pci>[:vulkan]" (schema §5.2)
    if engines is None:
        engines = enumerate_engines(sysroot, devroot)
    sel = engine_override or select_engine(cfg.get("engine.decoder", "auto"),
                                           engines)
    opts.update(mpv_options(sel, cfg, n_windows, sysroot, devroot))
    if bool(cfg.get("playback.keep_open", True)):
        opts["keep-open"] = "yes"
    if headless:                            # R13: never touch real hw in CI
        opts["hwdec"] = "no"
        opts["ao"] = "null"
        opts.pop("vaapi-device", None)      # no device may be pinned
        opts.pop("vulkan-device", None)
    user = cfg.get("mpv.options", {}) or {}
    if isinstance(user, dict):
        for k, v in user.items():
            k = str(k)
            if k in PINNED_INIT_KEYS or k in BLOCKED_INIT_KEYS or \
                    k.startswith("script"):
                log.warning("mpv.options: key %r blocked by safety sanitizer", k)
                continue
            opts[k] = str(v)
    return opts


# --------------------------------------------------------------------------
# renderer_override → DRI_PRIME (GTK_GL_DEVICE is a no-op; DRI_PRIME is the
# verified lever, P0 recon).  Re-exec once when the override doesn't match
# the environment yet, because the env must exist BEFORE GL initializes.

_CARD_RE = re.compile(r"card(\d+)$")


def drm_render_devices(devroot: str = "/dev") -> list[str]:  # SYNC-FS-OK
    return sorted(glob.glob(os.path.join(devroot, "dri", "renderD*")))


def prime_index_map(sysroot: str = "/sys", devroot: str = "/dev"  # SYNC-FS-OK
                    ) -> dict[str, int]:
    """pci-slot → DRI_PRIME index.  Verified mapping rule (P0 recon table:
    DRI_PRIME=2 → renderD128, the dGPU): index = position of the card in the
    numerically sorted /sys/class/drm/cardN set that has a render node."""
    base = os.path.join(sysroot, "class", "drm")
    entries: list[tuple[int, str]] = []
    try:
        names = os.listdir(base)
    except OSError:
        return {}
    for name in names:
        m = _CARD_RE.match(name)
        if not m:
            continue
        idx = int(m.group(1))
        dev = os.path.join(base, name, "device")
        try:
            real = os.path.realpath(dev)
        except OSError:
            continue
        slot = os.path.basename(real)          # "0000:03:00.0"
        entries.append((idx, slot))
    out: dict[str, int] = {}
    for order, (idx, slot) in enumerate(sorted(entries)):
        out[_short(slot)] = order
    return out


def _short(pci: str) -> str:
    return pci.split(":", 1)[1] if ":" in pci and pci.count(":") == 2 else pci


def apply_renderer_override(cfg) -> None:
    """engine.renderer_override is a DRI_PRIME index (string) per schema §5.2.
    The env must exist BEFORE GL initializes (call before importing gi);
    if it doesn't match, re-exec this same interpreter once (PLAN §5.3 —
    GTK_GL_DEVICE is a verified no-op, DRI_PRIME is the real lever)."""
    try:
        override = str(cfg.get("engine.renderer_override", "auto"))
    except Exception:
        return
    if override in ("", "auto"):
        return
    if not override.isdigit():
        log.warning("renderer_override %r is not a DRI_PRIME index, ignoring",
                    override)
        return
    if os.environ.get("DRI_PRIME") == override and \
            os.environ.get("REELSEE_PRIME_APPLIED") == override:
        return
    os.environ["DRI_PRIME"] = override
    os.environ["REELSEE_PRIME_APPLIED"] = override
    os.execv(sys.executable, [sys.executable] + sys.argv)


# ===========================================================================
# P6 — Engine model + Appendix-B enumeration (§5.3, Appendix B)
# ===========================================================================

@dataclass(frozen=True)
class Engine:
    id: str            # "auto" | "cpu" | "0000:13:00.0" | "0000:13:00.0:vulkan"
    label: str         # "iGPU — Granite Ridge [Radeon Graphics] (VAAPI)" …
    kind: str          # "auto" | "vaapi" | "vulkan" | "cpu"
    pci: str | None = None
    render_node: str | None = None    # "/dev/dri/renderD130"
    is_display_gpu: bool = False
    pci_id: str | None = None         # "1002:1453" vendor:device (vulkan join)


def _read_uevent(dev_dir: str) -> dict[str, str]:  # SYNC-FS-OK
    out: dict[str, str] = {}
    try:
        with open(os.path.join(dev_dir, "uevent"), encoding="utf-8") as fh:
            for line in fh:
                k, _, v = line.strip().partition("=")
                if k:
                    out[k] = v
    except OSError:
        pass
    return out


def by_path_render_nodes(devroot: str = "/dev") -> dict[str, str]:  # SYNC-FS-OK
    """`/dev/dri/by-path/pci-<slot>-render` → {slot: absolute node path}.
    reboot-stable mapping (Appendix B step 1)."""
    out: dict[str, str] = {}
    for p in sorted(glob.glob(os.path.join(devroot, "dri", "by-path",
                                           "pci-*-render"))):
        base = os.path.basename(p)
        slot = base[len("pci-"):-len("-render")]
        node = os.path.basename(os.path.realpath(p))
        if node.startswith("renderD"):
            out[slot] = os.path.join(devroot, "dri", node)
    return out


def sysfs_cards(sysroot: str = "/sys") -> dict[str, dict]:  # SYNC-FS-OK
    """pci-slot → {card, driver, pci_id, render} for cardN with a PCI device."""
    base = os.path.join(sysroot, "class", "drm")
    out: dict[str, dict] = {}
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return out
    for name in names:
        m = _CARD_RE.match(name)
        if not m:
            continue
        dev = os.path.join(base, name, "device")
        ev = _read_uevent(dev)
        slot = ev.get("PCI_SLOT_NAME") or os.path.basename(os.path.realpath(dev))
        if not re.fullmatch(r"[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.\d",
                            slot):
            continue
        render = None
        for rp in sorted(glob.glob(os.path.join(dev, "drm", "renderD*"))):
            render = os.path.basename(rp)
            break
        out.setdefault(slot, {"card": name, "driver": ev.get("DRIVER", ""),
                              "pci_id": ev.get("PCI_ID", "").lower(),
                              "render": render})
    return out


def display_gpu_slot(sysroot: str = "/sys") -> str | None:  # SYNC-FS-OK
    """Slot owning a connector whose status file says `connected`
    (Appendix B step 3; first match in card order wins)."""
    base = os.path.join(sysroot, "class", "drm")
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return None
    cards = sysfs_cards(sysroot)
    by_card = {info["card"]: slot for slot, info in cards.items()}
    for name in names:
        m = re.fullmatch(r"(card\d+)-.+", name)
        if not m:
            continue
        try:
            with open(os.path.join(base, name, "status"),
                      encoding="utf-8") as fh:
                status = fh.read().strip()
        except OSError:
            continue
        if status == "connected" and m.group(1) in by_card:
            return by_card[m.group(1)]
    return None


def lspci_device(slot: str) -> str | None:
    """Marketing device name for a pci slot via `lspci -mm -s <short>`."""
    try:
        r = subprocess.run(["lspci", "-mm", "-s", _short(slot)],
                           capture_output=True, text=True, timeout=5)
        if r.returncode != 0:
            return None
        quoted = re.findall(r'"((?:[^"\\]|\\.)*)"', r.stdout)
        if len(quoted) >= 3:
            name = quoted[2]
            return None if name.startswith("Device ") or not name else name
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def vulkan_device_names() -> dict[str, str]:
    """{PCI_ID lower → exact vulkan device name} from
    `mpv --no-config --vulkan-device=help` (Appendix B step 5).  NEVER
    hardcode names (§5.3 invariant).  No CLI configured → {} (engines stay
    selectable but `unverified`)."""
    cli = os.environ.get("REELSEE_MPV_CLI", "")
    if not cli:
        return {}
    try:
        r = subprocess.run([cli, "--no-config", "--vulkan-device=help"],
                           capture_output=True, text=True, timeout=10)
        out: dict[str, str] = {}
        for m in re.finditer(r"'([^']+)' \(GPU \d+, PCI ID ([0-9a-fA-F:]+)",
                             r.stdout):
            out[m.group(2).lower()] = m.group(1)
        return out
    except (OSError, subprocess.SubprocessError):
        return {}


def _label(cards: dict, slot: str, marketing: str, disp: str | None,
           dup: dict[str, int], total: dict[str, int], kind_tag: str) -> str:
    role = "iGPU" if slot == disp else "dGPU"
    name = marketing
    if total[marketing] > 1:
        dup[marketing] = dup.get(marketing, 0) + 1
        name = f"{marketing} #{dup[marketing]}"
    return f"{role} — {name} ({kind_tag})"


def enumerate_engines(sysroot: str = "/sys", devroot: str = "/dev",
                      lspci=lspci_device,
                      vulkan=vulkan_device_names) -> "list[Engine]":
    """Selector list, Appendix-B order: Auto → display GPU (VAAPI) → other
    GPUs (VAAPI) → Vulkan variants → CPU (R-8: CPU always last, present)."""
    cards = sysfs_cards(sysroot)
    bypath = by_path_render_nodes(devroot)
    disp = display_gpu_slot(sysroot)

    def node_of(slot: str) -> str | None:
        if slot in bypath:
            return bypath[slot]
        r = cards.get(slot, {}).get("render")
        return os.path.join(devroot, "dri", r) if r else None

    def mk(slot: str, info: dict, kind: str, tag: str,
           dup: dict, total: dict) -> Engine:
        marketing = lspci(slot) or f"GPU {slot}"
        return Engine(
            id=slot + (":vulkan" if kind == "vulkan" else ""),
            label=_label(cards, slot, marketing, disp, dup, total, tag),
            kind=kind, pci=slot, render_node=node_of(slot),
            is_display_gpu=(slot == disp),
            pci_id=info.get("pci_id") or None)

    vaapi_slots = sorted(s for s, i in cards.items() if i["driver"] == "amdgpu")
    marketing = {s: (lspci(s) or f"GPU {s}") for s in vaapi_slots}
    total: dict[str, int] = {}
    for name in marketing.values():
        total[name] = total.get(name, 0) + 1
    dup: dict[str, int] = {}

    def vaapi_order() -> "list[Engine]":
        ordered = ([disp] if disp in vaapi_slots else []) + \
            [s for s in vaapi_slots if s != disp]
        return [mk(s, cards[s], "vaapi", "VAAPI", dup, total)
                for s in ordered]

    auto = Engine(id="auto", kind="auto", pci=disp,
                  render_node=node_of(disp) if disp else None,
                  is_display_gpu=True,
                  label="Auto (display GPU)" if disp else "Auto")
    out = [auto] + vaapi_order()

    names = vulkan() or {}
    vk: "list[Engine]" = []
    vk_slots = [s for s in cards
                if s in vaapi_slots and cards[s]["pci_id"] in names]
    vk_total: dict[str, int] = {}
    for s in vk_slots:
        m = marketing[s]
        vk_total[m] = vk_total.get(m, 0) + 1
    vk_dup: dict[str, int] = {}
    for s in vk_slots:
        vk.append(mk(s, cards[s], "vulkan", "Vulkan", vk_dup, vk_total))
    out += vk
    out.append(Engine(id="cpu", label="CPU decode (software)", kind="cpu"))
    return out


def select_engine(decoder, engines: "list[Engine]") -> Engine:
    """"auto" | "cpu" | "<pci>[:vulkan]" → Engine; anything unknown falls
    back to auto (selector and config share this resolver)."""
    dec = str(decoder or "").strip() or "auto"
    for e in engines:
        if e.id == dec:
            return e
    log.warning("engine.decoder %r not enumerated — using auto", dec)
    return engines[0] if engines and engines[0].kind == "auto" else \
        Engine(id="auto", label="Auto", kind="auto")


def cpu_thread_count(cfg, n_windows: int = 1) -> int:
    try:
        want = int(cfg.get("engine.cpu_threads", 0)) if cfg else 0
    except (TypeError, ValueError):
        want = 0
    if want > 0:
        return want
    nproc = os.cpu_count() or 2
    return max(1, (nproc - 2) // max(1, int(n_windows)))


def mpv_options(e: Engine, cfg=None, n_windows: int = 1,
                sysroot: str = "/sys", devroot: str = "/dev",
                vulkan=vulkan_device_names) -> dict[str, str]:
    """§5.3 mapping (R2-P1 corrected).  Invariants: `vaapi-device` is always
    an absolute render-node path RESOLVED AT CALL TIME; `vulkan-device` is
    always an exact string re-read from the CLI (never hardcoded).

    R2-P1/B1: every GPU engine defaults to `vaapi-copy` — zero-copy `vaapi`
    cannot engage under the embedded EGL/GL render path (gpu-api=opengl),
    so requesting it meant SILENT CPU decode (B1).  config engine.zero_copy
    (default false) opts back into zero-copy `vaapi` on request."""
    o: dict[str, str] = {}
    if e.kind == "cpu":
        o["hwdec"] = "no"
        o["vd-lavc-threads"] = str(cpu_thread_count(cfg, n_windows))
        return o
    try:
        zero_copy = bool(cfg.get("engine.zero_copy", False)) if cfg \
            else False
    except Exception:
        zero_copy = False
    # GPU engines (auto / vaapi / vulkan kinds): uniform honest default.
    o["hwdec"] = "vaapi" if zero_copy else "vaapi-copy"
    o["hwdec-codecs"] = "all"
    node_slot = e.pci if e.kind != "auto" else (
        e.pci or display_gpu_slot(sysroot))
    node = render_node_for(node_slot, sysroot, devroot)
    if node:
        o["vaapi-device"] = node
    if e.kind == "vulkan":
        # Selection string stays emitted (exact, re-read — §5.3 invariant)
        # so a vulkan decode path remains configured the moment the render
        # path supports it; decode itself uses the proven copy mode.
        exact = (vulkan() or {}).get(str(e.pci_id).lower())
        if exact:
            o["vulkan-device"] = exact
    return o


def render_node_for(pci: str | None, sysroot: str = "/sys",
                    devroot: str = "/dev") -> str | None:
    """Absolute render node for a pci slot, resolved AT CALL TIME
    (§5.3 invariant): by-path first, sysfs card dir as fallback."""
    if not pci:
        return None
    node = by_path_render_nodes(devroot).get(pci)
    if node:
        return node
    info = sysfs_cards(sysroot).get(pci)
    if info and info.get("render"):
        return os.path.join(devroot, "dri", info["render"])
    return None


def engine_hwdec_mode(e: Engine, cfg=None) -> str:
    """The runtime hwdec string an engine maps to (§5.3)."""
    return mpv_options(e, cfg, 1, vulkan=lambda: {}).get("hwdec", "no")


def apply_engine(core, e: Engine, cfg=None) -> None:
    """Apply an engine to a LIVE handle — the P0-PROOF-A mechanism (R20):
    set hwdec <mode> + vid no/auto (hwdec alone never re-inits)."""
    core.apply_hwdec(engine_hwdec_mode(e, cfg))


# ------------------------------------------------------------- R10 readout
def readout_text(engines: "list[Engine]", decoder, snapshot,
                 renderer_override="auto") -> str:
    """Decode & GPU live readout.  R10: EVERY value comes from the observed
    Snapshot (never a synchronous getter) or from enumeration data."""
    sel = select_engine(decoder, engines)
    hw = str(snapshot.get("hwdec-current") or "—")
    node = os.path.basename(sel.render_node) if sel.render_node else \
        ("sw" if sel.kind == "cpu" else "—")
    renderer = ("auto" if str(renderer_override) in ("", "auto")
                else f"DRI_PRIME={renderer_override}")
    return (f"Renderer: {renderer} · Decoder: {hw} ({node}) · "
            f"hwdec-current: {hw}")


# ------------------------------------------------- capability probe cache
PROBE_TTL_S = 7 * 86400


def probe_key(pci: str | None, hwdec: str, codec: str = "h264") -> str:
    return f"{pci or 'auto'}|{hwdec}|{codec}"


class ProbeCache:
    """engines.json — {key: {"ok": bool, "ts": int}}, 7-day TTL (§5.3/§5.2).
    A probe that cannot run (headless, no render node) leaves entries
    absent → engines stay selectable, flagged unverified (Appendix B step 6)."""

    def __init__(self, path: str | None = None):  # SYNC-FS-OK
        if path is None:
            from config import state_dir
            path = os.path.join(state_dir(), "engines.json")
        self.path = path
        self.data: dict[str, dict] = {}
        try:
            with open(path, encoding="utf-8") as fh:
                d = json.load(fh)
            if isinstance(d, dict):
                self.data = {str(k): v for k, v in d.items()
                             if isinstance(v, dict)}
        except (OSError, ValueError):
            pass

    def get(self, key: str, now: float | None = None):
        entry = self.data.get(key)
        if not entry:
            return None
        try:
            ts = float(entry.get("ts", 0))
        except (TypeError, ValueError):
            return None
        if (now or time.time()) - ts > PROBE_TTL_S:
            return None
        return bool(entry.get("ok"))

    def set(self, key: str, ok: bool, now: float | None = None) -> None:  # SYNC-FS-OK
        self.data[key] = {"ok": bool(ok), "ts": int(now or time.time())}
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.data, fh, indent=1)
            os.replace(tmp, self.path)
        except OSError as exc:
            log.warning("engines.json save failed: %s", exc)


def record_last_fallback(entry: dict, path: str | None = None) -> bool:  # SYNC-FS-OK
    """R2-P2: merge {"last_fallback": entry} into engines.json (dispatch:
    fallback steps must be RECORDED there, next to the probe cache).  This
    module owns that file's format; callers (watchdog/engagement) pass a
    plain dict {from,to,ts,reason,...}.  Returns whether it persisted —
    failures log at WARNING so smoke greps catch silent record loss."""
    if path is None:
        from config import state_dir
        path = os.path.join(state_dir(), "engines.json")
    data: dict = {}
    try:
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh)
        if isinstance(d, dict):
            data = {str(k): v for k, v in d.items() if isinstance(v, dict)}
    except (OSError, ValueError):
        pass
    data["last_fallback"] = dict(entry)
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1)
        os.replace(tmp, path)
        return True
    except OSError as exc:
        log.warning("engines.json last_fallback record failed: %s", exc)
        return False


def can_probe(e: Engine, devroot: str = "/dev") -> bool:  # SYNC-FS-OK
    """A probe needs the real render node present (§0: vo=null and bare
    Xvfb probes are INVALID) — otherwise mark unverified, never fake."""
    if e.kind in ("cpu", "auto"):
        return True
    return bool(e.render_node) and os.path.exists(
        e.render_node.replace("/dev", devroot, 1) if devroot != "/dev"
        else e.render_node)
