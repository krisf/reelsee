#!/usr/bin/python3
"""P8 gate smoke: 4 windows × smoke_360p concurrently.

Checks: four live windows; every one plays (duration observed) and each
gets loop-file=inf so the 3 s clip cannot reach EOF; after 6 s every
window still reports eof-reached=false; main-loop heartbeat p95 < 50 ms
across the whole concurrent stretch; focus policy transient-mutes the
three others at the mpv level; then all four close cleanly (rc 0).
(Deviation: loop-file=inf — a 3 s clip cannot otherwise still be
eof-reached=false at t=6 s; focus applied via app.on_window_focus because
Xvfb has no WM to move the X input focus.)
"""
import os
import sys

from smoke_harness import SCRATCH, Heartbeat, check, finish

CFG_DIR = os.path.join(SCRATCH, "mw-cfg")
STATE_DIR = os.path.join(SCRATCH, "mw-state")
for _d in (CFG_DIR, STATE_DIR):
    os.makedirs(_d, exist_ok=True)
os.environ["REELSEE_CONFIG_DIR"] = CFG_DIR
os.environ["REELSEE_STATE_DIR"] = STATE_DIR
_CFG = os.path.join(CFG_DIR, "config.json")
if os.path.exists(_CFG):
    os.unlink(_CFG)
with open(_CFG, "w") as _fh:
    _fh.write('{"audio": {"muted": false}}\n')

from gi.repository import GLib  # noqa: E402

import reelsee  # noqa: E402

CLIP = "testdata/smoke_360p.mp4"
N = 4
hb = None


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

    def s0():
        global hb
        hb = Heartbeat(10).start()
        check("windows_count", len(app.windows) == N, str(len(app.windows)))
        # registry sees the same roster
        check("registry_count", app.registry.count == N,
              str(app.registry.count))
        wait_for("all_loaded",
                 lambda: all(w.core.snapshot.get("duration")
                             for w in app.windows) or None,
                 guard(s1), left=100)
        return False

    def s1(_v):
        # keep the 3 s clip looping so eof-reached must stay false at t=6 s.
        # config FIRST: a late file-loaded in any window re-runs
        # _push_all_settings, which re-applies playback.loop_file from cfg
        # (the app treating config as the source of truth is correct).
        for w in app.windows:
            w.cfg.set("playback.loop_file", "inf")
            w.core.set_property("loop-file", "inf")
        # second pin just before the clip would otherwise end
        GLib.timeout_add(3000, lambda: ([
            w.core.set_property("loop-file", "inf")
            for w in app.windows], False)[1])
        GLib.timeout_add(6000, guard(s2))
        return False

    def s2():
        # R2-P4(N2): resource visibility per window (all windows share one
        # process, so RSS/threads are process-level, printed alongside the
        # per-window decode/refresh numbers) + every window must still
        # sustain >= 25 fps of displayed video at t≈6 s.
        rss_kb = -1
        try:
            with open("/proc/self/status", encoding="utf-8") as fh:
                for line in fh:
                    if line.startswith("VmRSS:"):
                        rss_kb = int(line.split()[1])
                        break
        except OSError:
            pass
        import threading
        nthreads = threading.active_count()
        check("process_rss_kb", rss_kb > 0, str(rss_kb))
        check("process_threads", 0 < nthreads < 64, str(nthreads))
        for i, w in enumerate(app.windows):
            eof = w.core.snapshot.get("eof-reached")
            check(f"eof_w{i}", eof is not True, str(eof))
            check(f"playing_w{i}", bool(w.core.snapshot.get("duration")),
                  "ok")
            try:
                fps = float(w.core.snapshot.get("estimated-vf-fps") or 0.0)
            except (TypeError, ValueError):
                fps = -1.0
            check(f"vf_fps_w{i}", fps >= 25.0, f"{fps:.1f}fps")
        p95 = hb.p95()
        hb.stop()
        check("heartbeat_p95", p95 < 50.0, f"{p95:.1f}ms")
        GLib.timeout_add(100, guard(s3))
        return False

    def s3():
        # focus policy at the REAL mpv level (no WM under Xvfb → invoke the
        # focus handler the notify::is-active wiring would call)
        first = app.windows[0]
        app.on_window_focus(first)
        wait_for("focus_mute",
                 lambda: all(o.core.snapshot.get("mute") is True
                             for o in app.windows[1:])
                 and first.core.snapshot.get("mute") is False,
                 guard(s4), left=40)
        return False

    def s4(_v):
        for w in list(app.windows):
            w.close()
        wait_for("all_closed", lambda: not app.windows or None,
                 guard(s5), left=100)
        return False

    def s5(_v):
        # GtkApplication quit-on-last-window must have fired; belt:
        GLib.timeout_add(400, lambda: app.quit() or False)
        return False

    GLib.timeout_add(600, guard(s0))
    return False


def main() -> int:
    rc = reelsee.run(["reelsee"] + [CLIP] * N, on_app=probe_schedule)
    check("app_rc", rc == 0, str(rc))
    finish()
    return 0


if __name__ == "__main__":
    sys.exit(main())
