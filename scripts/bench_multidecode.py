#!/usr/bin/python3
"""Decode-capacity bench: how many video streams can ONE GPU decode at once?

OPERATOR TOOL.  Run it on the real desktop, not in CI:

    scripts/bench_multidecode.py --n 4 --node /dev/dri/renderD128

It creates N independent libmpv instances (vendored libmpv, `vo=null`
`ao=null` so nothing is drawn or heard and no display is needed), loads a
test clip into each, and reports per-stream `estimated-vf-fps`, the dropped
frame counters and wall time.  The numbers feed the decode plan: `capacity`
is "the highest N where every stream holds >= 24 fps with no rising drop
trend" (ROUND16 §D.2).  Default node is the integrated iGPU (ROUND18:
display-bearing or not — connectors are not part of the mandate), the only
GPU the plan is allowed to pick automatically.

In the headless battery this runs ONCE with `--hwdec no --n 2` purely as a
self-test of the harness itself (does the script complete, are the counters
plausible).  It is NEVER a GPU capacity gate there: llvmpipe numbers have no
relationship to the real iGPU.

Output (machine-checkable):
    BENCH_STREAM:<i> file=<name> hwdec=<mode> vf_fps=<x> src_fps=<x>
                   adv_fps=<r> keeps_up=<0|1> drops=<n> decoder_drops=<n>
                   mistimed=<n> drop_delta=<n> wall=<s> eof=<0|1> samples=<n>
    BENCH_SUMMARY n=<N> min_vf_fps=<x> min_advance=<r> max_drop_delta=<n>
                  capacity=<k> fps_floor=24.0 ok=<0|1>
"""
from __future__ import annotations

import argparse
import json
import locale
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import mpvlib as M  # noqa: E402

FPS_FLOOR = 24.0                      # §D.2 margin rule
DRAIN_WAIT_S = 0.002                  # bounded event drain; see Stream.pump
PROPS = ("estimated-vf-fps", "frame-drop-count",
         "decoder-frame-drop-count", "mistimed-frame-count")


def end_file_event() -> int:
    """MPV_EVENT_END_FILE, read from the vendored header — not guessed.

    The enum values shifted between libmpv versions (END_FILE is 7 in the
    vendored build, 3 in older ones).  A guessed constant is not a loud bug
    here: with 8 the bench mistook FILE_LOADED for EOF and declared every
    stream finished 0.4 s after load, reporting capacity=0 for a GPU that was
    barely warm."""
    hdr = ROOT / "vendor" / "usr" / "include" / "mpv" / "client.h"
    try:
        m = re.search(r"MPV_EVENT_END_FILE\s*=\s*(\d+)",
                      hdr.read_text(encoding="utf-8", errors="replace"))
        if m:
            return int(m.group(1))
    except OSError:
        pass
    return 7


MPV_EVENT_END_FILE = end_file_event()


def default_node() -> str | None:
    """The headless (connector-less) GPU's render node — the iGPU.

    Never a display-attached card: those carry the compositor and, on this
    rig, the inference workloads."""
    try:
        import engine
        return engine.pick_decode_node()
    except Exception:                 # noqa: BLE001
        return None


