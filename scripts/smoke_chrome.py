#!/usr/bin/python3
"""P3 gate smoke: chrome autohide lifecycle through the REAL window pipeline.

Uses a test config dir with ui.chrome_timeout_ms=400 for a fast, deterministic
timeline; motion events go through Chrome.motion() — the same path the
EventControllerMotion handler calls.  Checks §4.6 rules:
idle->hidden, motion->visible+re-arm, keyboard OSD without revealing bars,
pause->visible and stays.
"""
import json
import os
import sys
import tempfile

from smoke_harness import SCRATCH, check, finish

CFG = SCRATCH / "chrome-cfg"
CFG.mkdir(parents=True, exist_ok=True)
(CFG / "config.json").write_text(json.dumps(
    {"ui": {"chrome_timeout_ms": 400, "osd_ms": 1200}}))
os.environ["REELSEE_CONFIG_DIR"] = str(CFG)

import reelsee  # noqa: E402  (after env, like the real entry)

state: dict = {}


def bars_visible(win) -> bool:
    return win.chrome.bottom.get_child_visible()


def probe_schedule(app):
    from gi.repository import GLib
    state["app"] = app

    def win():
        return app.windows[0]

    def s0():
        win().chrome.motion(400, 300, 960, 540)
        GLib.timeout_add(150, s1)
        return False

    def s1():
        check("chrome_visible_initial", bars_visible(win()),
              str(bars_visible(win())))
        GLib.timeout_add(650, s2)          # 650ms > 400ms idle
        return False

    def s2():
        hidden = not bars_visible(win())
        check("chrome_hidden_after_idle", hidden, str(hidden))
        win()._actions["seek-small"](+1)   # keyboard seek while hidden
        GLib.timeout_add(150, s3)
        return False

    def s3():
        c = win().chrome
        check("osd_transient", c.osd.get_visible(), str(c.osd.get_label()))
        stay = not bars_visible(win())      # assertion result, not raw flag
        check("bars_stay_hidden", stay, str(stay))
        c.motion(400, 300, 960, 540)       # pointer motion reveals
        GLib.timeout_add(150, s4)
        return False

    def s4():
        check("chrome_visible_after_motion", bars_visible(win()),
              str(bars_visible(win())))
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
              str(bars_visible(win())))
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
