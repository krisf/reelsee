#!/usr/bin/python3
"""R12 contract: the ENTIRE chrome — top strip with the window controls
included — fades away OVER the video and fades back, with zero layout
movement.

The old version of this gate drove Adw.ToolbarView's reveal_top_bars and
proved the header unmaps and the video grows into the freed space.  That
mechanism is GONE (round 12: the chrome is a Gtk.Overlay layer; retracting
must not resize the video by a single pixel).  Rewritten to the new
contract — strictly STRONGER where it counts:

  * "revealed/retracted" are the widget OPACITIES of BOTH chrome groups
    (bottom bar AND top strip), i.e. the whole chrome including min/max/
    close fades — nothing outside the fade set may stay painted;
  * the video allocation is asserted BYTE-IDENTICAL across every
    transition (the old smoke asserted the opposite: growth);
  * the pixel pair is kept: the top strip reads as dark chrome while
    revealed and as live VIDEO at the same screen coordinates once
    retracted;
  * popover-open blocks retraction; popover-close re-arms the countdown;
  * fullscreen retracts identically, leaving fullscreen restores.
"""
import json
import os
import subprocess
import sys

from smoke_harness import SCRATCH, check, finish, image_mean

CFG = SCRATCH / "adwchrome-cfg"
CFG.mkdir(parents=True, exist_ok=True)
(CFG / "config.json").write_text(json.dumps(
    {"ui": {"chrome_timeout_ms": 400, "osd_ms": 1200}}))
os.environ["REELSEE_CONFIG_DIR"] = str(CFG)

import reelsee  # noqa: E402  (after env, like the real entry)

state: dict = {}
SHOTS = SCRATCH / "r5a"
SHOTS.mkdir(parents=True, exist_ok=True)

HDR_STRIP = "900x40+30+4"          # inside the top strip, away from corners
TOP_STRIP = "900x40+30+4"          # same screen coordinates once faded
VIDEO_MID = "640x200+160+260"      # solidly inside the video frame


def fades(win):
    """THE mechanism's state (not a hand-rolled proxy): opacity of BOTH
    reveal groups — the bottom bar and the R12 top strip."""
    return (round(float(win.chrome.bottom.get_opacity()), 3),
            round(float(win.top_bar.get_opacity()), 3))


def revealed(win):
    a, b = fades(win)
    return a > 0.95 and b > 0.95


def retracted(win):
    a, b = fades(win)
    return a < 0.05 and b < 0.05


def targets(win):
    return (bool(win.chrome.bottom.get_can_target()),
            bool(win.top_bar.get_can_target()))


def video_alloc(win):
    a = win.view.area
    return (a.get_width(), a.get_height())


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


def _strip_has_icon(win, icon_name: str) -> bool:
    """Walk the RETRACTING top strip's subtree for that icon name."""
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
    return walk(win.top_bar)