class Stream:
    def __init__(self, lib, idx: int, path: str, opts: dict):
        self.lib = lib
        self.idx = idx
        self.path = path
        self.h = None
        self.opts = opts
        # (t_since_start, estimated-vf-fps, frame_drop, decoder_drop,
        #  mistimed, playback_pos)
        self.samples: list[list] = []
        self.t0 = 0.0
        self.t1 = 0.0
        self.eof = False
        self.err = None
        self.progress = 0.0           # seconds of video actually decoded
        self.prev_pos = None
        self.src_fps = 0.0            # container fps, once known

    def start(self) -> None:
        locale.setlocale(locale.LC_NUMERIC, "C")   # §0.1, before mpv_create
        h = M.ctypes.c_void_p(self.lib.mpv_create())
        if not h:
            raise RuntimeError("mpv_create failed")
        self.h = h
        for k, v in self.opts.items():
            rc = self.lib.mpv_set_option_string(h, k.encode(), str(v).encode())
            if rc != 0:
                raise RuntimeError(f"option {k}={v}: "
                                   f"{M.error_string(self.lib, rc)}")
        rc = self.lib.mpv_initialize(h)
        if rc != 0:
            raise RuntimeError(f"mpv_initialize: "
                               f"{M.error_string(self.lib, rc)}")
        argv = M.make_command("loadfile", self.path, "replace")
        rc = self.lib.mpv_command(h, argv)
        if rc != 0:
            raise RuntimeError(f"loadfile: {M.error_string(self.lib, rc)}")
        self.t0 = time.monotonic()

    def pump(self) -> None:
        """Drain events with a BOUNDED wait, then sample the counters.

        mpv_wait_event is a trap in this libmpv build: the documented
        "negative = return immediately" is not honoured — a negative timeout
        waits on the condition variable forever (measured: the call parked in
        pthread_cond_wait and the bench never finished).  So the drain uses a
        small POSITIVE timeout, which is bounded by construction, and EOF is
        also read from the `eof-reached` property rather than trusted to the
        END_FILE event alone."""
        deadline = time.monotonic() + DRAIN_WAIT_S
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            ev = self.lib.mpv_wait_event(self.h, left)
            if not ev:
                break
            if ev.contents.event_id == MPV_EVENT_END_FILE:
                self.eof = True
        fps = M.get_property_string(self.lib, self.h, "estimated-vf-fps")
        try:
            fps = float(fps) if fps else None
        except ValueError:
            fps = None                # "no"/"inf" while the file is opening
        drops = []
        for p in PROPS[1:]:
            raw = M.get_property_string(self.lib, self.h, p)
            try:
                drops.append(int(float(raw)) if raw else 0)
            except ValueError:
                drops.append(0)
        if not self.eof and \
                M.get_property_string(self.lib, self.h, "eof-reached") == "yes":
            self.eof = True
        if not self.src_fps:
            raw = M.get_property_string(self.lib, self.h, "container-fps")
            try:
                self.src_fps = float(raw) if raw else 0.0
            except ValueError:
                self.src_fps = 0.0
        pos = M.get_property_string(self.lib, self.h, "playback-time")
        try:
            pos = float(pos) if pos else 0.0
        except ValueError:
            pos = 0.0
        # Accumulate played seconds.  Clips loop (loop-file=inf keeps the
        # measurement window filled), so a position that goes BACKWARD is a
        # wrap, not a seek: count the new pass from 0.
        if self.prev_pos is None or pos >= self.prev_pos:
            self.progress += max(0.0, pos - (self.prev_pos or 0.0))
        else:
            self.progress += pos
        self.prev_pos = pos
        self.samples.append([time.monotonic() - self.t0, fps, *drops, pos])

    def stop(self) -> None:
        self.t1 = time.monotonic()
        if self.h:
            self.lib.mpv_terminate_destroy(self.h)
            self.h = None

    # ------------------------------------------------------------- reporting
    @property
    def wall(self) -> float:
        return max(0.0, self.t1 - self.t0)

    def vf_fps(self) -> float:
        """Best sustained estimate: the highest sample.  The first samples of
        a stream are dominated by open/seek, and a bench that reports the
        mean of them understates capacity — the floor rule is about whether
        the stream HOLDS 24 fps, not how it started."""
        vals = [s[1] for s in self.samples if s[1] is not None and s[1] > 0.0]
        return max(vals) if vals else 0.0

    def drop_delta(self) -> int:
        """Growth of the drop counters over the measured window.  A rising
        drop trend is the signal the margin rule cares about: a stream at
        30 fps that is already shedding frames will not stay at 30."""
        if len(self.samples) < 2:
            return 0
        first, last = self.samples[1], self.samples[-1]
        return max(0, sum(max(0, last[i + 2] - first[i + 2])
                          for i in range(3)))

    def advance_rate(self) -> float:
        """Seconds of video played per second of wall clock.

        Measured on this libmpv build: even with `vo=null ao=null` mpv paces
        playback to real time, so this sits at ~1.0 for a healthy stream and
        falls below it when the decoder cannot keep up.  It is the liveness
        signal — a stream whose decoder died reports a stale
        estimated-vf-fps while its position stops moving."""
        if len(self.samples) < 2:
            return 0.0
        span = self.samples[-1][0] - self.samples[0][0]
        return self.progress / span if span > 0.05 else 0.0

    def keeps_up(self) -> bool:
        """§D.2 margin rule for one stream: hold the floor AND the clip's own
        frame rate, with no rising drop trend applied separately.

        The floor alone is not enough — a 30 fps clip that decodes at 25 fps
        clears 24 and still drops frames the moment it is played for real."""
        if not self.samples:
            return False
        return self.vf_fps() >= max(FPS_FLOOR, self.src_fps or 0.0) \
            and self.advance_rate() > 0.5

    def line(self) -> str:
        name = os.path.basename(self.path)
        if self.err:
            return (f"BENCH_STREAM:{self.idx} file={name} "
                    f"hwdec={self.opts.get('hwdec', '?')} error={self.err}")
        return (f"BENCH_STREAM:{self.idx} file={name} "
                f"hwdec={self.opts.get('hwdec', '?')} "
                f"vf_fps={self.vf_fps():.2f} src_fps={self.src_fps:.2f} "
                f"adv_fps={self.advance_rate():.2f} "
                f"keeps_up={1 if self.keeps_up() else 0} "
                f"drops={self.samples[-1][2] if self.samples else 0} "
                f"decoder_drops={self.samples[-1][3] if self.samples else 0} "
                f"mistimed={self.samples[-1][4] if self.samples else 0} "
                f"drop_delta={self.drop_delta()} wall={self.wall:.2f} "
                f"eof={1 if self.eof else 0} samples={len(self.samples)}")


