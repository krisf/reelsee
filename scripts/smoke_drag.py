#!/usr/bin/python3
"""R5-D gate smoke: the window is draggable from anywhere on the video
(Gtk.WindowHandle wraps the GLArea), yet single-click pause and
double-click fullscreen survive, drags never toggle pause, and during
fullscreen a drag is a structural no-op (handle out of the tree).

Headless but REAL input: events are injected with X11 XTEST (python-xlib)
onto the live window under Xvfb — genuine press/move/release sequences
through GDK and the GTK gesture machinery, not handler pokes.
Runs under scripts/headless-smoke.sh."""
import os
import sys
import tempfile

from smoke_harness import SCRATCH, check, finish

CFG_DIR = SCRATCH / "drag-cfg"
STATE_DIR = SCRATCH / "drag-state"
for _d in (CFG_DIR, STATE_DIR):
    _d.mkdir(parents=True, exist_ok=True)
os.environ["REELSEE_CONFIG_DIR"] = str(CFG_DIR)
os.environ["REELSEE_STATE_DIR"] = str(STATE_DIR)
for _f in (CFG_DIR / "config.json", STATE_DIR / "resume.json"):
    if _f.exists():
        _f.unlink()

import reelsee  # noqa: E402

state: dict = {}


def main() -> int:
    def schedule(app):
        from gi.repository import GLib
        state["app"] = app

        def win():
            return app.windows[0]

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

        def again(fn, ms):
            GLib.timeout_add(ms, fn)
            return False

        def wait_for(desc, pred, then, left=30):
            val = None
            try:
                val = pred()
            except Exception:                          # noqa: BLE001
                val = None
            if val:
                check(desc, True, "ok" if val is True else str(val))
                return again(then, 80)
            if left <= 0:
                check(desc, False, "timeout")
                app.quit()
                return False
            return again(lambda: wait_for(desc, pred, then, left - 1), 120)

        # ------------------------------------------------------ X plumbing
        def xopen():
            from Xlib import display as xdisplay
            d = xdisplay.Display(os.environ.get("DISPLAY", ":0"))
            state["xd"] = d
            return d

        def xfind():
            from Xlib import X
            d = state["xd"]
            root = d.screen().root
            stack, hit = [root], None

            def name_of(w):
                try:
                    n = w.get_wm_name()
                    if isinstance(n, bytes):
                        n = n.decode("utf-8", "replace")
                    return n or None
                except Exception:                      # noqa: BLE001
                    return None

            def mapped(w):
                try:
                    return w.get_attributes().map_state == X.IsViewable
                except Exception:                      # noqa: BLE001
                    return False

            def size(w):
                try:
                    g = w.get_geometry()
                    return g.width, g.height
                except Exception:                      # noqa: BLE001
                    return 0, 0

            # NOTE: no _NET_WM_NAME get_property reads here — python-xlib
            # 0.33 property parsing can stall the connection; the WM_NAME
            # match plus a geometry fallback is deterministic here (the
            # only large mapped window is ours).
            big = [None]

            def walk(w, depth=0):
                nonlocal hit
                if hit is not None or depth > 6:
                    return
                name, ok = name_of(w), mapped(w)
                if name and "REELSEE" in name and ok and w != root:
                    hit = w
                    return
                if ok and w != root and depth <= 2:
                    wd, ht = size(w)
                    if wd >= 800 and ht >= 400 and (big[0] is None
                                                    or wd * ht >
                                                    size(big[0])[0] *
                                                    size(big[0])[1]):
                        big[0] = w
                try:
                    kids = w.query_tree().children
                except Exception:                      # noqa: BLE001
                    kids = []
                for k in kids:
                    walk(k, depth + 1)

            walk(root)
            if hit is None and big[0] is not None:
                state["found_by"] = "geometry"
                return big[0]
            return hit

        def xcenter(winx):
            d = state["xd"]
            root = d.screen().root
            tr = winx.translate_coords(root, 0, 0)
            g = winx.get_geometry()
            return tr.x + g.width // 2, tr.y + g.height // 2

        # python-xlib 0.33 XTEST = fake_input (button/motion at the
        # CURRENT pointer position); position comes from real X warps,
        # which deliver MotionNotify to the window under the pointer.
        def fake_button(kind, x, y, state_mask=0):
            from Xlib import X
            from Xlib.ext import xtest
            d = state["xd"]
            root = d.screen().root
            root.warp_pointer(x, y)
            d.sync()
            et = X.ButtonPress if kind == "press" else X.ButtonRelease
            xtest.fake_input(d, et, 1)
            d.sync()

        def fake_motion(x, y, state_mask):
            d = state["xd"]
            root = d.screen().root
            root.warp_pointer(x, y)
            d.sync()

        def clicks_then(n, fn, x0, y0, gap=120):
            """n real click pairs; press and release 90 ms apart, next
            press `gap` after the release — inside GDK's 400 ms
            double-click window but never back-to-back in one tick."""
            done = {"k": 0}

            def press():
                fake_button("press", x0, y0)
                return again(release, 90)

            def release():
                fake_button("release", x0, y0)
                done["k"] += 1
                if done["k"] < n:
                    return again(press, max(30, gap - 90))
                return again(fn, 600)
            return again(guard(press), 30)

        def drag_then(fn, x0, y0, dx=60, dy=60):
            """press, incremental moves (past the dnd threshold), release."""
            fake_button("press", x0, y0)

            def step(i):
                if i <= 6:
                    fake_motion(x0 + dx * i // 6, y0 + dy * i // 6,
                                1 << 8)          # Button1Mask
                    return again(guard(lambda: step(i + 1)), 35)
                fake_button("release", x0 + dx, y0 + dy, 1 << 8)
                return again(guard(fn), 450)
            return again(guard(lambda: step(1)), 35)

        # ---------------------------------------------------------- phases
        def s_structural():
            w = win()
            check("handle_wraps_video",
                  w.overlay.get_child() is w._handle
                  and w._handle.get_child() is w.view.area,
                  f"{type(w.overlay.get_child()).__name__}/"
                  f"{type(w._handle.get_child()).__name__}")
            ctrl_on_area = any(
                type(c).__name__ == "GestureClick"
                for c in w.view.area.get_controllers()) \
                if hasattr(w.view.area, "get_controllers") else True
            check("click_gesture_on_area", ctrl_on_area, "ok")
            # bars stay OUTSIDE the handle (seek/volume/markers unstealable)
            check("bars_not_inside_handle",
                  w.chrome.bottom.get_parent() is w.overlay
                  and w.chrome.bottom.get_parent() is not w._handle,
                  str(w.chrome.bottom.get_parent() is w.overlay))
            return again(guard(s_click), 60)

        def paused():
            v = win().core.snapshot.get("pause")
            return None if v is None else bool(v)

        def s_click():
            fake_button("press", *state["xy"])
            return again(lambda: (fake_button("release", *state["xy"]),
                                  again(guard(s_click2), 500))[1], 40)

        def s_click2():
            check("single_click_pauses", paused() is True, str(paused()))
            fake_button("press", *state["xy"])
            return again(lambda: (fake_button("release", *state["xy"]),
                                  again(guard(s_click3), 500))[1], 40)

        def s_click3():
            check("single_click_resumes", paused() is False, str(paused()))
            x0, y0 = state["xy"]
            return again(guard(lambda: drag_then(s_drag_result, x0, y0)),
                         40)

        def s_drag_result():
            # A press+move+release is a DRAG: consumed by the WindowHandle
            # (move path), it must NOT toggle pause.
            check("drag_does_not_pause", paused() is False, str(paused()))
            x0, y0 = state["xy"]

            return clicks_then(2, guard(s_fs), x0, y0)

        def s_fs():
            check("double_click_fullscreen", bool(win()._fs),
                  str(win()._fs))
            if not win()._fs:               # guarantee the structural
                win()._act_fullscreen()     # fullscreen checks below
            return again(s_fs2, 250)

        def s_fs2():
            w = win()
            check("handle_detached_in_fullscreen",
                  w._handle.get_parent() is None
                  and w.overlay.get_child() is w.view.area,
                  f"parent={w._handle.get_parent()}")
            x0, y0 = xcenter(state["winx"])   # fresh fullscreen geometry
            return again(guard(lambda: drag_then(s_fs_drag_result,
                                                 x0, y0)), 60)

        def s_fs_drag_result():
            w = win()
            check("fullscreen_drag_noop", bool(w._fs)
                  and w._handle.get_parent() is None
                  and paused() is False,
                  f"fs={w._fs} parent={w._handle.get_parent()} "
                  f"pause={paused()}")
            x0, y0 = xcenter(state["winx"])

            return clicks_then(2, guard(s_unfs), x0, y0)

        def s_unfs():
            w = win()
            check("double_click_unfullscreen", not w._fs, str(w._fs))
            if w._fs:                        # fall back to the action so
                w._act_fullscreen()          # the rest of the phase runs
            check("back_windowed", not w._fs, str(w._fs))
            check("handle_reattached",
                  w.overlay.get_child() is w._handle
                  and w._handle.get_child() is w.view.area, "ok")
            t0 = w.core.snapshot.get("time-pos")
            return again(lambda: wait_for(
                "video_still_renders_after_reparent",
                lambda: (w.core.snapshot.get("time-pos") or 0) > (t0 or 0),
                guard(fin), left=25), 100)

        def fin():
            check("window_alive", win().get_realized(), "True")
            app.quit()
            return False

        def start():
            w = win()
            if not w.get_realized():
                return again(start, 100)

            def playing_then(left=25):
                try:
                    if "xd" not in state:
                        xopen()
                    winx = xfind()
                    if winx is None:
                        if left <= 0:
                            check("x_window_found", False,
                                  "no REELSEE window")
                            app.quit()
                            return False
                        return again(lambda: playing_then(left - 1), 300)
                    check("x_window_found", True, "REELSEE window mapped")
                    state["winx"] = winx
                    state["xy"] = xcenter(winx)
                    w.present()
                    return again(after_present, 300)
                except Exception as exc:               # noqa: BLE001
                    import traceback
                    traceback.print_exc()
                    check("x_setup", False, repr(exc))
                    app.quit()
                    return False

            def after_present():
                return again(guard(s_structural), 60)

            wait_for("playing",
                     lambda: w.core.snapshot.get("pause") is False,
                     guard(playing_then), left=40)
            return False

        GLib.timeout_add(700, start)
        return False

    rc = reelsee.run(["reelsee", "testdata/perf_1080p30.mp4"],
                     on_app=schedule)
    check("app_rc", rc == 0, str(rc))
    finish()
    return 0


if __name__ == "__main__":
    sys.exit(main())
