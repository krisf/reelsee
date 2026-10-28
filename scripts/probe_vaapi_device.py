#!/usr/bin/python3
"""Which render node should VAAPI decode use — and does it actually engage?

OPERATOR TOOL.  Run it on the real desktop:

    scripts/probe_vaapi_device.py                 # what AUTO would pick
    scripts/probe_vaapi_device.py --node /dev/dri/renderD128   # the pin

ROUND16 §E.1 (as corrected by ROUND18) makes the DECODE node an independent
knob from the render GPU and restricts automatic choice to the INTEGRATED
card — the iGPU, display-bearing or not (on this rig the Granite Ridge iGPU
carries the monitors, and connectors are not part of the mandate) — or CPU.
This script answers three questions an operator can only answer on the box:

  1. what does the §E.1 rule resolve to here (and why), card by card;
  2. does VAAPI ENGAGE on that node — `hwdec-current` read back from a live
     mpv instance, which is the only honest signal (the option being accepted
     means nothing; R2-P1 proved `vaapi` can be accepted and still decode on
     the CPU);
  3. which §E.3 hwdec mode the node implies (copy across cards, direct only
     same-card plus the `engine.zero_copy` opt-in).

It is NOT part of the headless battery and must never be read as a capacity
gate: llvmpipe has no VAAPI, so the headless answer is always "CPU", which is
also the correct §E.1 answer when no card qualifies.  The capacity number
comes from `scripts/bench_multidecode.py`; this script only proves a node
works at all.

Output (machine-checkable):
    PROBE_NODE slot=<pci> card=<cardN> node=<path> display=<0|1> discrete=<0|1>
    PROBE_AUTO node=<path|none> hwdec=<mode> reason=<text>
    PROBE_RESULT node=<path|none> requested=<mode> hwdec_current=<mode>
                 decoder=<name> vf_fps=<x> engaged=<0|1>
Exit: 0 = outcome matches the plan; 2 = a GPU node was asked for and VAAPI
did not engage (or §E.1 named a discrete card, which is a bug).
"""
from __future__ import annotations

import argparse
import locale
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import engine as E  # noqa: E402
import mpvlib as M  # noqa: E402
import watchdog as WD  # noqa: E402

DRAIN_WAIT_S = 0.002       # bounded drain: a 0/negative timeout never returns
CLIP = "testdata/smoke_360p.mp4"
PROPS = ("hwdec", "hwdec-current", "hwdec-active", "current-decoder",
         "video-decoder", "estimated-vf-fps", "eof-reached")


def card_table(sysroot: str, devroot: str) -> list[str]:
    """One line per card: the raw signals §E.1 reads, plus the verdict."""
    cards = E.sysfs_cards(sysroot)
    bearing = E.display_bearing_cards(sysroot)
    lines = []
    for slot, info in sorted(cards.items()):
        node = E.render_node_for(slot, sysroot, devroot)
        lines.append(
            f"PROBE_NODE slot={slot} card={info['card']} "
            f"driver={info['driver'] or 'none'} "
            f"node={node or 'none'} "
            f"display={int(info['card'] in bearing)} "
            f"discrete={int(E.discrete_gpu_slot(slot, sysroot))} "
            f"eligible={int(slot in set(E.integrated_gpu_slots(sysroot)))}")
    if not lines:
        lines.append("PROBE_NODE none — no DRM card under this sysroot")
    return lines


def auto_verdict(cfg, sysroot: str, devroot: str) -> tuple:
    """What §E.1 would choose right now, with the reason spelled out."""
    pin = E.vaapi_device_pin(cfg)
    if pin:
        return pin, E.hwdec_mode_for_node(pin, cfg, sysroot, devroot), \
            "playback.vaapi_device is set (explicit pin, §E.2)"
    node = E.pick_decode_node(sysroot, devroot)
    if node is None:
        return None, "no", ("no integrated non-discrete amdgpu card — §E.1 "
                            "falls back to CPU for everyone (never a "
                            "discrete GPU)")
    return node, E.hwdec_mode_for_node(node, cfg, sysroot, devroot), \
        "integrated card, last PCI slot (§E.1)"


def _discrete_nodes(sysroot: str, devroot: str) -> set:
    """Render nodes belonging to discrete cards — the set automatic choice
    may never name (§D.3)."""
    out = set()
    for slot, info in E.sysfs_cards(sysroot).items():
        if not E.discrete_gpu_slot(slot, sysroot):
            continue
        node = E.render_node_for(slot, sysroot, devroot)
        if node:
            out.add(node)
    return out