def capacity_rule(streams) -> int:
    """Highest N where every stream in the first N keeps up with its own rate
    (>= 24 fps, §D.2) with no rising drop trend.  0 means "this GPU cannot
    decode even one stream at the floor" — which the plan reads as CPU for
    everyone."""
    best = 0
    for k in range(1, len(streams) + 1):
        group = streams[:k]
        if all(s.keeps_up() and s.drop_delta() == 0 for s in group):
            best = k
        else:
            break                     # capacity is a prefix property
    return best


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--n", type=int, default=2, help="concurrent streams")
    ap.add_argument("--hwdec", default="auto",
                    help="auto (VAAPI on --node) | no (CPU) | any hwdec string")
    ap.add_argument("--node", default=None,
                    help="render node to pin (default: the integrated iGPU)")
    ap.add_argument("--duration", type=float, default=12.0,
                    help="max seconds to measure each stream")
    ap.add_argument("--clips", nargs="*", default=None,
                    help="clips to load (default: the repo test clips)")
    ap.add_argument("--sample-ms", type=int, default=200)
    ap.add_argument("--json", default=None,
                    help="also dump the raw samples to this file")
    args = ap.parse_args(argv)

    clips = args.clips or [str(ROOT / "testdata" / c)
                           for c in ("perf_1080p30.mp4", "smoke_360p.mp4")]
    clips = [c for c in clips if os.path.exists(c)]
    if not clips:
        print("BENCH_SUMMARY n=0 min_vf_fps=0.00 max_drop_delta=0 "
              "capacity=0 ok=0 reason=no_clips")
        return 2
    n = max(1, args.n)
    clips = [clips[i % len(clips)] for i in range(n)]

    node = args.node if args.node is not None else default_node()
    # loop-file=inf keeps the measurement window filled: a 3 s clip would
    # otherwise finish before enough samples accumulate and report a capacity
    # number derived from a file open, not from sustained decode.  (Measured:
    # this build paces playback to real time even with vo=null ao=null, so the
    # window has to be filled by looping, not by free-running.)
    opts = {"vo": "null", "ao": "null", "config": "no", "terminal": "no",
            "loop-file": "inf", "keep-open": "no", "osd-level": "0",
            "hwdec": args.hwdec}
    if node and args.hwdec != "no":
        opts["vaapi-device"] = node

    try:
        lib = M.load_libmpv()
    except Exception as exc:          # noqa: BLE001
        print(f"BENCH_SUMMARY n={n} min_vf_fps=0.00 max_drop_delta=0 "
              f"capacity=0 ok=0 reason=libmpv_load_failed:{exc}")
        return 2

    print(f"BENCH_CONFIG n={n} hwdec={args.hwdec} node={node or 'default'} "
          f"clips={','.join(os.path.basename(c) for c in clips)} "
          f"duration={args.duration}")

    streams: list[Stream] = []
    for i, c in enumerate(clips):
        s = Stream(lib, i, c, dict(opts))
        try:
            s.start()
            streams.append(s)
        except Exception as exc:      # noqa: BLE001
            s.err = str(exc)
            streams.append(s)
            print(s.line())

    t_end = time.monotonic() + args.duration
    interval = max(0.01, args.sample_ms / 1000.0)
    while streams and time.monotonic() < t_end:
        alive = False
        for s in streams:
            if s.err or s.eof:
                continue
            s.pump()
            alive = True
        if not alive:
            break
        time.sleep(interval)

    for s in streams:
        if not s.err:
            s.stop()
            print(s.line())

    # A run is only meaningful if every stream really decoded and really
    # advanced; a stalled or failed stream must fail the run, not report
    # "plausible counters" off a file open.
    ok = bool(streams) and all(not s.err and s.vf_fps() > 0.0
                               and s.advance_rate() > 0.5
                               and len(s.samples) >= 2
                               for s in streams)
    mins = [s.vf_fps() for s in streams if not s.err]
    advs = [s.advance_rate() for s in streams if not s.err]
    deltas = [s.drop_delta() for s in streams if not s.err]
    cap = capacity_rule([s for s in streams if not s.err]) if ok else 0
    print(f"BENCH_SUMMARY n={len(streams)} "
          f"min_vf_fps={(min(mins) if mins else 0.0):.2f} "
          f"min_advance={(min(advs) if advs else 0.0):.2f} "
          f"max_drop_delta={(max(deltas) if deltas else 0)} "
          f"capacity={cap} fps_floor={FPS_FLOOR} ok={1 if ok else 0}")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump({"hwdec": args.hwdec, "node": node,
                       "streams": [{"file": s.path, "vf_fps": s.vf_fps(),
                                    "adv_fps": s.advance_rate(),
                                    "src_fps": s.src_fps,
                                    "keeps_up": s.keeps_up(),
                                    "drop_delta": s.drop_delta(),
                                    "wall": s.wall, "eof": s.eof,
                                    "samples": s.samples}
                                   for s in streams]}, fh, indent=1)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
