#!/usr/bin/python3
"""R6 gate smoke: mute survives a FULL restart (two real processes).

Phase 1 (process A, fresh config): default audio.muted=True is applied
at load (muted icon); a REAL "m" key press unmutes — icon flips AND the
persisted config (audio.muted) is rewritten immediately (PERSIST_NOW);
a second press re-mutes; the on-disk value must be true again before
process A exits.  Phase 2 re-execs this script as a FRESH process with
the same config dir and asserts, BEFORE any user interaction, that the
mute state at load comes from disk: the mpv `mute` property is true AND
the mute button shows the muted icon.  That is the two-process proof the
brief asks for: button state AND mpv value.
"""
import json
import os
import subprocess
import sys

from smoke_harness import SCRATCH, check, finish

CFG_DIR = SCRATCH / "r6mute-cfg"
STATE_DIR = SCRATCH / "r6mute-state"
for _d in (CFG_DIR, STATE_DIR):
    _d.mkdir(parents=True, exist_ok=True)

PHASE = os.environ.get("REELSEE_R6MUTE_PHASE", "1")
CFG_FILE = CFG_DIR / "config.json"
if PHASE == "1" and CFG_FILE.exists():
    CFG_FILE.unlink()
os.environ["REELSEE_CONFIG_DIR"] = str(CFG_DIR)
os.environ["REELSEE_STATE_DIR"] = str(STATE_DIR)

import reelsee  # noqa: E402

FILES = ["testdata/perf_1080p30.mp4"]
MUTED = "audio-volume-muted-symbolic"
HIGH = "audio-volume-high-symbolic"
state: dict = {}


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

    def mpv_mute():
        v = win().core.snapshot.get("mute")
        return None if v is None else bool(v)

    def icon():
        return win().chrome.btn_mute.get_child().get_icon_name()

    def disk_muted():
        try:
            with open(CFG_FILE, encoding="utf-8") as fh:
                return json.load(fh).get("audio", {}).get("muted")
        except FileNotFoundError:
            return None

    def ensure_xd():
        if "xd" not in state:
            from Xlib import display as xdisplay
            state["xd"] = xdisplay.Display(
                os.environ.get("DISPLAY", ":0"))
        return state["xd"]

    def xkey(ch):
        from Xlib import X, XK
        from Xlib.ext import xtest
        d = ensure_xd()
        kc = d.keysym_to_keycode(getattr(XK, f"XK_{ch}"))
        xtest.fake_input(d, X.KeyPress, kc)
        d.sync()
        xtest.fake_input(d, X.KeyRelease, kc)
        d.sync()

    def focus_window():
        # no WM in this Xvfb: present, then TAKE X input focus explicitly
        # so GTK routes the XTEST key into the window controllers (the
        # exact recipe proven by the focus probe in this round).
        from Xlib import X
        d = ensure_xd()
        win().present()
        d.sync()
        root = d.screen().root
        big = [None, 0]

        def walk(c, dep=0):
            if dep > 6:
                return
            try:
                if c.get_attributes().map_state == X.IsViewable and c != root:
                    g = c.get_geometry()
                    if g.width >= 800 and g.height >= 400 and \
                            g.width * g.height > big[1]:
                        big[0], big[1] = c, g.width * g.height
            except Exception:                       # noqa: BLE001
                pass
            for k in c.query_tree().children:
                walk(k, dep + 1)
        walk(root)
        if big[0] is not None:
            big[0].set_input_focus(X.RevertToParent, X.CurrentTime)
            d.sync()

    # ------------------------------------------------------------ phase 1
    def p1_loaded():
        return again(guard(lambda: wait_for(
            "p1_default_mute_at_load",
            lambda: mpv_mute() is True and icon() == MUTED,
            guard(p1_unmute))), 100)

    def p1_unmute():
        focus_window()
        xkey("m")
        return again(guard(lambda: wait_for(
            "p1_key_unmutes_mpv", lambda: mpv_mute() is False,
            guard(p1_unmute_state))), 100)

    def p1_unmute_state():
        check("p1_icon_high", icon() == HIGH, icon())
        check("p1_config_flipped_on_disk", disk_muted() is False,
              str(disk_muted()))
        focus_window()
        xkey("m")
        return again(guard(lambda: wait_for(
            "p1_key_re_mutes_mpv", lambda: mpv_mute() is True,
            guard(p1_re_mute_state))), 100)

    def p1_re_mute_state():
        check("p1_icon_muted_again", icon() == MUTED, icon())
        check("p1_config_true_on_disk", disk_muted() is True,
              str(disk_muted()))
        app.quit()
        return False

    # ------------------------------------------------------------ phase 2
    def p2_loaded():
        # NO interaction whatsoever before these asserts: process 2 must
        # apply the persisted mute purely at load.
        return again(guard(lambda: wait_for(
            "p2_mute_applied_at_load",
            lambda: mpv_mute() is True,
            guard(p2_button_state))), 100)

    def p2_button_state():
        check("p2_mute_button_shows_muted", icon() == MUTED, icon())
        check("p2_disk_still_true", disk_muted() is True, str(disk_muted()))
        check("p2_no_transient_muted",
              getattr(win(), "_transient_muted", False) is False, "False")
        app.quit()
        return False

    def start(left=40):
        w = None
        try:
            w = app.windows[0]
        except IndexError:
            pass
        if w is None or not w.get_realized():
            if left <= 0:
                check("window_realized", False, "never realized")
                app.quit()
                return False
            return again(lambda: start(left - 1), 200)
        first = p1_loaded if phase == "1" else p2_loaded
        return again(guard(first), 400)

    GLib.timeout_add(400, guard(start))


def main() -> int:
    if PHASE == "1":
        rc = reelsee.run(["reelsee"] + FILES[:1],
                         on_app=lambda a: base_schedule(a, "1"))
        check("phase1_rc", rc == 0, str(rc))
        env = dict(os.environ, REELSEE_R6MUTE_PHASE="2")
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
