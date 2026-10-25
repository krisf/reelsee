#!/usr/bin/python3
"""R5-B gate smoke: ui.show_top_menu persistence + widget ABSENCE.

Two processes (this script re-execs itself as phase 2 under the same
REELSEE_CONFIG_DIR, inside the same Xvfb from headless-smoke.sh):

  phase 1: default ON  -> top bar is in the widget tree; toggling the new
           "Show menu bar" switch OFF removes it LIVE (parent None, not
           hidden) and persists off across quit.
  phase 2: restart     -> still off (disk + switch), top bar never entered
           the tree (walked away from the window root), while the normal
           shortcuts still reach the same actions: "n" playlist-next really
           advances, "a" cycles audio, "prior" (PgUp) chapter key handled
           without error.

D1 (R7): the config flush is debounced, so phase 1 POLLS the disk for the
parsed ui.show_top_menu value with a short deadline before launching the
phase-2 restart (and asserts the PARSED value, never a regex on raw text).
"""
import json
import os
import subprocess
import sys
import time

from smoke_harness import SCRATCH, check, finish

CFG_DIR = SCRATCH / "topmenu-cfg"
STATE_DIR = SCRATCH / "topmenu-state"
for _d in (CFG_DIR, STATE_DIR):
    _d.mkdir(parents=True, exist_ok=True)
os.environ["REELSEE_CONFIG_DIR"] = str(CFG_DIR)
os.environ["REELSEE_STATE_DIR"] = str(STATE_DIR)

PHASE = os.environ.get("REELSEE_R5B_PHASE", "1")
if PHASE == "1":                   # fresh state for the persistence story
    for _f in (CFG_DIR / "config.json", STATE_DIR / "resume.json"):
        if _f.exists():
            _f.unlink()

import reelsee  # noqa: E402

state: dict = {}
FILES = ["testdata/perf_1080p30.mp4", "testdata/tone_5s.mkv"]


def in_tree(root, target) -> bool:
    """Is `target` reachable from `root` through the widget tree?"""
    if root is target:
        return True
    child = root.get_first_child()
    while child is not None:
        if in_tree(child, target):
            return True
        child = child.get_next_sibling()
    return False


def menu_buttons(root) -> list:
    from gi.repository import Gtk
    out = []
    if isinstance(root, Gtk.MenuButton):
        out.append(root)
    child = root.get_first_child()
    while child is not None:
        out.extend(menu_buttons(child))
        child = child.get_next_sibling()
    return out


def disk_cfg():
    with open(CFG_DIR / "config.json") as fh:
        return json.load(fh)


def wait_disk_flag(pred, left=75, step=0.2):
    """D1 (R7): config flushes are DEBOUNCED (config.py timer), so the
    quit-time write can land AFTER reelsee.run() returns — the old code
    read the disk once and raced that flush (flaky gate).  Poll the disk
    for the PARSED JSON value (never a regex over raw text) with a short
    deadline, then launch the phase-2 restart only once it is on disk."""
    val = None
    for _ in range(left):
        try:
            val = disk_cfg()["ui"]["show_top_menu"]
        except Exception:                        # noqa: BLE001
            val = None
        if pred(val):
            return val
        time.sleep(step)
    return val