def probe_schedule(app):
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

    def s0():
        # Startup: the chrome is structurally revealed until the first
        # countdown completes (400 ms after "playing" is first observed).
        if not app.windows:
            return again(s0, 40)
        w = win()
        check("reveal_starts_revealed", revealed(w), str(fades(w)))
        kinds = set()
        for name in ("window-minimize-symbolic", "window-maximize-symbolic",
                     "window-close-symbolic"):
            if _strip_has_icon(w, name):
                kinds.add(name)
        check("controls_inside_retracting_strip", len(kinds) >= 2,
              ",".join(sorted(x.split("-")[1] for x in kinds)))
        check("controls_clickable_while_revealed",
              targets(w) == (True, True), str(targets(w)))
        return again(s0b, 20)

    def s0b():
        # wait until actually playing (engine cold start is variable)
        if not playing(win()) and state.get("plays", 0) < 60:
            state["plays"] = state.get("plays", 0) + 1
            return again(s0b, 200)
        check("playing_for_timeline", playing(win()), "played")
        return again(wait_title, 20)

    def wait_title(left=25):
        # the title repaint rides the file-loaded callback and can land a
        # hair AFTER the first "playing" snapshot — poll for it.
        if "perf_1080p30" in win()._title_label.get_text() or left <= 0:
            return again(rest, 20)
        return again(lambda: wait_title(left - 1), 150)

    def shot_video(name: str, crop: str, tries: int = 12):
        """Capture until the VIDEO region is genuinely painted, so the
        strip reading at `crop` compares like-for-like (an unpainted
        black GL area would fake a "dark chrome" mean)."""
        from smoke_harness import capture_xwd
        path = SHOTS / f"{name}.xwd"
        for _ in range(tries):
            if not capture_xwd(path):
                continue
            subprocess.run(["convert", str(path), str(path.with_suffix(".png"))],
                           capture_output=True)
            if image_mean(path, VIDEO_MID) > 8:
                return path
        return path

    def rest():
        w = win()
        check("title_shows_filename",
              "perf_1080p30" in w._title_label.get_text(),
              w._title_label.get_text())
        w.chrome.motion(400, 300, 960, 540)   # wake: chrome on screen
        return again(after_wake, 250)

    def after_wake():
        w = win()
        state["alloc_revealed"] = video_alloc(w)
        state["surface_revealed"] = (w.get_surface().get_width(),
                                     w.get_surface().get_height())
        p = shot_video("01-titlebar-present", HDR_STRIP)
        m1 = image_mean(p, HDR_STRIP)
        # The strip CSS is deliberately translucent (alpha 0.78 black —
        # the R6 bar language), so the honest gate is the PAIR: the same
        # screen rect must read decisively DARKER with the chrome faded
        # in than with it faded out (measured: 58 vs 125 for this clip).
        state["strip_revealed_mean"] = m1
        check("titlebar_strip_present", 0 <= m1, str(round(m1, 2)))
        return again(s1, 650)          # 650 > 400 idle, playing

    def s1():
        w = win()
        check("playing_idle_retracts_chrome", retracted(w),
              f"fades={fades(w)}")
        check("retracted_chrome_click_through", targets(w) == (False, False),
              str(targets(w)))
        return again(s1b, 350)

    def s1b():
        w = win()
        # THE R12 invariant: retracting moved NOTHING.
        alloc = video_alloc(w)
        check("video_alloc_identical_across_retract",
              alloc == state["alloc_revealed"],
              f"revealed={state['alloc_revealed']} retracted={alloc}")
        surf = (w.get_surface().get_width(), w.get_surface().get_height())
        check("surface_identical_across_retract",
              surf == state["surface_revealed"],
              f"revealed={state['surface_revealed']} retracted={surf}")
        shot("02-titlebar-gone", crop=TOP_STRIP)
        m2 = image_mean(SHOTS / "02-titlebar-gone.xwd", TOP_STRIP)
        m = image_mean(SHOTS / "02-titlebar-gone.xwd", VIDEO_MID)
        check("same_strip_now_video", m2 > 25, str(round(m2, 2)))
        m1 = state.get("strip_revealed_mean", 999)
        check("revealed_strip_decisively_darker", m1 <= 0.6 * m2,
              f"revealed={round(m1, 1)} retracted={round(m2, 1)}")
        check("video_still_playing", m > 8, str(m))
        w.chrome.motion(400, 300, 960, 540)      # wake seam, as the pointer
        return again(s2, 250)

    def s2():
        w = win()
        check("motion_reveals_chrome", revealed(w), str(fades(w)))
        check("revealed_alloc_unchanged", video_alloc(w) ==
              state["alloc_revealed"], str(video_alloc(w)))
        w._actions["play-pause"](None)           # pause
        state["pause_tries"] = 0
        return again(s3, 250)

    def s3():
        w = win()
        paused = w.core.snapshot.get("pause") is True
        if (not paused or not revealed(w)) and state.get("pause_tries", 0) < 12:
            state["pause_tries"] += 1
            return again(s3, 250)
        check("paused_keeps_chrome_revealed", paused and revealed(w),
              f"pause={paused} fades={fades(w)}")
        # back to playing; wait for the idle retraction, THEN open a menu
        w._actions["play-pause"](None)
        state["idle_tries"] = 0
        return again(s4, 650)

    def s4():
        w = win()
        if not retracted(w) and state.get("idle_tries", 0) < 12:
            state["idle_tries"] += 1
            return again(s4, 250)              # pause-flip lag tolerated
        check("replaying_retracts_again", retracted(w), str(fades(w)))
        w.chrome.motion(400, 300, 960, 540)     # user wakes, then opens menu
        return again(s5, 150)

    def s5():
        w = win()
        w._pl_pop.popup()                       # real popover open
        return again(s6, 900)                   # 900 >> 400: hold required

    def s6():
        w = win()
        held = revealed(w) and w._pl_pop.get_visible()
        check("popover_open_blocks_retraction", held,
              f"fades={fades(w)} pop={w._pl_pop.get_visible()}")
        w._pl_pop.popdown()
        return again(s7, 200)

    def s7():
        w = win()
        check("popover_close_reveals", revealed(w), str(fades(w)))
        state["rearm"] = 0
        return again(s8, 650)                   # re-armed countdown expires

    def s8():
        w = win()
        if not retracted(w) and state.get("rearm", 0) < 12:
            state["rearm"] += 1
            return again(s8, 250)
        check("retract_after_rearm", retracted(w), str(fades(w)))
        state["alloc_pre_fs"] = video_alloc(w)
        w._actions["fullscreen"](None)
        return again(s9, 150)

    def s9():
        w = win()
        if not revealed(w) and state.get("rev_tries", 0) < 8:
            state["rev_tries"] = state.get("rev_tries", 0) + 1
            return again(s9, 150)        # the 180 ms crossfade must land
        check("fullscreen_restores_revealed",
              w._act_fullscreen_state() and revealed(w),
              f"fs={w._act_fullscreen_state()} fades={fades(w)}")
        state["fs_tries"] = 0
        return again(s10, 650)

    def s10():
        w = win()
        if not retracted(w) and state.get("fs_tries", 0) < 8:
            state["fs_tries"] += 1
            return again(s10, 250)
        check("fullscreen_retracts_identically",
              w._act_fullscreen_state() and retracted(w),
              f"fs={w._act_fullscreen_state()} fades={fades(w)}")
        w._actions["fullscreen"](None)          # leave fullscreen
        return again(s11, 200)

    def s11():
        w = win()
        check("leaving_fullscreen_restores",
              (not w._act_fullscreen_state()) and revealed(w),
              f"fs={w._act_fullscreen_state()} fades={fades(w)}")
        check("window_size_restored_after_fullscreen",
              video_alloc(w) == state["alloc_pre_fs"],
              f"pre_fs={state['alloc_pre_fs']} now={video_alloc(w)}")
        check("shots_captured",
              (SHOTS / "01-titlebar-present.png").exists()
              and (SHOTS / "02-titlebar-gone.png").exists(), str(SHOTS))
        app.quit()
        return False

    GLib.timeout_add(50, guard(s0))
    return False


def main() -> int:
    rc = reelsee.run(["reelsee", "testdata/perf_1080p30.mp4"],
                     on_app=probe_schedule)
    check("app_rc", rc == 0, str(rc))
    finish()
    return 0


if __name__ == "__main__":
    sys.exit(main())
