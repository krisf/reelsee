#!/usr/bin/python3
"""P2 gate smoke: real app plays perf_1080p30.mp4 under Xvfb.

Checks (PLAN §8 P2): pixels land in the window (mean>8, ≥200 colors on the
640x360+0+0 crop), time-pos advances, hwdec-current=no (R13), estimated-vf-fps
>= 20, pause holds, seek jumps, F toggles fullscreen — all through the app's
own action paths.
"""
import logging
import sys
import time

import gi

from smoke_harness import (SCRATCH, capture_xwd, check, finish, image_colors,
                           image_mean)

gi.require_version("Gtk", "4.0")
from gi.repository import GLib  # noqa: E402

import reelsee

state = {}


def probe_schedule(app):
    GLib.timeout_add(1500, lambda: step(app))
    return False


def snap(win, key, default=None):
    return win.core.snapshot.get(key, default)


# ---- R23 helpers: watch the real unrealize/realize cycle -----------------
def install_error_capture():
    """Record every ERROR the render/window code logs.  The R23 symptom line
    is `mpv_render_context_create: something happened`; the old gate read
    only the app's own fullscreen bookkeeping and never looked at it."""
    state["errors"] = []

    class Grab(logging.Handler):
        def emit(self, record):
            if record.levelno >= logging.ERROR:
                state["errors"].append(record.getMessage())

    for name in ("reelsee.render", "reelsee.window"):
        lg = logging.getLogger(name)
        lg.addHandler(Grab())
        lg.setLevel(min(lg.level, logging.INFO))


def wire_cycle_counters(win):
    """Count the GLArea's REAL realize/unrealize signals, so a toggle can be
    waited out instead of guessed at with a fixed sleep."""
    state["realize"] = 0
    state["unrealize"] = 0
    state["r_base"] = 0
    state["u_base"] = 0
    area = win.view.area
    area.connect("realize", lambda *_a: state.__setitem__(
        "realize", state["realize"] + 1))
    area.connect("unrealize", lambda *_a: state.__setitem__(
        "unrealize", state["unrealize"] + 1))


def begin_cycle():
    """Baseline the counters immediately BEFORE a toggle: the unrealize can
    land synchronously inside win.fullscreen(), so baselining afterwards
    would miss it (measured: unrealize stayed 0 against a post-toggle
    baseline and the settle wait timed out)."""
    state["r_base"] = state["realize"]
    state["u_base"] = state["unrealize"]


def cycle_settled():
    """One unrealize + one realize since the last toggle."""
    return (state["realize"] >= state["r_base"] + 1
            and state["unrealize"] >= state["u_base"] + 1)


def wait_until(app, tag, pred, then, timeout_ms=8000, step_ms=50):
    """Poll `pred` with a deadline (generous polling, no fixed sleeps)."""
    t0 = time.monotonic()

    def tick():
        try:
            ok = bool(pred())
        except Exception as exc:                  # noqa: BLE001
            check(f"{tag}_probe_error", False, repr(exc))
            then(app)
            return False
        if ok:
            then(app)
            return False
        if (time.monotonic() - t0) * 1000.0 > timeout_ms:
            check(f"{tag}_deadline", False, f"no settle within {timeout_ms}ms")
            then(app)
            return False
        GLib.timeout_add(step_ms, tick)
        return False

    GLib.timeout_add(step_ms, tick)


def step(app):
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
    win = state["win"]
    paused = bool(snap(win, "pause"))
    check("pause_set", paused, str(paused))
    state["t_paused"] = float(snap(win, "time-pos") or -1)
    GLib.timeout_add(800, lambda: seek_phase(app))
    return False


def seek_phase(app):
    win = state["win"]
    t_paused = state["t_paused"]
    t_now = float(snap(win, "time-pos") or -1)
    check("pause_holds", abs(t_now - t_paused) < 0.2,
          f"{t_paused:.2f}->{t_now:.2f}")
    win._actions["play-pause"](None)          # resume
    GLib.timeout_add(300, lambda: do_seek(app))
    return False


def do_seek(app):
    """Absolute seek to 40% of the clip.

    The relative +5 s step looked fine on an idle box and flaked under load:
    the clip is 10.000 s, so any position past ~4.3 s put the seek within a
    second of EOF, and the follow-up "is time still advancing?" sample landed
    after the file wrapped (9.53 -> 0.27) and failed a healthy player. A
    mid-file target keeps the same assertion with real headroom on either side.
    """
    win = state["win"]
    state["t_before"] = float(snap(win, "time-pos") or -1)
    state["dur"] = float(snap(win, "duration") or 0.0)
    state["target"] = max(1.0, state["dur"] * 0.4)
    win.core.command("seek", f"{state['target']:.3f}", "absolute")
    GLib.timeout_add(1200, lambda: after_seek(app))
    return False


