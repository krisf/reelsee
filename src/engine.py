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
        # R11: the crop-to-window mechanism (panscan=1.0) is a COVER ZOOM
        # only while the aspect is kept — panscan with keepaspect=no would
        # STRETCH instead of fill-and-clip.  mpv defaults to yes; pinned
        # so the mechanism can never silently degrade to distortion.
        "keepaspect": "yes",
    }


def valid_loop_value(value) -> str | None:
    """R9-A gate for feeding a stored config loop value to mpv as an
    init OPTION: only values mpv itself speaks — "inf"/"force" or a
    non-negative integer string.  "no"/empty/None/anything unrecognised
    → None: the property simply keeps its mpv default ("no" → the icon
    derives "off") and the stored value stays untouched in config."""
    s = "" if value is None else str(value).strip()
    if s in ("inf", "force"):
        return s
    try:
        n = int(s)
    except ValueError:
        return None
    return s if n >= 0 else None


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
    # R16-E §E.1/§E.2: the DECODE node is an independent knob from the render
    # GPU, so it is resolved here rather than inherited from the engine
    # selector.  `playback.vaapi_device` pins ANY node (escape hatch).  Empty
    # means the §E.1 rule governs an AUTOMATIC engine: the INTEGRATED GPU —
    # display-bearing or not (ROUND18: connectors are not part of the
    # mandate) — or CPU when no card qualifies; never a board dGPU.  An
    # engine the user pinned to a PCI slot keeps its own node: a human
    # already chose that card, and overriding it silently would be worse
    # than the §D.3 ban it protects.
    if sel.kind != "cpu":
        pin = vaapi_device_pin(cfg)
        if pin:
            node: "str | None" = pin
        elif sel.kind == "auto":
            node = pick_decode_node(sysroot, devroot)
            if node is None:
                # §E.1's own escape: CPU for everyone, and say so in the log.
                log.info("no headless decode GPU — CPU decode for all "
                         "windows (playback.vaapi_device is unset)")
                opts.pop("vaapi-device", None)
                opts.pop("vulkan-device", None)
                opts["hwdec"] = "no"
                threads = cpu_thread_count(cfg, n_windows)
                if threads > 0:
                    opts["vd-lavc-threads"] = str(threads)
        else:
            node = None
        if node:
            opts["vaapi-device"] = node
            opts["hwdec"] = hwdec_mode_for_node(node, cfg, sysroot, devroot)
    if bool(cfg.get("playback.keep_open", True)):
        opts["keep-open"] = "yes"
    # R9-A: a persisted repeat choice is the core's BEGINNING state, not
    # a post-load patch — seeding it as an init option makes the FIRST
    # observed loop-file/loop-playlist value match the seeded bottom-bar
    # glyph and radio (no flicker to "off" at startup, same contract as
    # mute), and the pre-load PROPERTY_MAP push simply agrees with it.
    # Unrecognised stored values are skipped (property stays "no").
    for _key, _opt in (("playback.loop_file", "loop-file"),
                       ("playback.loop_playlist", "loop-playlist")):
        _val = valid_loop_value(cfg.get(_key))
        if _val is not None:
            opts[_opt] = _val
    # R11: crop-to-window obeys the same restart contract — a persisted
    # "on" is the core's BEGINNING state (the very first frame already
    # fills the window; no flicker), and the PROPERTY_MAP push on load
    # simply agrees with it.  "off" needs no seed (panscan defaults to 0).
    if bool(cfg.get("video.crop_to_window", False)):
        opts["panscan"] = "1"
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
# Drivers whose render node can carry VAAPI decode on this stack.  amdgpu
# only today (the rig is all-Radeon); the tuple keeps the integrated-card
# rule honest if an intel/amdgpu-adjacent driver is ever enumerated here.
GPU_DRIVERS = ("amdgpu",)


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
    # ROUND18: discrete-ness — not the display-ness — is what keeps a card
    # out of automatic decode.  The settings row labels from this, never
    # from is_display_gpu (the iGPU itself may carry the displays).
    is_discrete: bool = False


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
            pci_id=info.get("pci_id") or None,
            is_discrete=discrete_gpu_slot(slot, sysroot))

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


# ------------------------------------------------- R16-D/E: the decode GPU
# Decode GPU and render GPU are INDEPENDENT knobs.  AUTOMATIC selection is
# INTEGRATED-ONLY: the chip's own graphics (the iGPU) or CPU — it may NEVER
# pick a board dGPU (ROUND16 §D.3, user-mandated).  Display connectors are
# IRRELEVANT to eligibility: the iGPU may well be the display-bearing card,
# and on the operator's rig it IS (the Granite Ridge carries the monitors;
# ROUND18 closed the bug where ROUND16 §E.1's "zero connectors" clause —
# written on the false assumption that this rig's iGPU is connector-free —
# disqualified it and every window decoded on the CPU).  The mandate never
# mentioned connectors; `discrete_gpu_slot()` (PCIe topology depth ≥3,
# dedicated VRAM ≥ floor, both fail-closed) is what keeps the R9700s out.
# `playback.vaapi_device` remains the documented escape hatch that lets a
# human pin any node explicitly (§E.2); automatic selection (§E.1) never
# names a node that is not integrated, whatever that setting says.

