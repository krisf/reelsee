#!/usr/bin/python3
"""P2 gate smoke: real app plays perf_1080p30.mp4 under Xvfb.

Checks (PLAN §8 P2): pixels land in the window (mean>8, ≥200 colors on the
640x360+0+0 crop), time-pos advances, hwdec-current=no (R13), estimated-vf-fps
>= 20, pause holds, seek jumps, F toggles fullscreen — all through the app's
own action paths.
"""
import sys

from smoke_harness import (SCRATCH, capture_xwd, check, finish, image_colors,
                           image_mean)

import reelsee

state = {}


def probe_schedule(app):
    from gi.repository import GLib
    GLib.timeout_add(1500, lambda: step(app))
    return False


def snap(win, key, default=None):
    return win.core.snapshot.get(key, default)


def step(app):
    from gi.repository import GLib
    wins = app.windows
    check("window_open", len(wins) == 1, str(len(wins)))
    if not wins:
        app.quit()
        return False
    win = wins[0]
    state["win"] = win

    xwd = SCRATCH / "p2_capture.xwd"
    cap_ok = capture_xwd(xwd)
    mean = image_mean(xwd) if cap_ok else -1
    colors = image_colors(xwd) if cap_ok else -1
    check("capture", cap_ok, str(cap_ok))
    check("mean_luma", mean > 8, f"{mean:.1f}")
    check("unique_colors", colors >= 200, str(colors))

    t1 = float(snap(win, "time-pos") or -1)
    check("timepos_started", t1 > 0, f"{t1:.2f}")
    check("hwdec_no", snap(win, "hwdec-current") in (None, "no"),
          str(snap(win, "hwdec-current")))
    vf = float(snap(win, "estimated-vf-fps") or 0)
    check("vf_fps_ge_20", vf >= 20, f"{vf:.1f}")

    # pause via the app's own action
    win._actions["play-pause"](None)
    GLib.timeout_add(600, lambda: pause_phase(app))
    return False


def pause_phase(app):
    from gi.repository import GLib
    win = state["win"]
    paused = bool(snap(win, "pause"))
    check("pause_set", paused, str(paused))
    state["t_paused"] = float(snap(win, "time-pos") or -1)
    GLib.timeout_add(800, lambda: seek_phase(app))
    return False


def seek_phase(app):
    from gi.repository import GLib
    win = state["win"]
    t_paused = state["t_paused"]
    t_now = float(snap(win, "time-pos") or -1)
    check("pause_holds", abs(t_now - t_paused) < 0.2,
          f"{t_paused:.2f}->{t_now:.2f}")
    win._actions["play-pause"](None)          # resume
    GLib.timeout_add(300, lambda: do_seek(app))
    return False


def do_seek(app):
    from gi.repository import GLib
    win = state["win"]
    state["t_before"] = float(snap(win, "time-pos") or -1)
    win._actions["seek-small"](+1)            # +5 s (ui.seek_step_s)
    GLib.timeout_add(1200, lambda: after_seek(app))
    return False


def after_seek(app):
    from gi.repository import GLib
    win = state["win"]
    before = state["t_before"]
    now = float(snap(win, "time-pos") or -1)
    check("seek_jumps", now >= before + 4, f"{before:.2f}->{now:.2f}")
    state["t_after"] = now
    # time keeps advancing after the seek
    GLib.timeout_add(700, lambda: finish_phase(app))
    return False


def finish_phase(app):
    win = state["win"]
    t_b = float(snap(win, "time-pos") or -1)
    check("timepos_advancing", t_b > state["t_after"] + 0.3,
          f"{state['t_after']:.2f}->{t_b:.2f}")
    win._actions["fullscreen"](None)
    check("fullscreen_on", bool(win._act_fullscreen_state()), str(win._fs))
    win._actions["fullscreen"](None)
    check("fullscreen_off", not win._act_fullscreen_state(), str(win._fs))
    app.quit()
    return False


def main() -> int:
    rc = reelsee.run(
        ["reelsee", "testdata/perf_1080p30.mp4"], on_app=probe_schedule)
    check("app_rc", rc == 0, str(rc))
    finish()
    return 0


if __name__ == "__main__":
    sys.exit(main())
