#!/usr/bin/python3
"""P3 gate smoke (R12 contract): chrome autohide lifecycle through the
REAL window pipeline — and the chrome is now an OVERLAY.

Same §4.6 timeline as always (test config ui.chrome_timeout_ms=400 for a
fast deterministic run; motion goes through Chrome.motion(), the path the
EventControllerMotion handler calls): idle->hidden, motion->visible +
re-arm, keyboard OSD without revealing bars, pause->visible and stays.

R12 CONTRACT: reveal/hide is OPACITY ONLY — bottom bar and the top
overlay strip (title + window controls) fade as ONE group; nothing uses
child-visible or layout space any more.  So "visible" means opacity ~1
(+ can_target True) and "hidden" means opacity ~0 with can_target False
— and because the chrome never reserves layout, the VIDEO's allocation
must be the same number the whole timeline long (asserted at every
transition; the exhaustive byte-identical gate is smoke_overlaychrome).
"""
import json
import os
import sys

from smoke_harness import SCRATCH, check, finish

CFG = SCRATCH / "chrome-cfg"
CFG.mkdir(parents=True, exist_ok=True)
(CFG / "config.json").write_text(json.dumps(
    {"ui": {"chrome_timeout_ms": 400, "osd_ms": 1200}}))
os.environ["REELSEE_CONFIG_DIR"] = str(CFG)
os.environ["REELSEE_STATE_DIR"] = str(SCRATCH / "chrome-state")

import reelsee  # noqa: E402  (after env, like the real entry)

state: dict = {}


def chrome_fade(win) -> tuple[float, float]:
    """(bottom opacity, top-strip opacity) — the R12 reveal contract."""
    return (float(win.chrome.bottom.get_opacity()),
            float(win.top_bar.get_opacity()))


def bars_visible(win) -> bool:
    """R12 "visible": BOTH reveal groups faded IN and click-able."""
    ob, ot = chrome_fade(win)
    return ob > 0.95 and ot > 0.95 and win.chrome.bottom.get_can_target() \
        and win.top_bar.get_can_target()


def bars_hidden(win) -> bool:
    """R12 "hidden": BOTH groups faded OUT and click-THROUGH."""
    ob, ot = chrome_fade(win)
    return ob < 0.05 and ot < 0.05 \
        and not win.chrome.bottom.get_can_target() \
        and not win.top_bar.get_can_target()


def video_size(win) -> tuple[int, int]:
    """The render view's allocation — the number R12 says NEVER moves."""
    return (win.view.area.get_width(), win.view.area.get_height())


def probe_schedule(app):
    from gi.repository import GLib
    state["app"] = app

    def win():
        return app.windows[0]

    def s0():
        win().chrome.motion(400, 300, 960, 540)
        GLib.timeout_add(300, s1)         # 300 > the 180 ms reveal fade
        return False

    def s1():
        check("chrome_visible_initial", bars_visible(win()),
              str(chrome_fade(win())))
        state["size"] = video_size(win())
        GLib.timeout_add(650, s2)          # 650ms > 400ms idle (+ fade)
        return False

    def s2():
        hidden = bars_hidden(win())
        check("chrome_hidden_after_idle", hidden,
              f"fade={chrome_fade(win())} target="
              f"{win().chrome.bottom.get_can_target()}/"
              f"{win().top_bar.get_can_target()}")
        check("video_size_untouched_by_retract",
              video_size(win()) == state["size"],
              f"{video_size(win())} vs {state['size']}")
        win()._actions["seek-small"](+1)   # keyboard seek while hidden
        GLib.timeout_add(150, s3)
        return False

    def s3():
        c = win().chrome
        check("osd_transient", c.osd.get_visible(), str(c.osd.get_label()))
        stay = bars_hidden(win())          # assertion result, not raw flag
        check("bars_stay_hidden", stay, str(stay))
        c.motion(400, 300, 960, 540)       # pointer motion reveals
        GLib.timeout_add(250, s4)         # 250 > the 180 ms reveal fade
        return False

    def s4():
        check("chrome_visible_after_motion", bars_visible(win()),
              str(chrome_fade(win())))
        check("video_size_untouched_by_reveal",
              video_size(win()) == state["size"],
              f"{video_size(win())} vs {state['size']}")
        win()._actions["play-pause"](None)     # pause: chrome stays
        GLib.timeout_add(1000, s5)
        return False

    def s5():
        # self-paced: pause must round-trip into the snapshot and reveal the
        # chrome (not-playing rule); 3s ceiling catches real regressions.
        snap = win().core.snapshot
        paused = snap.get("pause") is True
        if (not paused or not bars_visible(win())) and state.get("tries", 0) < 12:
            state["tries"] = state.get("tries", 0) + 1
            GLib.timeout_add(250, s5)
            return False
        check("pause_roundtrip", paused, repr(snap.get("pause")))
        check("chrome_visible_when_paused", bars_visible(win()),
              str(chrome_fade(win())))
        check("video_size_final", video_size(win()) == state["size"],
              f"{video_size(win())} vs {state['size']}")
        app.quit()
        return False

    GLib.timeout_add(800, s0)
    return False


def main() -> int:
    # 10s clip: the whole ~6s probe timeline stays far from EOF (EOF would
    # legitimately reveal the chrome — §4.6).
    rc = reelsee.run(["reelsee", "testdata/perf_1080p30.mp4"],
                     on_app=probe_schedule)
    check("app_rc", rc == 0, str(rc))
    finish()
    return 0


if __name__ == "__main__":
    sys.exit(main())