def after_seek(app):
    win = state["win"]
    before = state["t_before"]
    now = float(snap(win, "time-pos") or -1)
    check("seek_jumps", abs(now - state["target"]) <= 1.5,
          f"{before:.2f}->{now:.2f} want {state['target']:.2f}")
    state["t_after"] = now
    # time keeps advancing after the seek
    GLib.timeout_add(700, lambda: finish_phase(app))
    return False


def finish_phase(app):
    win = state["win"]
    t_b = float(snap(win, "time-pos") or -1)
    # A wrap shows up here as a DECREASE (9.53->0.27); that value pair is the
    # diagnosis, so there is no separate wrap check — an earlier one that
    # tested "inside the file" passed on wrapped positions and was removed
    # rather than kept as a check that did not do what its name claimed.
    check("timepos_advancing", t_b > state["t_after"] + 0.3,
          f"{state['t_after']:.2f}->{t_b:.2f}")
    # ---- R23: drive the fullscreen cycle to completion ------------------
    # The old gate toggled fullscreen on and off back to back and only read
    # win._fs, the app's own bookkeeping — which is False (so
    # `fullscreen_off` PASSED) even when the re-created render context had
    # failed with "mpv_render_context_create: something happened" and the
    # surface had gone black.  That tolerance is what let the double-click
    # crash ship.  The cycle is now waited out and the RENDER STATE itself is
    # asserted: no create error, a live rctx, a non-black capture, a pump
    # that is drawing again.  Every pre-existing check is kept.
    install_error_capture()
    wire_cycle_counters(win)
    begin_cycle()
    win._actions["fullscreen"](None)
    check("fullscreen_on", bool(win._act_fullscreen_state()), str(win._fs))
    wait_until(app, "fullscreen_on_settle", cycle_settled,
               fullscreen_off_phase)
    return False


def fullscreen_off_phase(app):
    win = state["win"]
    check("fullscreen_on_cycle", cycle_settled(),
          f"r{state['r_base']}->{state['realize']} "
          f"u{state['u_base']}->{state['unrealize']}")
    begin_cycle()
    win._actions["fullscreen"](None)
    check("fullscreen_off", not win._act_fullscreen_state(), str(win._fs))
    wait_until(app, "fullscreen_off_settle", cycle_settled, restore_phase)
    return False


def restore_phase(app):
    """R23 assertions on the post-restore render state (B2)."""
    win = state["win"]
    check("fullscreen_off_cycle", cycle_settled(),
          f"r{state['r_base']}->{state['realize']} "
          f"u{state['u_base']}->{state['unrealize']}")
    check("create_errors_after_restore", state["errors"] == [],
          ";".join(state["errors"])[:160])
    check("rctx_after_restore", win.view.rctx is not None,
          str(win.view.rctx is not None))
    state["renders_base"] = win.view.render_count
    wait_until(app, "pump_after_restore",
               lambda: win.view.render_count >= state["renders_base"] + 3,
               capture_after_restore)
    return False


def capture_after_restore(app):
    win = state["win"]
    check("pump_after_restore",
          win.view.render_count >= state["renders_base"] + 3,
          f"{state['renders_base']}->{win.view.render_count}")
    xwd = SCRATCH / "p2_capture_after_restore.xwd"
    cap_ok = capture_xwd(xwd)
    mean = image_mean(xwd) if cap_ok else -1
    check("capture_after_restore", cap_ok, str(cap_ok))
    check("mean_luma_after_restore", mean > 8, f"{mean:.1f}")
    # B2, to the letter: the line `SMOKE_CHECK:fullscreen_off=False` used to
    # be the LAST WORD on the restore toggle and it was GREEN while the
    # render context was dead — the printed value is str(win._fs), so "False"
    # only means "the app is not fullscreen", which is exactly what the app
    # still believes after the context has been lost.  Re-assert the same
    # name once the cycle has settled, with the render state folded in, so a
    # dead restore can never print a green fullscreen_off.
    check("fullscreen_off",
          (not win._act_fullscreen_state()) and state["errors"] == []
          and win.view.rctx is not None and mean > 8,
          f"_fs={win._fs} rctx={win.view.rctx is not None} "
          f"errors={';'.join(state['errors'])[:60]} luma={mean:.1f}")
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
