#!/usr/bin/python3
"""R5-A gate smoke: the ENTIRE chrome — real titlebar included — retracts.

Drives the reveal mechanism of a real Adw.ApplicationWindow + Adw.ToolbarView
through the same ChromeState timeline as smoke_chrome (ui.chrome_timeout_ms
=400).  Checks the brief's transition sequence:

  start revealed -> playing+idle retracts -> motion reveals -> paused stays
  -> popover open holds revealed (no stranded menus) -> close re-arms ->
  fullscreen retracts identically -> leaving fullscreen restores revealed.

Also captures the report's screenshot pair (titlebar present, then absent,
video playing) and proves min/max/close buttons are INSIDE the retracting
Adw.HeaderBar (they vanish with it), never a separate WM decoration.
"""
import json
import os
import subprocess
import sys

from smoke_harness import SCRATCH, check, finish, image_colors, image_mean

CFG = SCRATCH / "adwchrome-cfg"
CFG.mkdir(parents=True, exist_ok=True)
(CFG / "config.json").write_text(json.dumps(
    {"ui": {"chrome_timeout_ms": 400, "osd_ms": 1200}}))
os.environ["REELSEE_CONFIG_DIR"] = str(CFG)

import reelsee  # noqa: E402  (after env, like the real entry)

state: dict = {}
SHOTS = SCRATCH / "r5a"
SHOTS.mkdir(parents=True, exist_ok=True)

HDR_STRIP = "900x40+30+4"          # inside the header, away from corners
TOP_STRIP = "900x40+30+4"          # same geometry once the video owns it
VIDEO_MID = "640x200+160+260"      # solidly inside the video frame


def image_std(xwd, crop: str) -> float:
    out = subprocess.run(
        ["convert", str(xwd), "-crop", crop, "+repage", "-colorspace",
         "gray", "-format", "%[fx:standard_deviation*255]", "info:"],
        capture_output=True, text=True)
    try:
        return float(out.stdout.strip())
    except ValueError:
        return -1.0


def reveal(win) -> bool:
    """THE mechanism's current state (not a hand-rolled proxy)."""
    return win._toolbar.get_reveal_top_bars()


def shot(name: str, crop: str = VIDEO_MID, tries: int = 10):
    """Capture + PNG, retrying until the video region is actually painted
    (first GL frame lands slightly after mpv reports time-pos)."""
    from smoke_harness import capture_xwd
    path = SHOTS / f"{name}.xwd"
    for _ in range(tries):
        if not capture_xwd(path):
            continue
        subprocess.run(["convert", str(path), str(path.with_suffix(".png"))],
                       capture_output=True)
        if image_mean(path, crop) > 8:
            return True
    return False


def playing(win) -> bool:
    snap = win.core.snapshot
    return (snap.get("pause") is False and snap.get("idle-active") is not True
            and (snap.get("time-pos") or 0) > 0.05)


