#!/usr/bin/python3
"""R23 gate: the fullscreen / maximize unrealize -> realize cycle.

The user's report was "double clicking to maximize a video causes a crash".
Fullscreen and maximize-restore reparent the window, which unrealizes and
re-realizes the GLArea; the app therefore frees and re-creates the mpv
render context on every double-click.  Three defects lived on that path
(references/ROUND23-BRIEF.md §0): the one-shot `teared_down` latch leaked
the second render context so the next `mpv_render_context_create` failed,
the free could run with no GL context current (§0.6), and mpv's libmpv VO
never resumed picture delivery after a clean free/create — black surface,
then SIGABRT.

Everything runs through the app's own action paths (like smoke_render.py).
Checks:
  B1  one cycle (fullscreen on -> off):
        create_errors          no mpv_render_context_create error logged
        rctx_after_restore     the render context is live again
        luma_after_restore     a captured frame is NOT black
        pump_after_restore     the render pump resumes (draws advance)
        unrealize_seen/realize_seen  positive control: the toggle really did
                               drive a lifecycle cycle, so the gate cannot
                               pass vacuously on a toggle that did nothing
  B3  double toggle (on -> off -> on -> off): live rctx, non-black surface
      and advancing time-pos after EVERY cycle — the user double-clicks
      repeatedly.
"""
import logging
import sys
import time

import gi

from smoke_harness import SCRATCH, capture_xwd, check, finish, image_mean

gi.require_version("Gtk", "4.0")
from gi.repository import GLib  # noqa: E402

import reelsee

LUMA_MIN = 8.0          # same margin as smoke_render.py's mean_luma check
#: Floor for the wall-scaled clock test below.  0.15 s could NOT be kept:
#: the R23 sample the brief cites moved 0.07 s, and `max(0.15, …)` is never
#: below 0.15, so the cited sample could never pass whatever the window was.
#: The wall term is the real test — at the ~0.7 s window this gate actually
#: measures it demands 0.175 s, STRICTER than the old constant — so the floor
#: only has to catch a clock that never moved at all.  See selfcheck_formula.
TPOS_MIN = 0.05         # s the clock must advance across one cycle
CLIP_S = 10.0           # testdata/perf_1080p30.mp4 duration (wrap detection)
state = {}


def install_error_capture():
    """Record every ERROR the render/window code logs (R23 symptom line)."""
    state["errors"] = []

    class Grab(logging.Handler):
        def emit(self, record):
            if record.levelno >= logging.ERROR:
                state["errors"].append(record.getMessage())

    for name in ("reelsee.render", "reelsee.window"):
        lg = logging.getLogger(name)
        lg.addHandler(Grab())
        lg.setLevel(min(lg.level, logging.INFO))


def luma():
    xwd = SCRATCH / "r23_cycle.xwd"
    if not capture_xwd(xwd):
        return -1.0
    return image_mean(xwd)


def wire_counters(win):
    """Count the REAL realize/unrealize signals — the positive control that
    a toggle actually drove the lifecycle cycle.  Counting starts now, so
    every predicate compares against a baseline taken just before a toggle."""
    state["realize"] = 0
    state["unrealize"] = 0
    area = win.view.area
    area.connect("realize", lambda *_a: state.__setitem__(
        "realize", state["realize"] + 1))
    area.connect("unrealize", lambda *_a: state.__setitem__(
        "unrealize", state["unrealize"] + 1))


def advanced(t_prev, t_now, wall_delta):
    """True if the clock moved forward ACROSS the wall time it was sampled
    over — or fell back past the clip length, i.e. it wrapped.

    R23 §B.5: a fixed floor called the R23 battery's `1.53->1.60` a frozen
    clock even though playback was fine (`create_errors=` empty,
    `luma_after_restore=152.1`, `app_rc=0`).  The honest question is "did the
    clock advance at a plausible fraction of the wall time we watched it", so
    the floor follows the sample window: ``max(TPOS_MIN, 0.25 * wall_delta)``.
    Measured here, a normal cycle spans ~0.7 s of wall and mpv advances
    ~0.5-0.9 s, so the window term (0.175 s) does the work and is stricter
    than the old constant; TPOS_MIN only has to catch a clock that never
    moved.  The wrap escape is unchanged."""
    floor = max(TPOS_MIN, 0.25 * max(0.0, wall_delta))
    return (t_now - t_prev) >= floor or (t_prev - t_now) > CLIP_S - 1.0


