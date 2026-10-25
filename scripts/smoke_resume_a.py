#!/usr/bin/python3
"""Resume gate, part A (runs under smoke_resume.py inside headless-smoke.sh):
play the 10 s clip, pause at ~3.5 s, let the resume tick persist it, quit.
"""
import sys

from smoke_harness import check, finish

import reelsee

state: dict = {}
TARGET = "testdata/perf_1080p30.mp4"


def probe_schedule(app):
    state["app"] = app

    def guard(fn):
        def run(*a):
            try:
                return fn(*a)
            except Exception as exc:               # noqa: BLE001
                import traceback
                traceback.print_exc()
                check(fn.__name__ + "_crashed", False, repr(exc))
                app.quit()
                return False
        run.__name__ = fn.__name__
        return run

    def wait_for(desc, pred, then, left=60):
        try:
            val = pred()
        except Exception:                         # noqa: BLE001
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

    from gi.repository import GLib

    def a0():
        w = app.windows[0]
        # test-scale threshold: 3.5 s is far below the 300 s default
        w.cfg.set("playback.resume_min_s", 2)
        w.core.set_property("pause", True)
        w.core.command("seek", "3.5", "absolute")
        wait_for("paused_at_3.5",
                 lambda: (w.core.snapshot.get("pause")
                          and 3.3 <= float(w.core.snapshot.get("time-pos")
                                           or 0) <= 4.6) or None,
                 guard(a1))

    def a1():
        # the 5 s resume tick must have captured the position by now
        wait_for("entry_captured",
                 lambda: app.resume_store.get(TARGET) or None, guard(a2))

    def a2():
        app.quit()
        return False

    def a_start():
        w = app.windows[0]
        wait_for("loaded",
                 lambda: "perf_1080p30" in str(
                     w.core.snapshot.get("filename") or "") or None,
                 guard(a0))

    GLib.timeout_add(700, guard(a_start))
    return False


def main() -> int:
    rc = reelsee.run(["reelsee", TARGET], on_app=probe_schedule)
    finish()
    return rc


if __name__ == "__main__":
    sys.exit(main())
