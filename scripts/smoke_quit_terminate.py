#!/usr/bin/python3
"""R23 gate: quitting the app must terminate mpv, not just flush the stores.

`app.quit()` (Ctrl+Q, the quit action, the SIGTERM grace path — and every
smoke) does NOT fire a window `close-request`, so the only caller of
`core.destroy()` (`forget_window`) never ran.  The mpv handle was never
terminated: libmpv's own threads and our event thread — a PYTHON thread —
outlived the interpreter, and a thread that asks for the GIL after
finalization has started cannot get it.  That is the exit-time
`Fatal Python error: gilstate_tss_set: failed to set current tstate (TSS)`
(SIGABRT) / `Segmentation fault (core dumped)` the battery kept producing
AFTER `SMOKE_ASSERTS_PASSED`.

So this gate drives the quit path on purpose — `app.quit()`, never
`w.close()`, and it asserts the window was never closing — and then checks,
after `reelsee.run()` has returned, that the handle really was torn down and
the terminate worker was joined on the way out.  `smoke_close.py` cannot cover
this: it closes the window, which is the path that already worked.
"""
import sys
import time

from smoke_harness import check, finish

import reelsee

state: dict = {"rctx_live": False, "closing": True}


def probe_schedule(app):
    from gi.repository import GLib
    state["app"] = app

    def poll_ready():
        for w in list(app.windows):
            if w.view.rctx is not None:          # mpv is really playing
                state["rctx_live"] = True
                state["core"] = w.core
                state["win"] = w
                state["closing"] = w._closing
                app.quit()                       # the path that skipped terminate
                return False
        return True

    GLib.timeout_add(250, poll_ready)
    return False


def main() -> int:
    t0 = time.monotonic()
    rc = reelsee.run(["reelsee", "testdata/smoke_360p.mp4"],
                     on_app=probe_schedule)
    waited = time.monotonic() - t0
    app = state.get("app")
    core = state.get("core")
    workers = list(app._retiring) if app else []

    check("rctx_live_before_quit", state["rctx_live"])
    # non-vacuity: this must be the quit path, not a window close
    check("quit_without_close", not state["closing"],
          f"_closing={state['closing']}")
    check("terminate_worker_queued", len(workers) >= 1, str(len(workers)))
    check("mpv_terminated_on_quit", core is not None and core.alive is False,
          f"alive={getattr(core, 'alive', None)}")
    check("terminate_workers_joined",
          all(not t.is_alive() for t in workers),
          f"{sum(1 for t in workers if t.is_alive())} still running")
    check("quit_within_drain_cap", waited < 20.0, f"{waited:.1f}s")
    check("app_rc", rc == 0, str(rc))
    finish()
    return 0


if __name__ == "__main__":
    sys.exit(main())
