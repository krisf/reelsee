#!/usr/bin/python3
"""R26 gate: a remembered position inside the LAST 10 % of the runtime must
NOT be restored — the file plays from 0 and the stale entry is dropped
(references/ROUND26-BRIEF.md §4 B3).

Two REAL app processes back-to-back, each with its OWN fresh config+state dir
(AGENTS.md: remembered state is shared state), on ``testdata/perf_1080p30.mp4``
(10.0 s per ffprobe, so with the default 0.10 the tail band is the last 1.0 s):

  phase A (tail)   seeded pos 9.5 s  → 0.5 s left < 0.10*10 = 1.0 s
                   ⇒ NO seek, entry dropped from the store and from
                   resume.json, and the clip really plays from 0.
  phase B (mid)    seeded pos 4.0 s  → 6.0 s left > 1.0 s
                   ⇒ still seeks back (regression guard: if the harness or
                   the seeding were broken, phase B would fail too, so phase
                   A cannot pass vacuously).

Both phases run with ``playback.resume_min_s = 1`` — with the shipped 300 s
default the entry would be skipped by the min_s gate and phase A would pass
without the tail rule ever running.  ``loop_file`` is NOT overridable
(``config.py:335`` migrates a stored ``"no"`` to the round-15 default
``"inf"``), so the clip loops and the "played from 0" assertion tracks the
MAXIMUM time-pos observed rather than one sample — a wrong seek to 9.5 s
reads ~9.5 there even though the loop wraps it back to 0.  The child spies on
``MpvCore.command`` and ``ReelseeWindow._resume_restore`` (installed before
``app.run``, so race-free) to see the actual decision.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time

from smoke_harness import ROOT, SCRATCH, check, finish

PY = "/usr/bin/python3"
CLIP = "testdata/perf_1080p30.mp4"          # 10.0 s (ffprobe)
TAIL_POS = 9.5                              # 0.5 s left  → tail
MID_POS = 4.0                               # 6.0 s left  → normal resume
# resume_min_s MUST be lowered: with the shipped 300 s default the entry is
# skipped by that gate and phase A would pass without the tail rule ever
# running.  loop_file is NOT overridable — config.py:335 migrates a stored
# "no" to the round-15 default "inf" — so the clip loops, and the phase A
# "played from 0" assertion tracks the MAXIMUM time-pos observed (a wrong
# seek to 9.5 s shows ~9.5 there even though the loop would wrap it to 0).
CONFIG = '{"playback": {"resume_min_s": 1}}\n'


# ---------------------------------------------------------------- child side
def run_child(phase: str) -> int:
    """One real app process; the assertions below are the in-process ones."""
    import reelsee
    from gi.repository import GLib
    import window as W

    seen: dict = {"seeks": [], "entries": [], "maxpos": 0.0, "watch": 0}
    key = CLIP                                   # store key == argv path

    def on_app(app):
        # Spy on the CORE command writer, not on a window helper: it exists
        # in every version of the app, it is installed before app.run() so
        # there is no race with the window being built, and "was a seek
        # command issued?" is exactly the contract.  Nothing else in this
        # controlled process seeks at startup.
        real_cmd = W.MpvCore.command
        real_restore = W.ReelseeWindow._resume_restore

        def spy_cmd(self, *args):
            if args and str(args[0]) == "seek":
                try:
                    seen["seeks"].append(float(args[1]))
                except (TypeError, ValueError):
                    seen["seeks"].append(args[1])
            return real_cmd(self, *args)

        def spy_restore(self):
            # what the store held when the restore path ran: proves the seed
            # was loaded and that the TAIL RULE (not an absent entry, not the
            # min_s gate) is what stopped the seek
            seen["entries"].append(self.app.resume_store.get(str(self.target)))
            return real_restore(self)

        W.MpvCore.command = spy_cmd
        W.ReelseeWindow._resume_restore = spy_restore

        def guard(fn):
            def run(*a):
                try:
                    return fn(*a)
                except Exception as exc:                    # noqa: BLE001
                    import traceback
                    traceback.print_exc()
                    check(fn.__name__ + "_crashed", False, repr(exc))
                    app.quit()
                    return False
            run.__name__ = fn.__name__
            return run

        def wait_for(desc, pred, then, left=150):
            try:
                val = pred()
            except Exception:                               # noqa: BLE001
                val = None
            if val:
                check(desc, True, repr(val) if val is not True else "ok")
                then()
                return False
            if left <= 0:
                check(desc, False, "timeout")
                app.quit()
                return False
            GLib.timeout_add(100, wait_for, desc, pred, then, left - 1)
            return False

        def watch_pos():
            """Highest time-pos seen since the file loaded.  The clip LOOPS
            (config.py:335 migrates a stored loop_file "no" to "inf"), so a
            wrong seek to 9.5 s would wrap back to ~0 and a single late
            sample could look like 'played from 0' — the maximum cannot."""
            try:
                p = float(app.windows[0].core.snapshot.get("time-pos") or 0)
            except (TypeError, ValueError, IndexError):
                p = 0.0
            seen["maxpos"] = max(seen["maxpos"], p)
            if seen["watch"] <= 0:
                return False
            seen["watch"] -= 1
            GLib.timeout_add(50, watch_pos)
            return False

        def after_decision():
            w = app.windows[0]
            store = app.resume_store
            check("seed_reached_restore_path",
                  bool(seen["entries"])
                  and isinstance(seen["entries"][0], dict)
                  and float(seen["entries"][0].get("pos") or 0) > 0,
                  repr(seen["entries"]))
            if phase == "A":
                check("tail_no_resume_seek", seen["seeks"] == [],
                      f"seeks={seen['seeks']}")
                check("tail_entry_dropped_in_memory",
                      store.get(key) is None, repr(store.get(key)))
                seen["watch"] = 60            # keep watching while it plays
                # play for >1 s before judging, so the maximum is a real
                # observation and not one early sample
                wait_for("tail_playing",
                         lambda: float(w.core.snapshot.get("time-pos") or 0)
                         >= 1.2 or None, guard(tail_plays_from_zero))
            else:
                seen["watch"] = 1
                check("mid_resume_seek_issued", seen["seeks"] == [MID_POS],
                      f"seeks={seen['seeks']}")
                check("mid_entry_kept_in_memory", store.get(key) is not None,
                      repr(store.get(key)))
                wait_for("mid_resumed",
                         lambda: float(w.core.snapshot.get("time-pos") or 0)
                         >= 3.5 or None, guard(mid_done))

        def tail_plays_from_zero():
            final = float(app.windows[0].core.snapshot.get("time-pos") or 0)
            # max(), because the watcher can lag the poller by one tick: the
            # value the poller itself saw is part of the observation
            worst = max(seen["maxpos"], final)
            check("tail_plays_from_zero", worst < 2.5 and final >= 1.2,
                  f"max time-pos={worst:.2f} now={final:.2f} "
                  "(a wrong seek would show ~9.5)")
            app.quit()
            return False

        def mid_done():
            w = app.windows[0]
            pos = float(w.core.snapshot.get("time-pos") or 0)
            check("mid_resumed_plausible", 3.5 <= pos <= 6.5, f"pos={pos:.2f}")
            app.quit()
            return False

        def on_loaded():
            w = app.windows[0]
            # the override really applied — otherwise the shipped 300 s
            # resume_min_s would skip the entry before the tail rule was ever
            # consulted and phase A would pass vacuously
            min_s = w.cfg.get("playback.resume_min_s")
            check("cfg_override_applied", float(min_s or -1) == 1.0,
                  f"resume_min_s={min_s}")
            seen["watch"] = 40                # ~2 s of time-pos observation
            GLib.timeout_add(50, watch_pos)
            wait_for("decision_made",
                     lambda: bool(seen["seeks"])
                     or app.resume_store.get(key) is None or None,
                     guard(after_decision))

        def start():
            w = app.windows[0]
            wait_for("loaded",
                     lambda: "perf_1080p30" in str(
                         w.core.snapshot.get("filename") or "") or None,
                     guard(on_loaded))

        GLib.timeout_add(400, guard(start))
        return False

    rc = reelsee.run(["reelsee", CLIP], on_app=on_app)
    finish()
    return rc


# ---------------------------------------------------------------- parent side
def seed(dirs: dict, pos: float) -> None:
    for d in dirs.values():
        shutil.rmtree(d, ignore_errors=True)
        os.makedirs(d)
    with open(os.path.join(dirs["cfg"], "config.json"), "w",
              encoding="utf-8") as fh:
        fh.write(CONFIG)
    with open(os.path.join(dirs["state"], "resume.json"), "w",
              encoding="utf-8") as fh:
        json.dump({CLIP: {"pos": pos}}, fh)


def resume_file(dirs: dict):
    p = os.path.join(dirs["state"], "resume.json")
    try:
        with open(p, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def one(phase: str, pos: float, tag: str) -> None:
    dirs = {"cfg": os.path.join(SCRATCH, f"r26-{tag}-cfg"),
            "state": os.path.join(SCRATCH, f"r26-{tag}-state")}
    seed(dirs, pos)
    env = dict(os.environ, REELSEE_CONFIG_DIR=dirs["cfg"],
               REELSEE_STATE_DIR=dirs["state"])
    t0 = time.monotonic()
    try:
        r = subprocess.run([PY, os.path.abspath(__file__), "--phase", phase],
                           cwd=str(ROOT), env=env, capture_output=True,
                           text=True, timeout=80)
    except subprocess.TimeoutExpired:
        check(f"{tag}_child_exit", False, "TIMEOUT")
        return
    print(r.stdout, end="", flush=True)
    if r.stderr:
        print("---- stderr tail ----")
        print("\n".join(r.stderr.splitlines()[-12:]))
    check(f"{tag}_child_exit", r.returncode == 0
          and "SMOKE_ASSERTS_PASSED" in r.stdout,
          f"rc={r.returncode} {time.monotonic() - t0:.1f}s")

    data = resume_file(dirs)
    entries = [v.get("pos") for v in (data or {}).values()
               if isinstance(v, dict)]
    if phase == "A":
        # the shutdown flush rewrites the whole store, so the seeded tail
        # position must be gone from disk (a fresh entry at the NEW position
        # is expected — the clip is playing from 0 — but never the seed)
        gone = not any(isinstance(p, (int, float))
                       and abs(p - TAIL_POS) < 1.0 for p in entries)
        check("tail_seed_gone_from_disk", gone, f"resume.json={data}")
    else:
        kept = any(isinstance(p, (int, float)) and p >= 3.5 for p in entries)
        check("mid_position_persisted", kept, f"resume.json={data}")


def main(argv: list[str]) -> int:
    if "--phase" in argv:
        return run_child(argv[argv.index("--phase") + 1])
    one("A", TAIL_POS, "tail")
    one("B", MID_POS, "mid")
    finish()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
