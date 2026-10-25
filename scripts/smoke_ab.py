#!/usr/bin/python3
"""P5 gate smoke: engine-side A/B loop driven through the app's own API.

Sets A=1.0 B=2.0, plays 6 s, asserts every observed time-pos AFTER the first
wrap lies inside [0.7, 2.3], that ab.json persisted the selection, and that
clearing restores free playback.  Runs under scripts/headless-smoke.sh.
"""
import json
import os
import sys

from smoke_harness import (SCRATCH, capture_xwd, check, finish, image_mean)

CFG_DIR = os.path.join(SCRATCH, "ab-cfg")
STATE_DIR = os.path.join(SCRATCH, "ab-state")
for _d in (CFG_DIR, STATE_DIR):
    os.makedirs(_d, exist_ok=True)
os.environ["REELSEE_CONFIG_DIR"] = CFG_DIR
os.environ["REELSEE_STATE_DIR"] = STATE_DIR
for _f in (os.path.join(CFG_DIR, "config.json"),
           os.path.join(STATE_DIR, "ab.json")):
    if os.path.exists(_f):
        os.unlink(_f)

from gi.repository import GLib  # noqa: E402

import reelsee  # noqa: E402

from pathlib import Path  # noqa: E402

CLIP = "testdata/perf_1080p30.mp4"      # 10 s
A_S, B_S = 1.0, 2.0
LO, HI = 0.7, 2.3
SAMPLE_MS = 200
PLAY_MS = 6000

state: dict = {"samples": [], "prev": None, "wrapped": False,
               "wraps": 0, "viol": [], "done": False}


def ab_disk():
    with open(os.path.join(STATE_DIR, "ab.json")) as fh:
        return json.load(fh)


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

    def wait_for(desc, pred, then, left=20):
        val = None
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

    def sample():
        w = app.windows[0]
        t = w.core.snapshot.get("time-pos")
        if t is None or state["done"]:
            return True
        t = float(t)
        prev = state["prev"]
        if prev is not None and t <= A_S + 0.35 and prev >= A_S + 0.35:
            # inside the loop window; a drop back near A counts as a wrap
            if t < prev - 0.3:
                state["wraps"] += 1
                state["wrapped"] = True
        if state["wrapped"] and not (LO <= t <= HI):
            state["viol"].append(t)
        state["samples"].append(t)
        state["prev"] = t
        return True

    def s0():
        w = app.windows[0]
        w._ab_hook("set_a", A_S)
        w._ab_hook("set_b", B_S)
        st = w.ab_state
        check("ab_state_set", (st.a, st.b) == (A_S, B_S), f"a={st.a} b={st.b}")
        seek = w.chrome.seek
        check("ab_markers_drawn",
              seek.ab_a == A_S and seek.ab_b == B_S,
              f"marks={seek.ab_a},{seek.ab_b}")
        check("ab_buttons_filled",
              w.chrome.ab_a_btn.has_css_class("reelsee-ab-on")
              and w.chrome.ab_b_btn.has_css_class("reelsee-ab-on"), "ok")
        GLib.timeout_add(SAMPLE_MS, sample)
        GLib.timeout_add(PLAY_MS, guard(s1))
        return False

    def s1():
        wraps = state["wraps"]
        check("ab_loops", wraps >= 1,
              f"wraps={wraps} samples={[round(t, 2) for t in state['samples']]}")
        check("ab_in_window", not state["viol"],
              f"n_after_wrap={len(state['samples'])} viol={state['viol'][:5]}")
        try:
            data = ab_disk()
            entry = data.get(CLIP, {})
            ok = (float(entry.get("a", -1)) == A_S
                  and float(entry.get("b", -1)) == B_S)
            check("ab_persisted", ok, repr(entry))
        except Exception as exc:                   # noqa: BLE001
            check("ab_persisted", False, repr(exc))
        app.windows[0].core.set_property("pause", True)
        GLib.timeout_add(500, guard(s1b))
        return False

    def _seek_crop(w):
        """Root-window crop string covering the seek bar strip."""
        try:
            ok, rect = w.chrome.seek.compute_bounds(w)
            if ok:
                # window content starts at the window's surface origin
                return (f"{int(rect.get_width())}"
                        f"x{max(6, int(rect.get_height()))}"
                        f"+{int(rect.get_x())}+{int(rect.get_y())}")
        except Exception:                          # noqa: BLE001
            pass
        return "960x28+0+508"

    def s1b():
        # pause → chrome visible → diff-capture proves ticks/band actually paint
        w = app.windows[0]
        wait_for("ab_paused", lambda: w.core.snapshot.get("pause") is True,
                 lambda: GLib.timeout_add(450, guard(_capture_on)),
                 left=40)
        return False

    def _capture_on():
        w = app.windows[0]
        state["crop"] = _seek_crop(w)
        p1 = Path(SCRATCH) / "p5_marks_on.xwd"
        ok1 = capture_xwd(p1)
        state["cap1"] = ok1
        w._ab_hook("clear")          # pause keeps chrome visible; markers go
        GLib.timeout_add(300, lambda: (
            _finish_s1b(w, state["crop"], p1,
                        Path(SCRATCH) / "p5_marks_off.xwd", ok1), False)[1])
        return False

    def _marker_crop(w):
        """Crop over just the A–B marker band (+ticks): the pixel-proving
        region.  Mirrors _SeekScale._x() geometry."""
        try:
            sk = w.chrome.seek
            ok, rect = sk.compute_bounds(w)
            W, X = rect.get_width(), rect.get_x()
            dur = sk.ab_dur
            if not ok or dur <= 0:
                raise ValueError("no bounds/dur")
            inset = min(13.0, W / 4.0)
            span = W - 2 * inset
            xa = X + inset + (A_S / dur) * span
            xb = X + inset + (B_S / dur) * span
            return (f"{int(xb - xa) + 8}x{int(rect.get_height())}"
                    f"+{int(xa) - 4}+{int(rect.get_y())}")
        except Exception:                          # noqa: BLE001
            return state["crop"]

    def _finish_s1b(w, crop, p1, p2, ok1):
        ok2 = capture_xwd(p2)
        if ok1 and ok2:
            mcrop = _marker_crop(w)
            on = image_mean(p1, mcrop)
            off = image_mean(p2, mcrop)
            check("ab_markers_paint", on - off > 2.0,
                  f"on={on:.1f} off={off:.1f} crop={mcrop}")
        else:
            check("ab_markers_paint", False, f"capture_failed={ok1},{ok2}")
        w.core.set_property("pause", False)
        GLib.timeout_add(120, guard(s2))
        return False

    def s2():
        w = app.windows[0]
        wait_for("ab_clear_restores",
                 lambda: float(w.core.snapshot.get("time-pos") or 0) > HI,
                 guard(s3), left=60)
        return False

    def s3():
        w = app.windows[0]
        check("ab_markers_cleared", w.chrome.seek.ab_a is None
              and w.chrome.seek.ab_b is None, "ok")
        try:
            check("ab_store_cleared", ab_disk().get(CLIP) is None,
                  repr(ab_disk()))
        except Exception as exc:                   # noqa: BLE001
            check("ab_store_cleared", False, repr(exc))
        state["done"] = True
        app.quit()
        return False

    GLib.timeout_add(900, guard(s0))
    return False


def main() -> int:
    rc = reelsee.run(["reelsee", CLIP], on_app=probe_schedule)
    check("app_rc", rc == 0, str(rc))
    finish()
    return 0


if __name__ == "__main__":
    sys.exit(main())