def probe(lib, node: str | None, hwdec: str, clip: str, seconds: float,
          vo: str) -> dict:
    """Run one mpv instance the way the app does and read its real state."""
    locale.setlocale(locale.LC_NUMERIC, "C")      # AGENTS.md: before create
    h = lib.mpv_create()
    if not h:
        raise RuntimeError("mpv_create failed")
    opts = {"vo": vo, "ao": "null", "config": "no", "terminal": "no",
            "loop-file": "inf", "keep-open": "no", "osd-level": "0",
            "gpu-api": "opengl",       # gpu-next segfaults against this build
            "hwdec": hwdec}
    if node:
        opts["vaapi-device"] = node
    for k, v in opts.items():
        lib.mpv_set_option_string(h, k.encode(), str(v).encode())
    if lib.mpv_initialize(h) != 0:
        lib.mpv_terminate_destroy(h)
        raise RuntimeError("mpv_initialize failed")
    cmd = M.make_command("loadfile", clip, "replace")
    if lib.mpv_command(h, cmd) != 0:
        lib.mpv_terminate_destroy(h)
        raise RuntimeError(f"loadfile {clip!r} failed")
    deadline = time.monotonic() + seconds
    seen: dict[str, str] = {}
    while time.monotonic() < deadline:
        left = min(DRAIN_WAIT_S, deadline - time.monotonic())
        if left <= 0:
            break
        lib.mpv_wait_event(h, left)
        for p in PROPS:
            val = M.get_property_string(lib, h, p)
            if val is not None and val != "":
                seen[p] = val
    lib.mpv_terminate_destroy(h)
    return seen


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--node", default=None,
                    help="render node to probe; default = what the app would "
                         "resolve (pin, else §E.1 auto)")
    ap.add_argument("--hwdec", default=None,
                    help="override the §E.3 mode (vaapi, vaapi-copy, no)")
    ap.add_argument("--clip", default=str(ROOT / CLIP))
    ap.add_argument("--duration", type=float, default=6.0)
    ap.add_argument("--vo", default="gpu", choices=("gpu", "null"),
                    help="gpu = a real window (desktop); null = headless")
    ap.add_argument("--sysroot", default="/sys")
    ap.add_argument("--devroot", default="/dev")
    args = ap.parse_args(argv)

    import config as C
    cfg = C.Config()
    for line in card_table(args.sysroot, args.devroot):
        print(line)
    node, mode, reason = auto_verdict(cfg, args.sysroot, args.devroot)
    if args.node:
        node = args.node
        mode = args.hwdec or E.hwdec_mode_for_node(node, cfg, args.sysroot,
                                                   args.devroot)
        reason = "--node override"
    else:
        mode = args.hwdec or mode
    print(f"PROBE_AUTO node={node or 'none'} hwdec={mode} reason={reason}")

    # §E.1's hard rule, re-checked on the operator's own machine: automatic
    # choice may name the iGPU or CPU, never a discrete board add-in.
    # ROUND18: the old check banned the DISPLAY GPU — but on this rig the
    # iGPU IS the display GPU, so the check fired on the correct answer.
    # Discrete-ness, not display-ness, is the mandate.
    if not args.node and not args.hwdec and node and args.devroot == "/dev":
        if node in _discrete_nodes(args.sysroot, args.devroot):
            print("PROBE_AUTO_BAN_VIOLATION node=%s is a discrete GPU"
                  % node)
            return 2

    if node is None or mode == "no":
        print("PROBE_RESULT node=none requested=no hwdec_current=no "
              "decoder=- vf_fps=0.00 engaged=1")
        print("PROBE_NOTE CPU decode is the expected outcome here; nothing "
              "was asked of the GPU")
        return 0

    try:
        lib = M.load_libmpv()
    except Exception as exc:                       # noqa: BLE001
        print(f"PROBE_ERROR libmpv: {exc}")
        return 3
    try:
        seen = probe(lib, node, mode, args.clip, args.duration, args.vo)
    except Exception as exc:                       # noqa: BLE001
        print(f"PROBE_ERROR mpv: {exc}")
        return 3
    current = seen.get("hwdec-current") or "unknown"
    # R7: one authoritative classifier for "is hardware decode actually
    # running" — the same one the app's watchdog uses, so the probe and the
    # app can never disagree about the same string.
    engaged = WD.decode_effective(current) == "on"
    print("PROBE_RESULT node=%s requested=%s hwdec_current=%s "
          "decoder=%s vf_fps=%s engaged=%d" % (
              node, mode, current,
              seen.get("current-decoder") or seen.get("video-decoder") or "-",
              seen.get("estimated-vf-fps") or "0.00", int(bool(engaged))))
    for p in PROPS:
        if p in seen:
            print(f"PROBE_PROP {p}={seen[p]}")
    if not engaged:
        print("PROBE_NOTE VAAPI did not engage on this node — the app would "
              "decode on the CPU here. The settings readout shows the same "
              "hwdec-current for the app's own windows.")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
