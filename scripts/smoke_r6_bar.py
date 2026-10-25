#!/usr/bin/python3
"""R6 gate smoke: the FINAL bottom bar under real X11 input (brief §B/§C).

Live widget tree asserts the exact left->right order (BOTTOM_ORDER by
name), the ABSENCE of any stop widget, then REAL XTEST events drive the
bar: Space pauses and the play/pause button's ICON flips to start (and
back), "m" flips the mute icon muted<->high together with the mpv mute
property, and a real LEFT CLICK on the mute button writes audio.muted
through the same action — without touching playback.  Bottom-bar
screenshots (bar up vs muted icon) are captured for the operator report.
"""
import os
import sys

from smoke_harness import (SCRATCH, capture_xwd, check, finish, image_mean)

CFG_DIR = SCRATCH / "r6bar-cfg"
STATE_DIR = SCRATCH / "r6bar-state"
for _d in (CFG_DIR, STATE_DIR):
    _d.mkdir(parents=True, exist_ok=True)
for _f in (CFG_DIR / "config.json", STATE_DIR / "resume.json"):
    if _f.exists():
        _f.unlink()
os.environ["REELSEE_CONFIG_DIR"] = str(CFG_DIR)
os.environ["REELSEE_STATE_DIR"] = str(STATE_DIR)

import reelsee  # noqa: E402
from chrome import BOTTOM_ORDER  # noqa: E402

FILES = ["testdata/perf_1080p30.mp4"]
state: dict = {}


def base_schedule(app):
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

    def setfocus():
        # no window manager in this Xvfb: take X input focus explicitly
        # so GTK routes XTEST keys into the window's key controllers.
        from Xlib import X
        state["winx"].set_input_focus(X.RevertToParent, X.CurrentTime)
        state["xd"].sync()

    def xkey(ch):
        from Xlib import X, XK
        from Xlib.ext import xtest
        d = state["xd"]
        ks = getattr(XK, "XK_space" if ch == " " else f"XK_{ch}")
        kc = d.keysym_to_keycode(ks)
        xtest.fake_input(d, X.KeyPress, kc)
        d.sync()
        xtest.fake_input(d, X.KeyRelease, kc)
        d.sync()

    def children(box):
        out, c = [], box.get_first_child()
        while c is not None:
            out.append(c)
            c = c.get_next_sibling()
        return out

    def pp_icon():
        return win().chrome.btn_play.get_child().get_icon_name()

    def mute_icon_name():
        return win().chrome.btn_mute.get_child().get_icon_name()

    def mpv_mute():
        v = win().core.snapshot.get("mute")
        return None if v is None else bool(v)

    def paused():
        v = win().core.snapshot.get("pause")
        return None if v is None else bool(v)

    # ------------------------------------------------------ assertions
    def s_structural():
        w = win()
        names = [c.get_name() for c in children(w.chrome.bottom)]
        check("bottom_order_exact", names == list(BOTTOM_ORDER), str(names))
        check("play_pause_first",
              names[0] == "play_pause"
              and type(children(w.chrome.bottom)[0]).__name__ == "Button"
              and children(w.chrome.bottom)[0].get_child().__class__.__name__
              == "Image", names[0])
        check("no_stop_anywhere",
              not hasattr(w.chrome, "btn_stop")
              and not any("stop" in n for n in names), str(names))
        # default audio.muted=True must ALREADY show the muted icon at
        # load (config -> mpv at load -> snapshot -> paint):
        return again(guard(lambda: wait_for(
            "default_mute_painted",
            lambda: mpv_mute() is True
            and mute_icon_name() == "audio-volume-muted-symbolic",
            guard(s_playing_baseline))), 100)

    def s_playing_baseline():
        return again(guard(lambda: wait_for(
            "playing_at_start", lambda: paused() is False,
            guard(s_play_click1))), 100)

    def click_widget(name, then):
        # REAL pointer press/release on a bottom-bar button (coords via
        # compute_point — the bar keeps its allocation at any opacity)
        w = win()
        b = getattr(w.chrome, name)
        ok, rect = b.compute_bounds(w)
        assert ok, "button has no bounds in window coords"
        x = int(rect.origin.x + rect.size.width / 2)
        y = int(rect.origin.y + rect.size.height / 2)
        return click(x, y, 1, then)

    def s_play_click1():
        # real CLICK on the first button: pause -> icon flips to start
        check("pp_icon_playing", pp_icon() == "media-playback-pause-symbolic",
              pp_icon())
        return click_widget("btn_play", guard(lambda: wait_for(
            "play_button_click_pauses", lambda: paused() is True,
            guard(s_pp_icon_paused))))

    def s_pp_icon_paused():
        check("pp_icon_paused", pp_icon() == "media-playback-start-symbolic",
              pp_icon())
        return click_widget("btn_play", guard(lambda: wait_for(
            "play_button_click_resumes", lambda: paused() is False,
            guard(s_m_key))))

    def s_m_key():
        setfocus()
        xkey("m")
        return again(guard(lambda: wait_for(
            "m_key_unmutes", lambda: mpv_mute() is False,
            guard(s_mute_icon_high))), 100)

    def s_mute_icon_high():
        check("mute_icon_unmuted",
              mute_icon_name() == "audio-volume-high-symbolic",
              mute_icon_name())
        xkey("m")
        return again(guard(lambda: wait_for(
            "m_key_re_mutes", lambda: mpv_mute() is True,
            guard(s_mute_icon_muted))), 100)

    def s_mute_icon_muted():
        check("mute_icon_remuted",
              mute_icon_name() == "audio-volume-muted-symbolic",
              mute_icon_name())
        state["mean_before_click"] = image_mean(shot("bar-mute-on"))
        return again(guard(s_click_mute), 100)

    def s_click_mute():
        # real POINTER click on the mute button itself (bar is revealed
        # by the warp inside click(); coords computed after that wake)
        w = win()
        ok, rect = w.chrome.btn_mute.compute_bounds(w)
        check("mute_btn_has_bounds", bool(ok), str(ok))
        x = int(rect.origin.x + rect.size.width / 2)
        y = int(rect.origin.y + rect.size.height / 2)
        state["pause_at_click"] = paused()
        return click(x, y, 1, guard(lambda: wait_for(
            "mute_button_click_unmutes", lambda: mpv_mute() is False,
            guard(s_click_effects))))

    def s_click_effects():
        w = win()
        check("mute_click_writes_config",
              w.cfg.get("audio.muted") is False, str(w.cfg.get(
                  "audio.muted")))
        check("mute_click_keeps_playback", paused() is False, "unchanged")
        shot("bar-muted-off")
        check("window_alive", w.get_realized(), "True")
        app.quit()
        return False

    def shot(name):
        p = SCRATCH / f"r6bar-{name}.xwd"
        capture_xwd(p)
        return p

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
    rc = reelsee.run(["reelsee"] + FILES[:1], on_app=base_schedule)
    check("rc", rc == 0, str(rc))
    finish()
    return 0


if __name__ == "__main__":
    sys.exit(main())
