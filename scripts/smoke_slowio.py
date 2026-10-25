#!/usr/bin/env python3
"""P9 gate smoke: REELSEE_FAKE_LATENCY_MS=3000 must not freeze the UI.

A scratch library folder is opened as target; the folder scan completes
only after the injected 3 s latency.  The window must still be presented
< 400 ms after app startup, the main-loop heartbeat p95 must stay under
50 ms across the whole delayed stretch, the playlist must only appear
AFTER the latency (proving the scan really deferred), and the next-entry
ffprobe (also delayed) must land with real metadata.  Then clean close.
"""
import os
import shutil
import sys
import time

from smoke_harness import SCRATCH, Heartbeat, check, finish

CFG_DIR = os.path.join(SCRATCH, "slowio-cfg")
STATE_DIR = os.path.join(SCRATCH, "slowio-state")
LIB = os.path.join(SCRATCH, "slowio-lib")
for _d in (CFG_DIR, STATE_DIR, LIB):
    os.makedirs(_d, exist_ok=True)
_CFG = os.path.join(CFG_DIR, "config.json")
if os.path.exists(_CFG):
    os.unlink(_CFG)
for f in os.listdir(LIB):
    os.unlink(os.path.join(LIB, f))
shutil.copy("testdata/smoke_360p.mp4", os.path.join(LIB, "1.mp4"))
shutil.copy("testdata/tone_5s.mkv", os.path.join(LIB, "2.mkv"))
shutil.copy("testdata/smoke_360p.mp4", os.path.join(LIB, "3.mp4"))

os.environ["REELSEE_CONFIG_DIR"] = CFG_DIR
os.environ["REELSEE_STATE_DIR"] = STATE_DIR
os.environ["REELSEE_FAKE_LATENCY_MS"] = "3000"

from gi.repository import GLib  # noqa: E402

import reelsee  # noqa: E402

hb = None
t0 = None
state = {"shown_ms": None, "deferred": None, "scanned": None}


def probe_schedule(app):
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

    def wait_for(desc, pred, then, left=30):
        val = None
        try:
            val = pred()
        except Exception:                         # noqa: BLE001
            val = None
        if val:
            check(desc, True, repr(val) if val is not True else "ok")
            then(val)
            return False
        if left <= 0:
            check(desc, False, "timeout")
            app.quit()
            return False
        GLib.timeout_add(100, wait_for, desc, pred, then, left - 1)
        return False

    def watch_shown(left=80):
        if app.windows and app.windows[0].get_mapped():
            state["shown_ms"] = (time.monotonic() - t0) * 1000.0
            return False
        if left > 0:
            GLib.timeout_add(10, watch_shown, left - 1)
        return False

    def s0():
        global hb, t0
        t0 = time.monotonic()
        hb = Heartbeat(10).start()
        watch_shown()
        GLib.timeout_add(2500, guard(s1))
        return False

    def s1():
        w = app.windows[0]
        ms = state["shown_ms"]
        check("window_shown_under_400ms",
              ms is not None and ms < 400.0,
              f"{ms:.0f}ms" if ms is not None else "never")
        pc = w.core.snapshot.get("playlist-count") or 0
        deferred = not w.playlist and pc == 0
        state["deferred"] = deferred
        check("scan_deferred_by_latency", deferred,
              f"playlist={bool(w.playlist)} mpv_count={pc}")
        GLib.timeout_add(2200, guard(s2))
        return False

    def s2():
        w = app.windows[0]
        n = len(w.playlist or [])
        state["scanned"] = n
        check("scan_completed_after_latency", n >= 3, str(n))
        check("first_file_loaded", bool(w.core.snapshot.get("duration")),
              str(w.core.snapshot.get("duration")))
        GLib.timeout_add(600, guard(s3))
        return False

    def s3():
        p95 = hb.p95()
        hb.stop()
        check("heartbeat_p95", p95 < 50.0, f"{p95:.1f}ms")
        alive = (p95 < 50.0 and state["deferred"]
                 and state["shown_ms"] is not None
                 and state["shown_ms"] < 400.0 and state["scanned"])
        check("ui_alive_during_slow_io", bool(alive), str(bool(alive)))
        w = app.windows[0]
        wait_for("probe_metadata",
                 lambda: w.next_probe_info and w.next_probe_info or None,
                 guard(s4), left=60)
        return False

    def s4(info):
        dur = info.get("duration")
        check("probe_next_entry",
              dur is not None and 4.5 <= dur <= 5.6
              and info["path"].endswith("2.mkv"),
              f"{info.get('path','?').rsplit('/',1)[-1]} "
              f"dur={dur}")
        GLib.timeout_add(200, guard(s5))
        return False

    def s5():
        for w in list(app.windows):
            w.close()

        def closer(_v=None):
            app.quit()

        wait_for("closed", lambda: not app.windows or None,
                 guard(closer), left=60)
        return False

    GLib.timeout_add(50, guard(s0))
    return False


def main() -> int:
    rc = reelsee.run(["reelsee", LIB], on_app=probe_schedule)
    check("app_rc", rc == 0, str(rc))
    finish()
    return 0


if __name__ == "__main__":
    sys.exit(main())
