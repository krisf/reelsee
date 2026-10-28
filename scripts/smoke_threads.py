#!/usr/bin/python3
"""R16-C gate: the CPU decode-thread budget follows the number of LIVE
windows, at runtime, not just at window open.

Before this change `vd-lavc-threads` was decided once, when a window opened,
from the window count AT THAT INSTANT — so opening a second window left the
first one still asking lavc for the whole machine.  The budget is now
`engine.threads_for(n, cores)` (pure, truth-table unit test in
tests/test_threads.py) and it is recomputed whenever the set of CPU-decoded
windows changes: a window opens, a window closes, or a window LEARNS its
decode path (hwdec-current is unknown until the first frame decodes, so the
open-time pass alone would skip every window in the process).

Checks, in order:
  auto_for_single_window      one window: no `vd-lavc-threads` write at all
  budget_written_on_open      a second window opens -> BOTH windows are given
                              their share (the first one is the regression)
  budget_value_matches_rule   the value is the brief's formula, spelled out
                              here independently of engine.threads_for
  one_write_per_window        the writer is idempotent, not a per-tick storm
  close_never_writes_auto     closing a window leaves the survivor's value
                              alone (mpv has no way back to auto, so writing
                              0 would be a lie — the budget simply stops
                              growing)
"""
import json
import os
import sys

from smoke_harness import SCRATCH, check, finish

CFG = SCRATCH / "threads-cfg"
CFG.mkdir(parents=True, exist_ok=True)
(CFG / "config.json").write_text(json.dumps({
    "engine": {"decoder": "cpu", "watchdog_enabled": False},
    "ui": {"chrome_timeout_ms": 600000},
}))
os.environ["REELSEE_CONFIG_DIR"] = str(CFG)
os.environ["REELSEE_STATE_DIR"] = str(SCRATCH / "threads-state")

from gi.repository import GLib  # noqa: E402

import engine as E  # noqa: E402
import reelsee  # noqa: E402

CLIP = "testdata/smoke_360p.mp4"
CORES = os.cpu_count() or 2
# The brief's rule, written out rather than imported, so the gate cannot be
# satisfied by a bug shared between the gate and the implementation.
WANT2 = min(16, max(2, CORES // (2 * 2)))


def spy_core(w):
    """Record every mpv property write this window makes."""
    core = w.core
    if not hasattr(core, "_r16_writes"):
        core._r16_writes = []
        real = core.set_property

        def rec(name, value):
            core._r16_writes.append((str(name), value))
            return real(name, value)

        core.set_property = rec
    return core._r16_writes


def thread_writes(w):
    return [(n, v) for n, v in getattr(w.core, "_r16_writes", [])
            if n == "vd-lavc-threads"]


def probe_schedule(app):
    from types import SimpleNamespace
    st = SimpleNamespace()

    def poll(name, every, deadline_s, cond, then):
        tries = {"n": 0}

        def step():
            if cond():
                then()
                return False
            tries["n"] += 1
            if tries["n"] * every >= deadline_s * 1000:
                check(name, False,
                      f"no within {deadline_s}s; "
                      f"lavc={[w._lavc_threads for w in app.windows]} "
                      f"hwdec={[w.core.snapshot.get('hwdec-current')
                              for w in app.windows]}")
                app.quit()
                return False
            GLib.timeout_add(every, step)
            return False

        GLib.timeout_add(every, step)

    def later(ms, fn):
        GLib.timeout_add(ms, lambda: (fn(), False)[1])

    # ------------------------------------------------------------------ 1
    def s0():
        check("started_with_one_window", len(app.windows) == 1,
              str(len(app.windows)))
        w = app.windows[0]
        spy_core(w)
        poll("first_window_decoding", 100, 30.0,
             lambda: w._cpu_decoding(), s1)

    def s1():
        w = app.windows[0]
        check("auto_for_single_window", w._lavc_threads is None,
              repr(w._lavc_threads))
        check("single_window_wrote_no_budget", not thread_writes(w),
              f"writes={thread_writes(w)}")
        st.w1 = w
        app._spawn_window().open_target(CLIP)
        poll("second_window_decoding", 100, 30.0,
             lambda: len(app.windows) >= 2 and app.windows[1]._cpu_decoding(),
             s2)

    # ------------------------------------------------------------------ 2
    def s2():
        w1, w2 = app.windows[0], app.windows[1]
        spy_core(w2)
        poll("budget_written_on_open", 100, 15.0,
             lambda: w1._lavc_threads is not None
             and w2._lavc_threads is not None, s3)

    def s3():
        w1, w2 = app.windows[0], app.windows[1]
        check("budget_written_on_open",
              w1._lavc_threads == str(WANT2)
              and w2._lavc_threads == str(WANT2),
              f"w1={w1._lavc_threads} w2={w2._lavc_threads} want={WANT2} "
              f"(cores={CORES})")
        check("budget_value_matches_rule",
              int(w1._lavc_threads) == WANT2
              and int(w1._lavc_threads) == E.threads_for(2, CORES),
              f"{w1._lavc_threads} vs threads_for(2,{CORES})="
              f"{E.threads_for(2, CORES)}")
        check("one_write_per_window",
              len(thread_writes(w1)) == 1 and len(thread_writes(w2)) <= 1,
              f"w1={thread_writes(w1)} w2={thread_writes(w2)}")
        later(1200, s4)

    def s4():
        """Idempotence: the budget must not be rewritten every snapshot."""
        w1, w2 = app.windows[0], app.windows[1]
        check("no_write_storm",
              len(thread_writes(w1)) == 1 and len(thread_writes(w2)) <= 1,
              f"w1={thread_writes(w1)} w2={thread_writes(w2)}")
        st.before = w1._lavc_threads
        w2.close()
        poll("second_window_gone", 100, 20.0,
             lambda: len(app.windows) == 1, s5)

    def s5():
        w1 = app.windows[0]
        check("close_never_writes_auto",
              w1._lavc_threads == st.before,
              f"{st.before} -> {w1._lavc_threads}")
        check("survivor_still_decoding", w1._cpu_decoding(),
              repr(w1.core.snapshot.get("hwdec-current")))
        later(800, s6)

    def s6():
        check("still_no_write_after_close",
              len(thread_writes(app.windows[0])) == 1,
              f"writes={thread_writes(app.windows[0])}")
        app.quit()

    GLib.timeout_add(600, s0)
    return False


def main() -> int:
    rc = reelsee.run(["reelsee", CLIP], on_app=probe_schedule)
    check("app_rc", rc == 0, str(rc))
    finish()
    return 0


if __name__ == "__main__":
    sys.exit(main())
