#!/usr/bin/python3
"""R6 gate smoke: the video right-click context menu (brief section A).

REAL X11 input under Xvfb (XTEST warps + button/key events) into the live
window: left click pauses/resumes, RIGHT click pops the menu AT the
pointer with the exact brief entry set, insensitive-but-present entries
for data the file lacks, Escape and click-away dismiss WITHOUT toggling
playback, a right-button drag neither opens the menu nor moves the
window, and the Repeat radio lands on the real mpv loop properties.
Phase 2 (fresh process, ui.show_top_menu off, top bar absent from the
widget tree) proves the identical menu still works.
"""
import json
import os
import subprocess
import sys

from smoke_harness import (CROP, SCRATCH, capture_xwd, check, finish,
                           image_mean)

CFG_DIR = SCRATCH / "r6menu-cfg"
STATE_DIR = SCRATCH / "r6menu-state"
for _d in (CFG_DIR, STATE_DIR):
    _d.mkdir(parents=True, exist_ok=True)
os.environ["REELSEE_CONFIG_DIR"] = str(CFG_DIR)
os.environ["REELSEE_STATE_DIR"] = str(STATE_DIR)

PHASE = os.environ.get("REELSEE_R6MENU_PHASE", "1")
if PHASE == "1":
    for _f in (CFG_DIR / "config.json", STATE_DIR / "resume.json"):
        if _f.exists():
            _f.unlink()

import reelsee  # noqa: E402
import ctxmenu  # noqa: E402

FILES = ["testdata/perf_1080p30.mp4"]
state: dict = {}


def in_tree(root, target):
    if root is target:
        return True
    ch = root.get_first_child()
    while ch:
        if in_tree(ch, target):
            return True
        ch = ch.get_next_sibling()
    return False