DECODE_CAPACITY = 2       # streams one integrated GPU holds; refined by the bench

# decode_plan's "which engine is the GPU?" default: §E.1's integrated-only
# rule picks it.  A sentinel, not None, because None is a MEANINGFUL value
# here — "no GPU engine at all" — and a pinned node passes a real Engine
# instead (§E.2).
_UNSET = object()


def connected_cards(sysroot: str = "/sys") -> set[str]:  # SYNC-FS-OK
    """cardN names that expose at least one CONNECTED display connector."""
    base = os.path.join(sysroot, "class", "drm")
    out: set[str] = set()
    try:
        names = os.listdir(base)
    except OSError:
        return out
    for name in names:
        m = re.fullmatch(r"(card\d+)-.+", name)
        if not m:
            continue
        try:
            with open(os.path.join(base, name, "status"),
                      encoding="utf-8") as fh:
                if fh.read().strip() == "connected":
                    out.add(m.group(1))
        except OSError:
            continue
    return out


DISCRETE_VRAM_FLOOR = 4 * 1024 ** 3   # 4 GiB dedicated VRAM ⇒ discrete card
DISCRETE_TOPOLOGY_DEPTH = 3           # hops below the PCI domain root


def discrete_gpu_slot(slot: str, sysroot: str = "/sys") -> bool:  # SYNC-FS-OK
    """True when a PCI card is a DISCRETE GPU (a board add-in), not the
    chip's integrated graphics.

    Why this exists: it is the ONLY gate on automatic decode eligibility
    (ROUND18) — the mandate is "integrated GPU or CPU, never a dGPU", and
    this is what "integrated" means in code.  ROUND16 §E.1 used to ALSO
    require zero connected display connectors, a proxy that INVERTED on the
    operator's rig: the Granite Ridge iGPU carries the displays, so the
    connector clause disqualified it while a headless boot would have
    qualified the very R9700s §D.3 forbids.  Two independent topology
    signals stand alone:

      * PCIe path: a dGPU hangs off a root port (and usually a switch), so
        its sysfs device path is >= 3 components below `pci0000:00`
        (`0000:00:01.1/0000:01:00.0/0000:02:00.0/0000:03:00.0`); the iGPU
        hangs off a chipset function, 2 components
        (`0000:00:08.1/0000:13:00.0`).
      * dedicated VRAM: a board dGPU ships >= 6 GiB of its own memory; the
        Granite Ridge iGPU reports 2 GiB.

    Both fail CLOSED: anything unreadable or unexpected counts as discrete,
    which removes the card from automatic selection and lands the plan on
    CPU.  Getting this wrong can only cost CPU decode, never an R9700."""
    dev = os.path.join(sysroot, "class", "drm")
    cards = sysfs_cards(sysroot)
    info = cards.get(slot)
    if not info:
        return True
    card = info.get("card") or ""
    real = os.path.realpath(os.path.join(dev, card, "device"))
    tail = real.split("pci0000:00/", 1)
    depth = len(tail[1].split("/")) if len(tail) == 2 else 0
    if depth >= DISCRETE_TOPOLOGY_DEPTH:
        return True
    vram = os.path.join(real, "mem_info_vram_total")
    try:
        with open(vram, encoding="utf-8") as fh:
            total = int(fh.read().strip())
    except (OSError, ValueError):
        return True                     # unknown memory ⇒ assume discrete
    return total >= DISCRETE_VRAM_FLOOR


def integrated_gpu_slots(sysroot: str = "/sys",
                         cards: dict | None = None) -> "list[str]":
    """PCI slots of cards eligible for AUTOMATIC video decode: amdgpu cards
    with a render node that are NOT discrete (`discrete_gpu_slot()`).

    That is the iGPU class, and it is the whole mandate: decode lands on the
    chip's integrated graphics or falls back to CPU — never on a board dGPU.
    Display connectors do NOT gate eligibility (ROUND18): a card driving
    monitors may be the iGPU, and on the operator's rig it is — the old
    "zero connected connectors" clause disqualified the only eligible card
    and left every window on CPU decode.  Sorted ascending by slot so callers
    can take the LAST one (§E.1, the skill-verified position of the iGPU on
    this rig).  A dGPU is excluded even when it is headless — see
    discrete_gpu_slot()."""
    cards = cards if cards is not None else sysfs_cards(sysroot)
    return sorted(s for s, i in cards.items()
                  if i.get("driver") in GPU_DRIVERS
                  and i.get("render")
                  and not discrete_gpu_slot(s, sysroot))