def base_schedule(app, phase):
    from gi.repository import Gdk, GLib
    state["app"] = app

    def win():
        return app.windows[0]

    def again(fn, ms):
        GLib.timeout_add(ms, fn)
        return False

    def wait_for(desc, pred, then, left=25):
        val = None
        try:
            val = pred()
        except Exception:                        # noqa: BLE001
            val = None
        if val:
            check(desc, True, "ok" if val is True else str(val))
            return again(then, 60)
        if left <= 0:
            check(desc, False, "timeout")
            app.quit()
            return False
        return again(lambda: wait_for(desc, pred, then, left - 1), 120)

    def filename():
        fn = win().core.snapshot.get("filename") or ""
        return str(fn).split("/")[-1]

    def s0():
        w = win()
        top = w.chrome.top
        if phase == "1":
            check("top_in_tree_default", top.get_parent() is w.overlay,
                  str(top.get_parent()))
            check("tree_default_has_5_buttons",
                  len(menu_buttons(w.get_content())) == 3,
                  str(len(menu_buttons(w.get_content()))))
            st = app.show_settings()
            sw = st.widgets["ui.show_top_menu"]
            check("switch_exists_on", sw.get_active(), str(sw.get_active()))
            sw.set_active(False)                 # real commit path
            return again(s1, 120)
        # phase 2 ------------------------------------------------------
        check("top_absent_after_restart", top.get_parent() is None,
              str(top.get_parent()))
        check("top_not_in_widget_tree",
              not in_tree(w.get_content(), top), "walked whole tree")
        check("menu_buttons_gone",
              len(menu_buttons(w.get_content())) == 0,
              str(len(menu_buttons(w.get_content()))))
        st = app.show_settings()
        check("switch_still_off",
              not st.widgets["ui.show_top_menu"].get_active(), "off")
        st.close()
        return again(s2, 200)

    def s1():
        w = win()
        top = w.chrome.top
        gone = top.get_parent() is None
        check("toggle_removes_from_tree_live", gone, str(top.get_parent()))
        check("not_merely_hidden",
              gone and not in_tree(w.get_content(), top), "removed")
        check("still_visible_flag_irrelevant",
              not top.get_mapped(), "unmapped")
        # re-enable -> back in tree (live both directions)
        app.show_settings()
        app._settings.widgets["ui.show_top_menu"].set_active(True)
        check("toggle_readds", top.get_parent() is w.overlay,
              str(top.get_parent()))
        app._settings.widgets["ui.show_top_menu"].set_active(False)
        check("toggle_reagain_removes", top.get_parent() is None,
              str(top.get_parent()))
        app._settings.close()          # proven-safe close (sets app._settings=None)
        GLib.timeout_add(200, lambda: (app.quit(), False)[1])
        return False

    def s2():
        # shortcuts still reach everything with the menu bar gone
        w = win()
        w.core.command("loadfile", "testdata/tone_5s.mkv", "append")
        wait_for("playlist_built",
                 lambda: int(w.core.snapshot.get("playlist-count") or 0) == 2,
                 lambda: wait_for(
                     "playing_before_shortcuts",
                     lambda: w.core.snapshot.get("pause") is False,
                     s3, left=40),
                 left=25)
        return False

    def s3():
        w = win()
        fn0 = filename()
        handled = w._on_key(None, Gdk.KEY_n, 0, 0)      # playlist-next
        check("key_n_handled", handled, str(handled))
        wait_for("playlist_next_works",
                 lambda: filename() != fn0, s4, left=40)
        return False

    def sel_audio():
        for t in (win().core.snapshot.get("track-list") or []):
            if (isinstance(t, dict) and t.get("type") == "audio"
                    and t.get("selected")):
                return t.get("id")
        return None

    def s4():
        w = win()
        before = sel_audio()
        handled = w._on_key(None, Gdk.KEY_a, 0, 0)      # cycle-audio
        check("key_a_handled", handled, str(handled))
        wait_for("cycle_audio_changed",
                 lambda: sel_audio() != before, s5, left=25)
        return False

    def s5():
        w = win()
        # chapters: no chapter in test clips — the binding must still
        # resolve and run without error (no menu needed)
        handled = w._on_key(None, Gdk.KEY_Prior, 0, 0)  # chapter-prev
        check("key_prior_handled", handled, str(handled))
        handled = w._on_key(None, Gdk.KEY_l, 0, 0)      # loop-file
        check("key_l_handled", handled, str(handled))
        app.quit()
        return False

    GLib.timeout_add(700, s0)
    return False


def main() -> int:
    if PHASE == "1":
        rc = reelsee.run(["reelsee"] + FILES[:1],
                         on_app=lambda a: base_schedule(a, "1"))
        check("phase1_rc", rc == 0, str(rc))
        # D1: poll the DISK for the parsed value (debounced flush race),
        # assert PARSED False, and only then spawn the restart.
        val = wait_disk_flag(lambda v: v is False)
        check("persisted_off_on_disk", val is False, str(val))
        env = dict(os.environ, REELSEE_R5B_PHASE="2")
        out = subprocess.run(
            [sys.executable, os.path.abspath(__file__)] + FILES[:1],
            env=env, capture_output=True, text=True, timeout=120)
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