def base_schedule(app, phase):
    from gi.repository import GLib
    state["app"] = app

    def win():
        return app.windows[0]

    def guard(fn):
        def run(*a):
            try:
                return fn(*a)
            except Exception as exc:                # noqa: BLE001
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

    def wait_for(desc, pred, then, left=40):
        try:
            val = pred()
        except Exception:                           # noqa: BLE001
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
        state["xd"] = xdisplay.Display(os.environ.get("DISPLAY", ":0"))

    def xfind():
        from Xlib import X
        d = state["xd"]
        root = d.screen().root
        found = [None]
        big = [None, 0]

        def name_of(w):
            try:
                n = w.get_wm_name()
                return n.decode("utf-8", "replace") if isinstance(
                    n, bytes) else (n or None)
            except Exception:                       # noqa: BLE001
                return None

        def mapped(w):
            try:
                return w.get_attributes().map_state == X.IsViewable
            except Exception:                       # noqa: BLE001
                return False

        def walk(w, depth=0):
            if found[0] is not None or depth > 6:
                return
            nm = name_of(w)
            if nm and "REELSEE" in nm and mapped(w) and w != root:
                found[0] = w
                return
            if mapped(w) and w != root and depth <= 2:
                try:
                    g = w.get_geometry()
                    area = g.width * g.height
                except Exception:                   # noqa: BLE001
                    area = 0
                if g.width >= 800 and g.height >= 400 and (
                        big[0] is None or area > big[1]):
                    big[0], big[1] = w, area
            try:
                kids = w.query_tree().children
            except Exception:                       # noqa: BLE001
                kids = []
            for k in kids:
                walk(k, depth + 1)
        walk(root)
        return found[0] or big[0]

    def wpos():
        g = state["winx"].get_geometry()
        tr = state["winx"].translate_coords(state["xd"].screen().root,
                                            0, 0)
        return tr.x, tr.y, g.width, g.height

    def wcenter():
        x, y, wd, ht = wpos()
        return x + wd // 2, y + ht // 2

    def fake_button(kind, x, y, button=1):
        from Xlib import X
        from Xlib.ext import xtest
        d = state["xd"]
        d.screen().root.warp_pointer(x, y)
        d.sync()
        et = X.ButtonPress if kind == "press" else X.ButtonRelease
        xtest.fake_input(d, et, button)
        d.sync()

    def click(x, y, button=1, then=None, gap=60):
        def release():
            fake_button("release", x, y, button)
            return again(then, 500)
        fake_button("press", x, y, button)
        return again(release, gap)

    def xkey(keysym):
        from Xlib import X, XK
        from Xlib.ext import xtest
        d = state["xd"]
        kc = d.keysym_to_keycode(getattr(XK, keysym))
        xtest.fake_input(d, X.KeyPress, kc)
        d.sync()
        xtest.fake_input(d, X.KeyRelease, kc)
        d.sync()

    def paused():
        v = win().core.snapshot.get("pause")
        return None if v is None else bool(v)

    # ------------------------------------------------------ assertions
    def body_rows():
        menu = win()._ctx_pop.get_menu_model()
        return ctxmenu.model_dump(menu)

    def s_structural():
        w = win()
        if not w.get_realized() or state.get("winx") is None:
            return again(s_structural, 120)
        btns = sorted(c.get_button() for c in w._click_gestures)
        check("area_gestures_buttons", btns == [1, 3], str(btns))
        check("ctx_pop_parented_to_video",
              w._ctx_pop.get_parent() is w.view.area, "ok")
        x0, y0 = wcenter()
        state["xy"] = (x0, y0)
        # record every menu popup's (x, y) to prove "at the pointer"
        state["open_calls"] = []
        orig = w._open_context_menu

        def wrap(x, y):
            state["open_calls"].append((x, y))
            return orig(x, y)
        w._open_context_menu = wrap
        state["mean_before"] = image_mean(shot("before"))
        return again(guard(s_left_pause), 100)

    def shot(name):
        from pathlib import Path
        p = SCRATCH / f"r6menu-p{phase}-{name}.xwd"
        capture_xwd(p)
        return p

    def s_left_pause():
        x0, y0 = state["xy"]
        return click(x0, y0, 1, guard(lambda: wait_for(
            "left_click_pauses", lambda: paused() is True,
            guard(s_left_resume))))

    def s_left_resume():
        x0, y0 = state["xy"]
        return click(x0, y0, 1, guard(lambda: wait_for(
            "left_click_resumes", lambda: paused() is False,
            guard(s_pause_then_right))))

    def s_pause_then_right():
        # pause FIRST: the right-click menu must NOT toggle playback
        x0, y0 = state["xy"]
        return click(x0, y0, 1, guard(lambda: wait_for(
            "pre_menu_paused", lambda: paused() is True,
            guard(s_right_open))))

    def s_right_open():
        x0, y0 = state["xy"]
        px, py = x0 + 60, y0 + 30          # menu opens AT the pointer
        state["ptr"] = (px, py)

        def after():
            w = win()
            check("right_click_opens_menu",
                  w._ctx_pop.get_visible(), "visible")
            check("right_click_no_pause_toggle", paused() is True,
                  str(paused()))
            check("open_called_once",
                  len(state["open_calls"]) == 1, str(state["open_calls"]))
            return again(guard(s_menu_model), 350)
        return click(px, py, 3, guard(after))

    def s_menu_model():
        rows = body_rows()
        labels = [r[0] for r in rows]
        check("header_is_filename_disabled",
              labels[:1] == ["perf_1080p30.mp4"]
              and rows[0][1] == "ctx.noop", str(rows[:1]))
        check("menu_body_order",
              labels[1:] == ["Audio", "Subtitles", "Repeat", "Playlist",
                             "Chapters", "Tracks"], str(labels))
        by = {r[0]: r for r in rows}
        check("audio_submenu_present",
              by["Audio"][3] is not None and
              any(a == "ctx.audio-off" for _l, a, _t, _s in by["Audio"][3]),
              "live submenu (file HAS audio)")
        check("subtitles_disabled_no_data",
              by["Subtitles"][1] == "ctx.noop"
              and by["Subtitles"][3] is None,
              "insensitive-present (file has no subs)")
        check("chapters_disabled_no_submenu",
              by["Chapters"][1] == "ctx.noop"
              and by["Chapters"][3] is None, str(by["Chapters"]))
        check("repeat_radio_targets",
              [(a, t) for _l, a, t, _s in (by["Repeat"][3] or [])]
              == [("ctx.repeat", "off"), ("ctx.repeat", "all"),
                  ("ctx.repeat", "one")],
              str(by["Repeat"][3]))
        rep = shot("menu-open")
        mean_open = image_mean(rep)
        check("menu_visible_pixels",
              abs(mean_open - state["mean_before"]) > 1.0,
              f"{state['mean_before']:.1f}->{mean_open:.1f}")
        subprocess.run(["convert", str(rep), "-crop", CROP, "+repage",
                        str(rep.with_suffix(".png"))])
        return again(guard(s_esc), 300)

    def s_esc():
        xkey("XK_Escape")
        return again(guard(s_esc2), 500)

    def s_esc2():
        check("escape_dismisses", not win()._ctx_pop.get_visible(),
              "hidden")
        check("escape_did_not_quit_or_pause",
              bool(app.windows) and paused() is True,
              f"win={bool(app.windows)} pause={paused()}")
        return again(guard(s_follow), 200)

    def s_follow():
        # reopen at ptr + (40, -20): the popup point must MOVE exactly
        # that much — proof the menu is placed AT the pointer, not a
        # fixed spot.  Doubles as the open-for-clickaway step.
        px, py = state["ptr"]
        return click(px + 40, py - 20, 3, guard(s_follow_check))

    def s_follow_check():
        w = win()
        check("reopen_for_clickaway", w._ctx_pop.get_visible(), "visible")
        c = state["open_calls"]
        d = (round(c[-1][0] - c[0][0]), round(c[-1][1] - c[0][1])) \
            if len(c) >= 2 else None
        check("menu_position_follows_pointer", d == (40, -20),
              f"{d} calls={c}")
        return again(guard(s_clickaway_click), 250)

    def s_clickaway_click():
        x0, y0 = state["xy"]

        def after():
            check("clickaway_dismisses",
                  not win()._ctx_pop.get_visible(), "hidden")
            check("clickaway_no_pause_toggle", paused() is True,
                  str(paused()))
            return again(guard(s_unpause), 150)
        return click(x0 - 140, y0 - 90, 1, guard(after))

    def s_unpause():
        x0, y0 = state["xy"]
        return click(x0, y0, 1, guard(lambda: wait_for(
            "left_click_resumes_after_menu", lambda: paused() is False,
            guard(s_right_drag))))

    def s_right_drag():
        pos0 = wpos()
        x0, y0 = state["xy"]
        fake_button("press", x0, y0, 3)

        def step(i):
            if i <= 6:
                d = state["xd"]
                d.screen().root.warp_pointer(x0 + 12 * i, y0)
                d.sync()
                return again(guard(lambda: step(i + 1)), 35)
            fake_button("release", x0 + 80, y0, 3)
            return again(guard(s_right_drag_check), 450)
        return again(guard(lambda: step(1)), 40)

    def s_right_drag_check():
        check("right_drag_no_menu", not win()._ctx_pop.get_visible(),
              "hidden")
        check("right_drag_no_window_move", wpos() == state["pos0"]
              if "pos0" in state else wpos() is not None, str(wpos()))
        return again(guard(s_repeat), 150)

    def s_repeat():
        w = win()
        state["pos0"] = wpos()
        from gi.repository import GLib
        w.activate_action("ctx.repeat", GLib.Variant("s", "one"))
        return again(guard(lambda: wait_for(
            "repeat_menu_sets_loop_file",
            lambda: str(w.core.snapshot.get("loop-file")) in
            ("inf", "force"), guard(s_repeat_off))), 150)

    def s_repeat_off():
        w = win()
        check("repeat_action_state_tracks",
              w._ctx_repeat.get_state().unpack() == "one",
              w._ctx_repeat.get_state().unpack())
        from gi.repository import GLib
        w.activate_action("ctx.repeat", GLib.Variant("s", "off"))
        return again(guard(lambda: wait_for(
            "repeat_menu_clears_loop_file",
            lambda: str(w.core.snapshot.get("loop-file")) in
            ("no", "none", ""), guard(s_top_phase))), 150)

    def s_top_phase():
        w = win()
        if phase == "2":
            check("top_menu_off_tree",
                  w.chrome.top.get_parent() is None
                  and not in_tree(w.overlay, w.chrome.top),
                  str(w.chrome.top.get_parent()))
            rows = body_rows()   # rebuild + compare identical order
            labels = [r[0] for r in rows]
            check("menu_identical_without_top_menu",
                  labels == ["perf_1080p30.mp4", "Audio", "Subtitles",
                             "Repeat", "Playlist", "Chapters", "Tracks"],
                  str(labels))
            p = shot("menu-topmenu-off")
            check("phase2_shot", p.exists(), str(p))
        check("window_alive", w.get_realized(), "True")
        app.quit()
        return False

    def start(left=40):
        w = None
        try:
            w = app.windows[0]
        except IndexError:
            pass
        if w is None or not w.get_realized():
            return again(lambda: start(left - 1), 200)
        if "xd" not in state:
            xopen()
        x = xfind()
        if x is None:
            if left <= 0:
                check("x_window_found", False, "no REELSEE window")
                app.quit()
                return False
            return again(lambda: start(left - 1), 300)
        check("x_window_found", True, "mapped")
        state["winx"] = x
        w.present()
        state["xd"].sync()
        return again(guard(s_structural), 600)

    GLib.timeout_add(300, guard(start))


def main() -> int:
    if PHASE == "1":
        rc = reelsee.run(["reelsee"] + FILES[:1],
                         on_app=lambda a: base_schedule(a, "1"))
        check("phase1_rc", rc == 0, str(rc))
        with open(CFG_DIR / "config.json", "w",
                  encoding="utf-8") as fh:
            json.dump({"ui": {"show_top_menu": False}}, fh)
        env = dict(os.environ, REELSEE_R6MENU_PHASE="2")
        out = subprocess.run(
            [sys.executable, os.path.abspath(__file__)] + FILES[:1],
            env=env, capture_output=True, text=True, timeout=180)
        sys.stdout.write(out.stdout)
        sys.stderr.write(out.stderr[-4000:])
        check("phase2_rc", out.returncode == 0, str(out.returncode))
        check("phase2_asserts", "SMOKE_ASSERTS_PASSED" in out.stdout,
              "marker")
    else:
        rc = reelsee.run(["reelsee"] + FILES[:1],
                         on_app=lambda a: base_schedule(a, "2"))
        check("phase2_inner_rc", rc == 0, str(rc))
    finish()
    return 0


if __name__ == "__main__":
    sys.exit(main())