def probe_schedule(app):
    from gi.repository import GLib
    state["app"] = app

    def win():
        return app.windows[0]

    def again(fn, ms):
        GLib.timeout_add(ms, fn)
        return False

    def s0():
        # R5 restructure: the reveal-at-start and header-structure checks
        # used to ride the first "playing" poll — with a slow engine cold
        # start the 400 ms idle retraction fires BEFORE playing is first
        # observed, and the check raced.  Assert the startup state the
        # MOMENT the window exists (reveal is structurally true until the
        # first countdown completes, 400 ms later), then gate on playing.
        if not app.windows:
            return again(s0, 40)
        w = win()
        check("reveal_starts_revealed", reveal(w), str(reveal(w)))
        kinds = set()
        for name in ("window-minimize-symbolic", "window-maximize-symbolic",
                     "window-close-symbolic"):
            if _header_has_icon(w, name):
                kinds.add(name)
        check("headerbar_owns_window_controls", len(kinds) >= 2,
              ",".join(sorted(x.split("-")[1] for x in kinds)))
        return again(s0b, 20)

    def s0b():
        # wait until actually playing (engine cold start is variable)
        if not playing(win()) and state.get("plays", 0) < 60:
            state["plays"] = state.get("plays", 0) + 1
            return again(s0b, 200)
        check("playing_for_timeline", playing(win()), "played")
        return again(wait_title, 20)

    def wait_title(left=25):
        # the title repaint rides the file-loaded observed callback and
        # can land a hair AFTER the first "playing" snapshot (R5: extra
        # observed props shifted event interleaving) — poll for it;
        # delaying the chain from here is harmless (s1/s1b tolerate it).
        if "perf_1080p30" in win()._title_label.get_text() or left <= 0:
            return again(rest, 20)
        return again(lambda: wait_title(left - 1), 150)

    def rest():
        w = win()
        check("title_shows_filename",
              "perf_1080p30" in w._title_label.get_text(),
              w._title_label.get_text())
        w.chrome.motion(400, 300, 960, 540)   # wake: header on screen
        return again(after_wake, 200)

    def after_wake():
        shot("01-titlebar-present")
        m1 = image_mean(SHOTS / "01-titlebar-present.xwd", HDR_STRIP)
        check("titlebar_strip_present", 0 <= m1 <= 12, str(round(m1, 2)))
        return again(s1, 650)          # 650 > 400 idle, playing

    def s1():
        w = win()
        check("playing_idle_retracts_header", not reveal(w),
              f"reveal={reveal(w)}")
        return again(s1b, 350)             # let the reveal animation settle

    def s1b():
        w = win()
        check("header_unmapped_after_anim", not w.header.get_mapped(),
              str(w.header.get_mapped()))
        # content grew into the freed space: overlay now fills the window
        ov_h, wh = w.overlay.get_height(), w.get_height()
        check("video_fills_window_when_retracted",
              abs(ov_h - wh) <= 2 and _at_top(w), f"h={ov_h}/{wh}")
        shot("02-titlebar-gone", crop=TOP_STRIP)
        m2 = image_mean(SHOTS / "02-titlebar-gone.xwd", TOP_STRIP)
        m = image_mean(SHOTS / "02-titlebar-gone.xwd", VIDEO_MID)
        check("same_strip_now_video", m2 > 25, str(round(m2, 2)))
        check("video_still_playing", m > 8, str(m))
        w.chrome.motion(400, 300, 960, 540)      # wake seam, as the pointer
        return again(s2, 150)

    def s2():
        w = win()
        check("motion_reveals_header", reveal(w) and w.header.get_mapped(),
              str(reveal(w)))
        w._actions["play-pause"](None)           # pause
        state["pause_tries"] = 0
        return again(s3, 250)

    def s3():
        w = win()
        paused = w.core.snapshot.get("pause") is True
        if (not paused or not reveal(w)) and state.get("pause_tries", 0) < 12:
            state["pause_tries"] += 1
            return again(s3, 250)
        check("paused_keeps_header_revealed", paused and reveal(w),
              f"pause={paused} reveal={reveal(w)}")
        # back to playing; wait for the idle retraction, THEN open a menu
        w._actions["play-pause"](None)
        state["idle_tries"] = 0
        return again(s4, 650)

    def s4():
        w = win()
        if reveal(w) and state.get("idle_tries", 0) < 12:
            state["idle_tries"] += 1
            return again(s4, 250)              # still playing+paused flip lag
        check("replaying_retracts_again", not reveal(w), str(reveal(w)))
        w.chrome.motion(400, 300, 960, 540)     # user wakes, then opens menu
        return again(s5, 120)

    def s5():
        w = win()
        w._pl_pop.popup()                       # real popover open
        return again(s6, 900)                   # 900 >> 400: hold required

    def s6():
        w = win()
        held = reveal(w) and w._pl_pop.get_visible()
        check("popover_open_blocks_retraction", held,
              f"reveal={reveal(w)} pop={w._pl_pop.get_visible()}")
        w._pl_pop.popdown()
        return again(s7, 200)

    def s7():
        w = win()
        check("popover_close_reveals", reveal(w), str(reveal(w)))
        state["rearm"] = 0
        return again(s8, 650)                   # re-armed countdown expires

    def s8():
        w = win()
        check("retract_after_rearm", not reveal(w), str(reveal(w)))
        w._actions["fullscreen"](None)
        return again(s9, 150)

    def s9():
        w = win()
        check("fullscreen_restores_revealed",
              w._act_fullscreen_state() and reveal(w),
              f"fs={w._act_fullscreen_state()} reveal={reveal(w)}")
        state["fs_tries"] = 0
        return again(s10, 650)

    def s10():
        w = win()
        if reveal(w) and state.get("fs_tries", 0) < 8:
            state["fs_tries"] += 1
            return again(s10, 250)
        check("fullscreen_retracts_identically",
              w._act_fullscreen_state() and not reveal(w),
              f"fs={w._act_fullscreen_state()} reveal={reveal(w)}")
        w._actions["fullscreen"](None)          # leave fullscreen
        return again(s11, 150)

    def s11():
        w = win()
        check("leaving_fullscreen_restores",
              (not w._act_fullscreen_state()) and reveal(w),
              f"fs={w._act_fullscreen_state()} reveal={reveal(w)}")
        check("shots_captured",
              (SHOTS / "01-titlebar-present.png").exists()
              and (SHOTS / "02-titlebar-gone.png").exists(), str(SHOTS))
        app.quit()
        return False

    GLib.timeout_add(50, s0)
    return False


def _header_has_icon(win, icon_name: str) -> bool:
    """Walk the headerbar subtree for a widget carrying that icon name."""
    from gi.repository import Gtk

    def walk(w):
        if isinstance(w, Gtk.Image) and w.get_icon_name() == icon_name:
            return True
        child = w.get_first_child()
        while child is not None:
            if walk(child):
                return True
            child = child.get_next_sibling()
        return False
    return walk(win.header)


def _at_top(win) -> bool:
    """Overlay's top edge sits at the window's top (header space reclaimed)."""
    from gi.repository import Graphene
    ok, pt = win.overlay.compute_point(win, Graphene.Point())
    return bool(ok) and abs(pt.y) < 2


def main() -> int:
    rc = reelsee.run(["reelsee", "testdata/perf_1080p30.mp4"],
                     on_app=probe_schedule)
    check("app_rc", rc == 0, str(rc))
    finish()
    return 0


if __name__ == "__main__":
    sys.exit(main())