def sample_pos(win, key):
    """time-pos AND the wall clock at the same instant.  The clock is only
    credible relative to the wall time between two such samples (B.5)."""
    val = float(win.core.snapshot.get("time-pos") or 0.0)
    state[key] = (val, time.monotonic())
    return val


def fmt(key_a, key_b):
    (ta, wa), (tb, wb) = state[key_a], state[key_b]
    return f"{ta:.2f}->{tb:.2f} d={tb - ta:.2f} wall={wb - wa:.2f}"


def selfcheck_formula():
    """B.5's deterministic half.  The live checks below are timing-dependent
    by nature, so the predicate itself gets pinned here — otherwise the flake
    could silently come back on a box that happens to be fast today.

    Every case is RED on the old fixed-floor predicate: it rejected the R23
    sample (0.07 s over a short window) and it accepted 0.15 s of movement
    over a 0.7 s window that the new rule rejects."""
    check("formula_accepts_r23_sample", advanced(1.53, 1.60, 0.25),
          "1.53->1.60 over a 0.25s window (the R23 battery sample)")
    check("formula_rejects_frozen_clock", not advanced(1.53, 1.53, 0.70),
          "1.53->1.53 over 0.70s: a clock that never moved is still caught")
    check("formula_rejects_stalled_clock", not advanced(1.53, 1.55, 1.50),
          "1.53->1.55 over 1.50s (needs 0.375)")
    check("formula_stricter_on_a_normal_window",
          not advanced(1.53, 1.68, 0.70),
          "1.53->1.68 over 0.70s: the OLD floor let this through, the new "
          "rule demands 0.175 s — hardening, not weakening")
    check("formula_accepts_a_real_advance", advanced(1.53, 1.71, 0.70),
          "1.53->1.71 over 0.70s (floor 0.175)")
    check("formula_scales_with_a_slow_box", advanced(1.53, 1.95, 1.50),
          "1.53->1.95 over 1.50s (floor 0.375)")
    check("formula_keeps_wrap_escape", advanced(9.80, 0.20, 0.70),
          "9.80->0.20 = wrap past the 10 s clip")


def wait_until(tag, pred, then, timeout_ms=8000, step_ms=50):
    """Poll `pred` with a deadline (no fixed sleeps — gate-race rule)."""
    t0 = time.monotonic()

    def tick():
        try:
            ok = bool(pred())
        except Exception as exc:                  # noqa: BLE001
            check(f"{tag}_probe_error", False, repr(exc))
            then()
            return False
        if ok:
            then()
            return False
        if (time.monotonic() - t0) * 1000.0 > timeout_ms:
            check(f"{tag}_deadline", False, f"no settle within {timeout_ms}ms")
            then()
            return False
        GLib.timeout_add(step_ms, tick)
        return False

    GLib.timeout_add(step_ms, tick)


def toggle_and_wait(app, n_next, label, prev_key=None):
    """Drive one fullscreen toggle and wait for the unrealize -> realize
    pair it causes (baseline-relative: the initial realize happened before
    the counters were wired).  `prev_key` additionally samples the clock,
    wall-stamped, as the start of a cycle's measurement window."""
    win = app.windows[0]
    if prev_key:
        sample_pos(win, prev_key)
    state["r_base"] = state["realize"]
    state["u_base"] = state["unrealize"]
    win._actions["fullscreen"](None)
    wait_until(
        label,
        lambda: (state["realize"] >= state["r_base"] + 1
                 and state["unrealize"] >= state["u_base"] + 1),
        lambda: step(app, n_next))


def probe_schedule(app):
    GLib.timeout_add(1500, lambda: step(app, 0))
    return False


