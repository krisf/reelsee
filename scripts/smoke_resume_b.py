#!/usr/bin/python3
"""Resume gate, part B (runs under smoke_resume.py): same state dir as part
A — the fresh window must auto-seek back to ~3.5 s (§7 resume position)."""
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

    def b1():
        w = app.windows[0]
        wait_for("auto_resumed",
                 lambda: float(w.core.snapshot.get("time-pos") or 0) >= 3.0
                 or None, guard(b2))

    def b2():
        w = app.windows[0]
        pos = float(w.core.snapshot.get("time-pos") or 0)
        check("resume_plausible", pos <= 6.0, f"pos={pos:.2f}")
        app.quit()
        return False

    def b_start():
        w = app.windows[0]
        wait_for("loaded",
                 lambda: "perf_1080p30" in str(
                     w.core.snapshot.get("filename") or "") or None,
                 guard(b1))

    GLib.timeout_add(700, guard(b_start))
    return False


def main() -> int:
    rc = reelsee.run(["reelsee", TARGET], on_app=probe_schedule)
    finish()
    return rc


if __name__ == "__main__":
    sys.exit(main())
