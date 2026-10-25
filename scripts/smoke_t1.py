#!/usr/bin/python3
"""P11 T1 gate smoke (§7 feature matrix, live core).  Runs under
scripts/headless-smoke.sh.  Covers: playlist ops (append/next/prev/shuffle/
remove/move + m3u save→loadlist roundtrip), playlist/chapter/track popovers,
equalizer+geometry property roundtrip via observed properties, audio-only
toggle, track-change OSD, clipboard screenshot, and the R2-P3 URL phase:
orchestrated in TWO variants (yt-dlp ASSERTED present / PATH stripped to
an empty dir), direct-media targets must skip the resolver, page URLs must
settle inside playback.ytdlp_timeout + margin with an honest error toast
(no network anywhere: dead .invalid host only).
"""
import json
import os
import shutil
import subprocess
import sys
import time

from smoke_harness import ROOT, SCRATCH, check, finish

CFG_DIR = os.path.join(SCRATCH, "t1-cfg")
STATE_DIR = os.path.join(SCRATCH, "t1-state")
SHOT_DIR = os.path.join(SCRATCH, "t1-shots")
M3U = os.path.join(SCRATCH, "t1-playlist.m3u")
for _d in (CFG_DIR, STATE_DIR, SHOT_DIR):
    os.makedirs(_d, exist_ok=True)
os.environ["REELSEE_CONFIG_DIR"] = CFG_DIR
os.environ["REELSEE_STATE_DIR"] = STATE_DIR
for _f in ("config.json", "resume.json", "ab.json"):
    _p = os.path.join(CFG_DIR, _f)
    if os.path.exists(_p):
        os.unlink(_p)
    _p2 = os.path.join(STATE_DIR, _f)
    if os.path.exists(_p2):
        os.unlink(_p2)
if os.path.exists(M3U):
    os.unlink(M3U)

from gi.repository import GLib, Gdk, Gtk  # noqa: E402

import reelsee  # noqa: E402

state: dict = {}
VARIANT = os.environ.get("REELSEE_T1_VARIANT", "")


def win():
    return state["app"].windows[0]


def run_variants() -> int:
    """R2-P3 orchestrator: run the ENTIRE t1 gate twice.

      present — PATH augmented with ~/.local/bin and yt-dlp presence is
                ASSERTED (round-1's '13/13' was a sandbox-PATH artifact;
                a present variant that silently loses yt-dlp is void)
      hidden  — PATH set to an EMPTY scratch dir so no binary at all is
                findable; asserts the no-ytdlp path is green too.

Both children inherit Xvfb DISPLAY + vendor LD_LIBRARY_PATH from
headless-smoke.sh; both must exit 0.  The child prints
SMOKE_CHECK:resolve_elapsed=... with the margin against
playback.ytdlp_timeout + 3 s delivery allowance."""
    base = dict(os.environ)
    base.pop("REELSEE_YTDLP_BIN", None)
    user_bin = os.path.expanduser("~/.local/bin")
    empty_bin = os.path.join(SCRATCH, "t1-emptybin")
    os.makedirs(empty_bin, exist_ok=True)
    if shutil.which("yt-dlp", path=user_bin + os.pathsep
                    + base.get("PATH", "")) is None:
        check("ytdlp_available", False, "yt-dlp not installed anywhere")
        finish()
        return 1
    rcs = {}
    for name, path in (("present", user_bin + os.pathsep
                        + base.get("PATH", "")),
                       ("hidden", empty_bin)):
        env = dict(base, PATH=path, REELSEE_T1_VARIANT=name)
        print(f"--- t1 variant={name} ---", flush=True)
        rcs[name] = subprocess.run(
            [sys.executable, os.path.abspath(__file__)],
            env=env, cwd=str(ROOT)).returncode
        print(f"--- t1 variant={name} rc={rcs[name]} ---", flush=True)
    for name, rc in rcs.items():
        check(f"variant_{name}_rc0", rc == 0, f"rc={rc}")
    finish()
    return 0 if all(v == 0 for v in rcs.values()) else 1