def step(app, n):
    wins = app.windows
    check("window_open", len(wins) == 1, str(len(wins)))
    if not wins:
        app.quit()
        return False
    win = wins[0]

    if n == 0:
        install_error_capture()
        wire_counters(win)
        base = luma()
        check("baseline_luma", base > LUMA_MIN, f"{base:.1f}")
        check("rctx_before", win.view.rctx is not None,
              str(win.view.rctx is not None))
        toggle_and_wait(app, 1, "cycle1_on", prev_key="t_c1_prev")
    elif n == 1:
        # Positive control: the toggle really drove a lifecycle cycle.
        check("unrealize_seen", state["unrealize"] >= state["u_base"] + 1,
              f"{state['u_base']}->{state['unrealize']}")
        check("realize_seen", state["realize"] >= state["r_base"] + 1,
              f"{state['r_base']}->{state['realize']}")
        check("fullscreen_on", bool(win._act_fullscreen_state()), str(win._fs))
        check("create_errors_after_on", len(state["errors"]) == 0,
              ";".join(state["errors"])[:160])
        toggle_and_wait(app, 2, "cycle1_off")
    elif n == 2:
        # ---- B1: the restore cycle ------------------------------------
        check("fullscreen_off", not win._act_fullscreen_state(), str(win._fs))
        check("create_errors", len(state["errors"]) == 0,
              ";".join(state["errors"])[:160])
        check("rctx_after_restore", win.view.rctx is not None,
              str(win.view.rctx is not None))
        lum = luma()
        check("luma_after_restore", lum > LUMA_MIN, f"{lum:.1f}")
        sample_pos(win, "t_c1_now")
        check("timepos_advances_cycle1",
              advanced(state["t_c1_prev"][0], state["t_c1_now"][0],
                       state["t_c1_now"][1] - state["t_c1_prev"][1]),
              fmt("t_c1_prev", "t_c1_now"))
        state["renders1"] = win.view.render_count
        # the pump must resume: draws advance after the restore
        wait_until("pump_after_restore",
                   lambda: win.view.render_count >= state["renders1"] + 3,
                   lambda: step(app, 3))
    elif n == 3:
        check("pump_after_restore",
              win.view.render_count >= state["renders1"] + 3,
              f"{state['renders1']}->{win.view.render_count}")
        # ---- B3: second full cycle (on -> off) -------------------------
        sample_pos(win, "t_c2_prev")
        toggle_and_wait(app, 4, "cycle2_on")
    elif n == 4:
        check("fullscreen_on_2", bool(win._act_fullscreen_state()), str(win._fs))
        check("create_errors_after_cycle2_on", len(state["errors"]) == 0,
              ";".join(state["errors"])[:160])
        toggle_and_wait(app, 5, "cycle2_off")
    elif n == 5:
        check("fullscreen_off_2", not win._act_fullscreen_state(), str(win._fs))
        sample_pos(win, "t_c2_now")
        check("timepos_advances_cycle2",
              advanced(state["t_c2_prev"][0], state["t_c2_now"][0],
                       state["t_c2_now"][1] - state["t_c2_prev"][1]),
              fmt("t_c2_prev", "t_c2_now"))
        state["renders2"] = win.view.render_count
        wait_until("pump_after_cycle2",
                   lambda: win.view.render_count >= state["renders2"] + 3,
                   lambda: step(app, 6))
    elif n == 6:
        lum = luma()
        check("create_errors_after_double_toggle", len(state["errors"]) == 0,
              ";".join(state["errors"])[:160])
        check("rctx_after_double_toggle", win.view.rctx is not None,
              str(win.view.rctx is not None))
        check("luma_after_double_toggle", lum > LUMA_MIN, f"{lum:.1f}")
        check("pump_after_double_toggle",
              win.view.render_count >= state["renders2"] + 3,
              f"{state['renders2']}->{win.view.render_count}")
        app.quit()
    return False


def main() -> int:
    selfcheck_formula()
    rc = reelsee.run(["reelsee", "testdata/perf_1080p30.mp4"],
                     on_app=probe_schedule)
    check("app_rc", rc == 0, str(rc))
    finish()
    return 0


if __name__ == "__main__":
    sys.exit(main())