def pick_decode_node(sysroot: str = "/sys",
                     devroot: str = "/dev") -> str | None:
    """Render node automatic decode is allowed to use: the LAST PCI slot among
    the integrated cards.  None means nothing qualified — and then the plan is
    CPU for everyone, never a display-attached dGPU."""
    slots = integrated_gpu_slots(sysroot)
    if not slots:
        return None
    return render_node_for(slots[-1], sysroot, devroot)


def pick_decode_engine(engines: "list[Engine]",
                       sysroot: str = "/sys") -> "Engine | None":
    """The Engine automatic decode may use, or None (= CPU).

    Only an integrated card qualifies (connectors are irrelevant — see
    integrated_gpu_slots).  VAAPI is preferred over Vulkan for the same slot
    because the plan and the bench are expressed in VAAPI terms."""
    slots = set(integrated_gpu_slots(sysroot))
    if not slots:
        return None
    for kind in ("vaapi", "vulkan"):
        cands = [e for e in engines
                 if getattr(e, "kind", "") == kind
                 and getattr(e, "pci", None) in slots]
        if cands:
            return max(cands, key=lambda e: str(e.pci))
    return None


def display_bearing_cards(sysroot: str = "/sys") -> set[str]:  # SYNC-FS-OK
    """cardN names that carry a display: at least one connector that is
    `connected` OR `enabled`.

    ROUND18: this NO LONGER gates decode eligibility — the iGPU may be
    display-bearing and must stay eligible.  It still feeds labels and
    probes (the settings row, the engine labels, probe_vaapi_device's card
    table).  `status` and `enabled` are separate sysfs files and a monitor
    can be enabled-but-unplugged, so a card counts as display-bearing if
    EITHER says so — the conservative union for labelling."""
    base = os.path.join(sysroot, "class", "drm")
    out: set[str] = set()
    try:
        names = os.listdir(base)
    except OSError:
        return out
    for name in names:
        m = re.fullmatch(r"(card\d+)-.+", name)
        if not m:
            continue
        for field in ("status", "enabled"):
            try:
                with open(os.path.join(base, name, field),
                          encoding="utf-8") as fh:
                    val = fh.read().strip()
            except OSError:
                continue
            if val in ("connected", "enabled"):
                out.add(m.group(1))
                break
    return out


def render_gpu_node(sysroot: str = "/sys",
                    devroot: str = "/dev") -> str | None:  # SYNC-FS-OK
    """The render node the compositor (and therefore our GL surface) uses:
    the display GPU's node.  §E.3's comparison target — decode on this very
    node needs no frame copy across cards, decode anywhere else does."""
    return render_node_for(display_gpu_slot(sysroot), sysroot, devroot)


def hwdec_mode_for_node(node: str | None, cfg=None, sysroot: str = "/sys",
                        devroot: str = "/dev") -> str:
    """§E.3: which hwdec string a chosen decode NODE implies.

    Decode GPU and render GPU are independent knobs, but frames have to reach
    the render GPU: when they are different cards the frames must be copied,
    so `vaapi-copy` is mandatory.  When they are the SAME card copy is not
    required by the topology — and there direct `vaapi` would be preferred —
    except that R2-P1/B1 measured zero-copy `vaapi` never engaging under this
    embedded EGL/GL path (gpu-api=opengl): asking for it produced SILENT CPU
    decode.  Direct `vaapi` therefore stays behind the documented
    `engine.zero_copy` opt-in even same-GPU; the engagement watcher
    self-corrects if a future render path lets it stick.
    """
    try:
        zero_copy = bool(cfg.get("engine.zero_copy", False)) if cfg else False
    except Exception:
        zero_copy = False
    if not node:
        return "no"
    if zero_copy and node == render_gpu_node(sysroot, devroot):
        return "vaapi"
    return "vaapi-copy"


def vaapi_device_pin(cfg) -> str:
    """The explicit `playback.vaapi_device` setting, "" when unset/blank.

    §E.2's escape hatch: a non-empty value pins that node for every window
    and may name ANY node, including a dGPU — the ban in §D.3 is on
    AUTOMATIC selection, and at this point a human owns the choice."""
    try:
        return str(cfg.get("playback.vaapi_device", "") or "").strip()
    except Exception:
        return ""