def main() -> int:
    if not VARIANT:
        return run_variants()
    which = shutil.which("yt-dlp")
    if VARIANT == "present":
        # the whole point of the variant: yt-dlp MUST be visible here
        check("ytdlp_visible", which is not None, which or "MISSING")
    elif VARIANT == "hidden":
        check("ytdlp_hidden", which is None, which or "absent")
    rc = reelsee.run(["reelsee", "testdata/smoke_360p.mp4"],
                     on_app=probe_schedule)
    finish()
    return rc


def probe_schedule(app):
    state["app"] = app

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

    def wait_for(desc, pred, then, left=30):
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

    def loaded(pred_name):
        return lambda: (pred_name in str(win().core.snapshot.get("filename")
                                         or "")) or None

    # ---- steps -----------------------------------------------------------
    def t0():
        w = win()
        w._load_path("testdata/tone_5s.mkv", "append")
        wait_for("append_playlist",
                 lambda: int(win().core.snapshot.get("playlist-count")
                             or 0) == 2, guard(t1))

    def t1():
        w = win()
        w._actions["playlist-next"](None)
        wait_for("playlist_next", loaded("tone_5s"), guard(t2))

    def t2():
        win()._actions["playlist-prev"](None)
        wait_for("playlist_prev", loaded("smoke_360p"), guard(t3))

    def t3():
        # m3u save (async) → wipe playlist → loadlist roundtrip
        import reelsee_io as IO
        # mpv resolves relative m3u entries against the m3u's directory
        urls = [u if IO.is_url(u := str(r.get("filename")))
                else os.path.abspath(u) for r in win()._pl_rows()]
        parent = os.path.dirname(M3U)

        def then_write(ok):
            if not ok:                    # missing dir: create, then retry
                try:
                    os.makedirs(parent, exist_ok=True)
                except OSError:
                    pass
            IO.save_text_async(M3U, IO.build_m3u(urls),
                               guard(lambda ok2=None: GLib.timeout_add(
                                   150, guard(t4))))

        IO.save_text_async(M3U, IO.build_m3u(urls), then_write)

    def t4():
        with open(M3U) as fh:
            lines = fh.read().splitlines()
        check("m3u_saved", len(lines) == 3 and lines[0] == "#EXTM3U",
              f"lines={len(lines)}")
        win().core.command("playlist-clear")
        # modern mpv keeps the currently playing entry: count drops to <=1
        wait_for("playlist_cleared",
                 lambda: int(win().core.snapshot.get("playlist-count")
                             or 9) <= 1, guard(t5))

    def t5():
        win().core.command("loadlist", M3U, "replace")
        wait_for("loadlist_roundtrip",
                 lambda: int(win().core.snapshot.get("playlist-count")
                             or 0) == 2, guard(t6))

    def t6():
        w = win()
        w.core.command("playlist-shuffle")
        wait_for("shuffle_keeps_count",
                 lambda: int(w.core.snapshot.get("playlist-count")
                             or 0) == 2, guard(t7))

    def t7():
        win()._pl_remove_current()
        wait_for("remove_current",
                 lambda: int(win().core.snapshot.get("playlist-count")
                             or 9) == 1, guard(t75))

    def t75():
        # loadlist-replace left nothing playing; start the remaining entry
        # so track-list populates for the popover/OSD checks ahead.
        win().core.command("set", "playlist-index", "0")  # jumps + plays
        wait_for("replay_started",
                 lambda: (win().core.snapshot.get("filename")
                          not in (None, "")), guard(t8))

    def t8():
        # R8-A tree: NO top bar / MenuButtons exist any more — the
        # playlist/chapters/tracks lists live in snapshot-fed popovers
        # parented to the video area, and the same features are entries of
        # the right-click menu model.
        w = win()

        def no_menubutton(node):
            if isinstance(node, Gtk.MenuButton):
                return False
            c = node.get_first_child()
            while c is not None:
                if not no_menubutton(c):
                    return False
                c = c.get_next_sibling()
            return True

        check("no_top_bar_menubuttons", no_menubutton(w), "tree walk")
        check("popovers_wired",
              all(p is not None and p.get_parent() is not None
                  for p in (w._pl_pop, w._ch_pop, w._tr_pop)), "ok")
        GLib.timeout_add(200, guard(t9))

    def nrows(lb):
        n = 0
        while lb.get_row_at_index(n) is not None:
            n += 1
        return n

    def t9():
        w = win()
        w._sync_menus()
        check("pl_popover_rows", nrows(w._pl_list) == 1,
              f"rows={nrows(w._pl_list)}")
        check("ch_popover_placeholder", nrows(w._ch_list) == 1,
              f"rows={nrows(w._ch_list)}")
        check("tr_popover_rows", nrows(w._tr_list) >= 4,
              f"rows={nrows(w._tr_list)}")
        # observe the props we are about to poke (main thread never reads)
        import mpvlib as M
        w.core.observe({
            "brightness": M.MPV_FORMAT_DOUBLE,
            "contrast": M.MPV_FORMAT_DOUBLE,
            "video-zoom": M.MPV_FORMAT_DOUBLE,
            "video-aspect-override": M.MPV_FORMAT_STRING,
            "sub-scale": M.MPV_FORMAT_DOUBLE,
        })
        w.core.set_property("brightness", 20.0)
        w.core.set_property("contrast", -10.0)
        wait_for("eq_roundtrip",
                 lambda: (float(w.core.snapshot.get("brightness") or 0)
                          == 20.0
                          and float(w.core.snapshot.get("contrast") or 0)
                          == -10.0) or None, guard(t10))

    def t10():
        w = win()
        w.core.set_property("video-zoom", 0.5)
        w.core.set_property("video-aspect-override", "16:9")

        def geom():
            v = w.core.snapshot.get("video-aspect-override")
            try:
                asp = abs(float(v) - 16 / 9) < 0.01   # mpv reads "1.777778"
            except (TypeError, ValueError):
                asp = v == "16:9"
            return (float(w.core.snapshot.get("video-zoom") or 0) == 0.5
                    and asp) or None
        wait_for("geometry_roundtrip", geom, guard(t11))

    def t11():
        # subtitle style live-apply via the REAL production entry point
        w = win()
        import settings_ui
        settings_ui.apply_live(state["app"], "subtitles.scale", 1.75)
        wait_for("sub_style_live",
                 lambda: float(w.core.snapshot.get("sub-scale") or 0)
                 == 1.75 or None, guard(t12))

    def t12():
        # audio-only toggle both ways (track OSD fires on the first)
        w = win()
        state["osd_hits"] = []
        real = w.chrome.show_osd

        def spy(text):
            state["osd_hits"].append(text)
            return real(text)
        w.chrome.show_osd = spy
        w.on_setting_changed("playback.video_off", True)
        GLib.timeout_add(500, guard(t13))

    def t13():
        hits = [h for h in state["osd_hits"] if h.startswith("video track")]
        check("track_off_osd", bool(hits), ";".join(state["osd_hits"][:4]))
        win().on_setting_changed("playback.video_off", False)
        GLib.timeout_add(400, guard(t14))

    def t14():
        # screenshot → clipboard: demand the SUCCESS toast (real copy),
        # not merely that the callback fired
        w = win()
        w.cfg.set("screenshot.directory", SHOT_DIR)
        w.cfg.set("screenshot.format", "png")
        w.cfg.set("ui.screenshot_clipboard", True)
        w._push_all_settings()
        seen = {}
        toasts = []
        real_toast = w.chrome.show_toast

        def spy_toast(text, *a):
            toasts.append(text)
            return real_toast(text, *a)
        w.chrome.show_toast = spy_toast

        def spy_bytes(src, res, _u=None, orig=w._on_shot_bytes):
            seen["called"] = True
            return orig(src, res)
        w._on_shot_bytes = spy_bytes
        w._actions["screenshot"](None)
        wait_for("clipboard_copy",
                 lambda: (seen.get("called")
                          and any("copied to clipboard" in t.lower()
                                  for t in toasts)) or None,
                 guard(t15), left=40)

    def t15():
        files = [f for f in os.listdir(SHOT_DIR) if f.startswith("mpv-shot")]
        check("screenshot_file", bool(files), files[0] if files else "none")
        # R2-P3 (c): direct-media URL — the resolver must NOT be started
        # even with yt-dlp installed (mpv opens .mp4/.m3u8-style targets
        # natively; yt-dlp there is pure latency).
        w = win()
        w.open_target("https://reelsee-nonexistent.invalid/x.mp4")
        check("url_direct_media_skip",
              w._resolving is None
              and not (w._url_resolver and w._url_resolver.busy),
              "resolver-not-started")
        GLib.timeout_add(700, guard(t16))

    def t16():
        # Page URL (NOT direct media) → resolver engages and must SETTLE
        # within playback.ytdlp_timeout + delivery margin, in BOTH
        # variants: present = yt-dlp really runs and fails honestly on the
        # dead host; hidden = "not found" is reported the same way.
        w = win()
        state["toasts"] = []
        real_toast = w.chrome.show_toast

        def spy(text, *a):
            state["toasts"].append(text)
            return real_toast(text, *a)
        w.chrome.show_toast = spy
        url = "https://reelsee-nonexistent.invalid/watch?v=deadbeef"
        # R2-P3 (d): Escape CANCELS the in-flight resolve (keymap "exit"
        # must not see the press) and nothing loads afterwards.
        w.open_target(url)
        check("url_resolver_engaged", w._resolving is not None,
              repr(w._resolving))
        handled = w._on_key(None, Gdk.KEY_Escape, 0, 0)
        check("url_cancel_key_swallowed", handled is True, repr(handled))
        check("url_cancel_state_restored",
              w._resolving is None
              and "URL resolve cancelled" in state["toasts"],
              "|".join(state["toasts"])[:120])
        state["toasts"].clear()
        GLib.timeout_add(1500, guard(t16b))

    def t16b():
        w = win()
        # the cancelled worker finished by now — its stale result must have
        # been dropped (generation token), never delivered as a load
        check("url_cancel_drops_stale",
              w._url_resolver.poll() is None
              and "watch?v" not in str(w.core.snapshot.get("filename") or ""),
              "stale-dropped")
        state["t0"] = time.monotonic()
        w.open_target("https://reelsee-nonexistent.invalid/watch?v=deadbeef")
        wait_for("url_resolve_settled",
                 lambda: (w._resolving is None
                          and w._url_resolver is not None
                          and not w._url_resolver.busy) or None,
                 guard(t16c), left=80)

    def t16c():
        w = win()
        elapsed = time.monotonic() - state["t0"]
        budget = float(w.cfg.get("playback.ytdlp_timeout", 5.0)) + 3.0
        check("resolve_elapsed", elapsed <= budget,
              f"{elapsed:.2f}s<=budget margin={budget - elapsed:.2f}s")
        joined = " | ".join(state["toasts"])
        # R2-P3 (e): failure is an ERROR TOAST with the resolver message,
        # not a silent return to idle.
        check("resolve_error_toast", "URL resolve failed:" in joined,
              (joined[:140] or "NO TOAST"))
        wait_for("url_error_survived",
                 lambda: bool(win().core.snapshot.get("idle-active"))
                 or None, guard(t17), left=80)

    def t17():
        check("window_alive_after_url", win() in state["app"].windows, "ok")
        state["app"].quit()
        return False

    def t_wait_loaded():
        wait_for("first_file_loaded", loaded("smoke_360p"), guard(t0))

    GLib.timeout_add(700, guard(t_wait_loaded))
    return False


if __name__ == "__main__":
    sys.exit(main())