def resolve_decode_node(cfg, sysroot: str = "/sys",
                        devroot: str = "/dev") -> str | None:  # SYNC-FS-OK
    """The render node decode should use, or None (= CPU).

    §E.1/§E.2: a pin wins outright; otherwise AUTO — the integrated
    (non-discrete; connectors irrelevant, ROUND18) card's node, last PCI
    slot first — and nothing qualifying returns None, which the caller must
    read as CPU decode and never as "use the display GPU".  This is the
    question the settings row and the probe script both answer;
    `mpv_init_options` asks the same question through `vaapi_device_pin` so
    it can tell "no pin" from "no node" (see the comment there)."""
    pin = vaapi_device_pin(cfg)
    if pin:
        return pin
    return pick_decode_node(sysroot, devroot)


def engine_for_node(node: str, engines: "list[Engine] | None" = None) -> Engine:
    """The Engine that decodes on `node` — for §E.2's pin.

    A pinned node is an explicit human choice, so it is honoured even when it
    is a dGPU or a node that enumeration did not offer (a card added after
    boot, a node named by symlink): a synthesised VAAPI engine keeps the
    escape hatch working instead of silently dropping the pin."""
    if engines is None:
        engines = enumerate_engines()
    for e in engines:
        if e.render_node == node and e.kind in ("vaapi", "vulkan"):
            return e
    return Engine(id=node, label=f"VAAPI on {node}", kind="vaapi",
                  render_node=node)


def decode_plan(n_windows, capacity: int = DECODE_CAPACITY,
                engines: "list[Engine] | None" = None,
                sysroot: str = "/sys",
                gpu_engine: "Engine | None" = _UNSET) -> "list[Engine]":
    """Per-window decoder assignment for a session of N windows (§D.2).

    GPU (the iGPU's VAAPI) for the first `capacity` windows, CPU beyond it.
    `capacity` defaults to 2 — the value the bench's margin rule confirms or
    corrects on the real desktop (`scripts/bench_multidecode.py`: the highest
    N where every stream holds >= 24 fps with no rising drop trend).

    `engines` / `sysroot` are injectable so the whole rule is a unit-test
    truth table without a GPU.  When NO integrated card qualifies the plan is
    CPU for every window: the fallback is CPU, never a display-attached dGPU.

    `gpu_engine` overrides WHICH engine counts as "the GPU" — §E.2's pinned
    node uses this so the pin decides the card while the plan still decides
    how many windows may use it.  Left unset, §E.1's integrated-only rule
    picks it.
    """
    try:
        n = max(0, int(n_windows))
    except (TypeError, ValueError):
        n = 0
    try:
        cap = max(0, int(capacity))
    except (TypeError, ValueError):
        cap = DECODE_CAPACITY
    if engines is None:
        engines = enumerate_engines()
    gpu = gpu_engine if gpu_engine is not _UNSET else \
        pick_decode_engine(engines, sysroot)
    cpu = select_engine("cpu", engines)
    return [gpu if (gpu is not None and i < cap) else cpu
            for i in range(n)]


# R16-C: the CPU decode budget.  `vd-lavc-threads` used to be a flat
# `(cores - 2) // n_windows` for EVERY window count, so a single window got a
# number lavc would have picked anyway while N windows each still got a slice
# of the whole machine — the oversubscription bites precisely when several
# windows are ALSO demoted to CPU decode.
THREADS_CAP = 16


def threads_for(n_windows: int, cores: int) -> int:
    """Threads per CPU-decoded window: `max(2, cores // (2*N))`, capped at 16.

    Pure: no config, no clock, no /proc — the whole truth table is unit-test
    (tests/test_engine.py::ThreadBudget).  0 means "leave lavc's own default
    alone", which is the brief's "1 window ⇒ auto/current default" and the
    honest answer for a machine that reports no usable core count.  Two
    threads is the floor because lavc needs one decode plus one reference
    frame thread to make any progress at all."""
    try:
        n = int(n_windows)
    except (TypeError, ValueError):
        n = 1
    try:
        c = int(cores)
    except (TypeError, ValueError):
        c = 0
    if n <= 1 or c <= 0:
        return 0
    return min(THREADS_CAP, max(2, c // (2 * n)))


def cpu_thread_count(cfg, n_windows: int = 1) -> int:
    """Threads for one CPU-decoded window: an explicit `engine.cpu_threads`
    wins outright (0/negative = "not set"), otherwise the R16-C budget."""
    try:
        want = int(cfg.get("engine.cpu_threads", 0)) if cfg else 0
    except (TypeError, ValueError):
        want = 0
    if want > 0:
        return want
    return threads_for(n_windows, os.cpu_count() or 2)


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
        # R16-C: only a multi-window run gets a budget; a single window keeps
        # lavc's own default (no option written at all).
        threads = cpu_thread_count(cfg, n_windows)
        if threads > 0:
            o["vd-lavc-threads"] = str(threads)
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
