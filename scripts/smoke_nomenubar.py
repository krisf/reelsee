#!/usr/bin/python3
"""R8-A gate smoke: the in-video top menu bar is GONE, entirely (brief A).

Runs under Xvfb with a FRESH config dir pre-seeded with an OLD config
carrying the deleted key {"ui": {"show_top_menu": false}} — the exact
upgrade scenario from brief §A.3: it must load cleanly, and change
NOTHING any more.  Gates (brief §C token names verbatim):

  * config_loads_with_dead_key        — old config boots, no exception;
  * dead_key_gone                     — no default, no binding left;
  * dead_key_loads_silently           — zero log records of the whole boot
      mention the dead key, and the config
      namespace logged no WARNING (the battery
      separately fails on any Traceback);
  * top_bar_absent                    — NO bar widget for ANY config: EVERY
      descendant of the window is walked — no
      MenuButton, no chrome.top, no home/
      audio/subs/playlist/chapters/tracks
      button anywhere; the overlay holds ONLY
      the kept children; bottom bar survives;
  * window_title_shows_filename       — filename visibility moved to the
      window title, where the bar's own
      label used to be;
  * settings_model_has_no_dead_setting— the settings window has no row;
  * menu_has_all_7_entries_in_order   — media-title header + Audio, Subtitles,
      Repeat, Playlist, Chapters, Tracks,
      in that order;
  * chapters_present_insensitive      — on a chapter-less file the Chapters
      row is present but bound to ctx.noop
      (disabled, not hidden);
  * menu_entries_wired /
    menu_leaves_carry_ctx_actions     — activating the ctx actions lands the
      SAME core.command calls, and every leaf row
      of the popover carries a ctx.* action;
  * audio_off_hits_mpv /
    audio_pick_hits_mpv               — Audio, proven INSIDE mpv: mpv's own
      track-list loses `selected` to aid=no
      and gets it back from aid=<id>;
  * repeat_cycles_mpv                 — radio off→all→one→off drives the real
      mpv loop properties;
  * chapters_entry_fires, tracks_entry_fires,
    subtitles_entry_fires, playlist_entry_fires
    (each with its *_entries_live /
     *_command_issued step)           — the four entries a bare clip cannot
      prove, fired the honest way: the
      (action, target) pair comes OUT of the
      model the popover paints, is activated,
      and mpv confirms in its own state —
      `chapter` moved, `selected` landed on
      the picked audio and sub track, and
      `current` + media-title followed the
      jumped-to playlist item.  That media is
      generated into scratch/ (320x240, 3
      chapters, 2 audio tracks, 1 subtitle)
      — never into testdata/;
  * opened_menu_has_all_7_entries_in_order /
    opened_menu_entries_wired        — with the popover OPEN, the seven
      entries are read from the model THIS popover paints (its
      `get_menu_model()`), in order, every leaf carrying a ctx.* action;
  * menu_opens / menu_dismisses /
    window_alive_after_dismiss        — a real button-3 press paints the
      popover, a real XTEST Escape closes it,
      the window is still alive afterwards;
  * x_window_mapped                   — the X11 window is mapped throughout.

Filename visibility moves to the window title: checked after load.
"""
import json
import logging
import os
import subprocess
import sys

from smoke_harness import SCRATCH, check, finish

CFG_DIR = SCRATCH / "nomenubar-cfg"
STATE_DIR = SCRATCH / "nomenubar-state"
for _d in (CFG_DIR, STATE_DIR):
    _d.mkdir(parents=True, exist_ok=True)
CFG_FILE = CFG_DIR / "config.json"
for _f in (CFG_FILE, STATE_DIR / "resume.json"):
    if _f.exists():
        _f.unlink()
# an OLD (pre-R8) config: the deleted key is present and must be inert
with open(CFG_FILE, "w") as fh:
    json.dump({"ui": {"show_top_menu": False}}, fh)
os.environ["REELSEE_CONFIG_DIR"] = str(CFG_DIR)
os.environ["REELSEE_STATE_DIR"] = str(STATE_DIR)

import reelsee  # noqa: E402
import ctxmenu  # noqa: E402

FILES = ["testdata/perf_1080p30.mp4"]
DEAD_LABELS = {"home", "audio", "subs", "playlist", "chapters", "tracks"}
state: dict = {}

# Generated into scratch/ (NEVER into testdata/, per the round rule): three
# chapters, TWO audio tracks and a subtitle track.  Chapters, Tracks and
# Subtitles are the menu entries a chapter-less single-track clip can only
# render insensitive — the model has nothing to offer there — so proving them
# FIRING needs a file that actually carries them.
CHAP = SCRATCH / "chapters_320x240.mkv"


def ensure_chaptered_clip() -> bool:
    """320x240, 4 s, 3 chapters, 2 audio tracks (eng + deu), 1 subtitle.
    Idempotent."""
    if CHAP.exists() and CHAP.stat().st_size > 1024:
        return True
    caps = SCRATCH / "chapters.srt"
    caps.write_text("1\n00:00:00,200 --> 00:00:01,200\nfirst line\n\n"
                    "2\n00:00:01,600 --> 00:00:03,600\nsecond line\n")
    meta = SCRATCH / "chapters.ffmeta"
    meta.write_text(
        ";FFMETADATA1\n"
        "[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\nEND=1500\ntitle=one\n"
        "[CHAPTER]\nTIMEBASE=1/1000\nSTART=1500\nEND=3000\ntitle=two\n"
        "[CHAPTER]\nTIMEBASE=1/1000\nSTART=3000\nEND=4000\ntitle=three\n")
    try:
        subprocess.run(
            ["/usr/bin/ffmpeg", "-v", "error", "-y",
             "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=10",
             "-f", "lavfi", "-i", "sine=frequency=440:duration=4",
             "-f", "lavfi", "-i", "sine=frequency=880:duration=4",
             "-f", "srt", "-i", str(caps),
             "-i", str(meta),
             "-map", "0:v", "-map", "1:a", "-map", "2:a", "-map", "3:s",
             "-t", "4", "-c:v", "libx264", "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-c:s", "subrip",
             "-metadata:s:a:0", "language=eng",
             "-metadata:s:a:1", "language=deu",
             "-metadata:s:s:0", "language=eng",
             "-map_metadata", "4", str(CHAP)],
            check=True, timeout=120, capture_output=True)
    except Exception as exc:                           # noqa: BLE001
        print("SMOKE_NOTE:chaptered_clip_failed=%r" % (exc,), flush=True)
        return False
    return True


class _LogCapture(logging.Handler):
    """Every record the app logs during this boot, so \"loads silently\" can
    be asserted instead of assumed (the battery separately fails on any
    Traceback in the log)."""

    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


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

    def xmapped():
        # same discovery as smoke_r7_decode.xfind (name match OR big
        # mapped window) — proven plumbing, no bespoke variant.
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

    def xkey(keysym):
        from Xlib import X, XK
        from Xlib.ext import xtest
        d = state["xd"]
        kc = d.keysym_to_keycode(getattr(XK, keysym))
        xtest.fake_input(d, X.KeyPress, kc)
        d.sync()
        xtest.fake_input(d, X.KeyRelease, kc)
        d.sync()

    # ------------------------------------------------------ tree walk
    def walk(node, out=None):
        out = out if out is not None else []
        out.append(node)
        c = node.get_first_child()
        while c is not None:
            walk(c, out)
            c = c.get_next_sibling()
        return out

    # ------------------------------------------------------ assertions
    def s_structural():
        import config as C
        import settings_ui
        w = win()
        raw = json.load(open(CFG_FILE))
        check("config_loads_with_dead_key",
              raw.get("ui", {}).get("show_top_menu") is False
              and w.get_realized(),
              f"raw={raw} realized={w.get_realized()}")
        check("dead_key_gone",
              "show_top_menu" not in C.DEFAULTS.get("ui", {})
              and "ui.show_top_menu" not in settings_ui.binding_keys(),
              "no default + no binding")
        # SILENTLY: not one log record during the whole boot mentions the
        # dead key, and the config namespace logged nothing at WARNING+.
        cap = state.get("cap")
        noisy = [] if cap is None else [
            r.name + ": " + r.getMessage() for r in cap.records
            if "show_top_menu" in r.getMessage()
            or ("config" in r.name and r.levelno >= logging.WARNING)]
        check("dead_key_loads_silently", not noisy,
              "records=%s total=%d" % (noisy[:3],
                                       0 if cap is None else len(cap.records)))

        nodes = walk(w)
        mb = [n for n in nodes if isinstance(n, Gtk.MenuButton)]
        dead_btns = []
        for n in nodes:
            if isinstance(n, Gtk.Button):
                t = ""
                ch = n.get_first_child()
                if isinstance(ch, Gtk.Label):
                    t = ch.get_text()
                if t.lower() in DEAD_LABELS:
                    dead_btns.append(t)
        named = [n for n in nodes if n.get_css_name() == "reelsee-top"]
        chrome_gone = all(not hasattr(w.chrome, a)
                          for a in ("top", "title_label", "btn_audio",
                                    "btn_sub"))
        kids = 0
        k = w.overlay.get_first_child()
        while k is not None:
            kids += 1
            k = k.get_next_sibling()
        check("top_bar_absent",
              not mb and not dead_btns and not named and chrome_gone
              and kids == 5,
              f"menubuttons={len(mb)} dead={dead_btns} named={len(named)} "
              f"chrome_attrs_gone={chrome_gone} overlay_kids={kids}")
        check("bottom_bar_survives",
              w.chrome.bottom.get_parent() is not None, "kept")
        # load a file: everything below needs live snapshot data
        w._load_path("testdata/perf_1080p30.mp4")
        return again(guard(lambda: wait_for(
            "video_loaded",
            lambda: win().core.snapshot.get("duration"),
            guard(s_title))), 80)

    def s_title():
        import config as C
        w = win()
        check("window_title_shows_filename",
              "perf_1080p30" in (w.get_title() or ""),
              str(w.get_title()))
        st = app.show_settings()
        rows = list(st.widgets)
        check("settings_model_has_no_dead_setting",
              "ui.show_top_menu" not in rows
              and all(k in rows for k in C.schema_leaf_keys()),
              f"rows={len(rows)}")
        st.close()
        return again(guard(s_menu_order), 250)

    def s_menu_order():
        w = win()
        rows = ctxmenu.model_dump(ctxmenu.build(w.core.snapshot))
        labels = [r[0] for r in rows]
        check("menu_has_all_7_entries_in_order",
              labels == ["perf_1080p30.mp4", "Audio", "Subtitles", "Repeat",
                         "Playlist", "Chapters", "Tracks"], str(labels))
        ch = [r for r in rows if r[0] == "Chapters"]
        check("chapters_present_insensitive",
              len(ch) == 1 and ch[0][1] == "ctx.noop", str(ch))
        return again(guard(s_wired), 80)

    def s_wired():
        w = win()
        calls = []
        orig = w.core.command

        def spy(name, *a):
            calls.append((name,) + tuple(a))
            return orig(name, *a)
        w.core.command = spy
        state["orig_command"] = (orig,)
        w.activate_action("ctx.audio-off")
        w.activate_action("ctx.sub-off")
        w.activate_action("ctx.pick-audio", GLib.Variant("i", 1))
        w.activate_action("ctx.goto-playlist", GLib.Variant("i", 0))
        w.core.command = orig
        need = [("set", "aid", "no"), ("set", "sid", "no"),
                ("set", "aid", "1"), ("set", "playlist-index", "0")]
        check("menu_entries_wired",
              all(n in calls for n in need),
              "seen=" + repr(calls))
        leaves = ctxmenu.model_dump(ctxmenu.build(w.core.snapshot))

        def all_wired(rows):
            for r in rows:
                if r[3]:
                    if not all_wired(r[3]):
                        return False
                elif r[1] is None or not str(r[1]).startswith("ctx."):
                    return False
            return True
        check("menu_leaves_carry_ctx_actions", all_wired(leaves), "ok")
        # And the Audio entry is proven all the way INTO mpv, not just into
        # core.command: with aid=no every audio track loses `selected` in
        # the very track-list the menu builds itself from.
        w.activate_action("ctx.audio-off")
        return again(guard(lambda: wait_for(
            "audio_off_hits_mpv",
            lambda: bool(tracks("audio"))
            and not any(t.get("selected") for t in tracks("audio")),
            guard(s_audio_back))), 30)

    def s_audio_back():
        """... and picking the track from the menu puts it back (the same
        entry the Audio submenu offers a real track)."""
        w = win()
        tid = next((t.get("id") for t in tracks("audio")), None)
        check("audio_track_id_found", tid is not None, "tid=%s" % (tid,))
        w.activate_action("ctx.pick-audio", GLib.Variant("i", int(tid)))
        return again(guard(lambda: wait_for(
            "audio_pick_hits_mpv",
            lambda: any(t.get("selected") for t in tracks("audio")),
            guard(s_repeat))), 30)

    def s_repeat():
        w = win()
        snap = w.core.snapshot

        def lf():
            return snap.get("loop-file")

        def lp():
            return snap.get("loop-playlist")
        w.activate_action("ctx.repeat", GLib.Variant("s", "all"))
        check("repeat_action_state_all",
              w._ctx_repeat.get_state().unpack() == "all", "ok")
        return again(guard(lambda: wait_for(
            "repeat_all_hits_mpv", lambda: lp() == "inf",
            guard(s_repeat_one))), 20)

    def s_repeat_one():
        w = win()
        w.activate_action("ctx.repeat", GLib.Variant("s", "one"))

        def snap():
            return win().core.snapshot

        return again(guard(lambda: wait_for(
            "repeat_one_hits_mpv",
            lambda: snap().get("loop-file") == "inf"
            and snap().get("loop-playlist") == "no",
            guard(s_repeat_off))), 20)

    def s_repeat_off():
        w = win()
        w.activate_action("ctx.repeat", GLib.Variant("s", "off"))

        def snap():
            return win().core.snapshot

        return again(guard(lambda: wait_for(
            "repeat_cycles_mpv",
            lambda: snap().get("loop-file") == "no"
            and snap().get("loop-playlist") == "no",
            guard(s_chaptered_load))), 20)

    def tracks(kind=None):
        tl = win().core.snapshot.get("track-list") or []
        out = [t for t in tl if isinstance(t, dict)]
        return out if kind is None else [t for t in out
                                         if t.get("type") == kind]

    def live_rows(rows, label):
        """(action, target) of every FIREABLE entry under a labelled
        submenu, read straight out of the model the popover paints — a
        disabled entry is exactly one bound to ctx.noop, so what survives
        here is what a user can actually click.  Nothing is hardcoded: the
        smoke fires what the menu itself says it fires."""
        for r in rows:
            if str(r[0]).startswith(label):
                return [(a, t) for (_l, a, t, _s) in (r[3] or [])
                        if a and a.startswith("ctx.") and a != ctxmenu.NOOP]
        return []

    def spy_on():
        """Capture core.command calls while STILL forwarding them — the
        command has to reach mpv for the wait that follows to end."""
        w = win()
        calls = []
        orig = w.core.command

        def spy(name, *a):
            calls.append((name,) + tuple(a))
            return orig(name, *a)
        w.core.command = spy
        return calls, orig

    def s_chaptered_load():
        win()._load_path(str(CHAP))
        return again(guard(lambda: wait_for(
            "chaptered_media_loaded",
            lambda: len(win().core.snapshot.get("chapter-list") or []) >= 3,
            guard(s_chapters_fire))), 40)

    def s_chapters_fire():
        w = win()
        rows = ctxmenu.model_dump(ctxmenu.build(w.core.snapshot))
        ch = live_rows(rows, "Chapters")
        check("chapters_entries_live",
              len(ch) >= 3 and all(a == "ctx.goto-chapter" for a, _t in ch)
              and all(isinstance(t, int) for _a, t in ch),
              "entries=%s" % (ch,))
        calls, orig = spy_on()
        act, want = ch[1]                       # chapter "two", as clicked
        want = int(want)
        # model_dump unpacks the target to a plain int; activation needs the
        # typed variant the action's parameter_type declares ("i").
        fired = w.activate_action(act, GLib.Variant("i", want))
        w.core.command = orig
        check("chapters_command_issued",
              bool(fired) and ("set", "chapter", str(want)) in calls,
              "activate=%s %s target=%s calls=%s"
              % (bool(fired), act, want, calls[:4]))
        state["want_chapter"] = want
        return again(guard(lambda: wait_for(
            # ground truth: mpv's own chapter index moved
            "chapters_entry_fires",
            lambda: win().core.snapshot.get("chapter") == want,
            guard(s_tracks_fire))), 40)

    def s_tracks_fire():
        w = win()
        rows = ctxmenu.model_dump(ctxmenu.build(w.core.snapshot))
        alltr = live_rows(rows, "Tracks")
        tr = [(a, t) for a, t in alltr if a == "ctx.pick-audio"
              and t is not None]
        check("tracks_entries_live", bool(tr), "entries=%s" % (alltr,))
        calls, orig = spy_on()
        act, want = tr[0]                       # the un-selected [deu] #2
        want = int(want)
        fired = w.activate_action(act, GLib.Variant("i", want))
        w.core.command = orig
        check("tracks_command_issued",
              bool(fired) and ("set", "aid", str(want)) in calls,
              "activate=%s %s target=%s calls=%s"
              % (bool(fired), act, want, calls[:4]))
        return again(guard(lambda: wait_for(
            # ground truth: mpv moved `selected` onto that track id
            "tracks_entry_fires",
            lambda: any(t.get("id") == want and t.get("selected")
                        for t in tracks("audio")),
            guard(s_subs_fire))), 40)

    def s_subs_fire():
        """Subtitles: the identical proof on the subtitle track the clip
        carries — the (action, target) pair out of the menu model, fired,
        mpv's own `selected` flag moved onto it."""
        w = win()
        rows = ctxmenu.model_dump(ctxmenu.build(w.core.snapshot))
        subs = [(a, t) for a, t in live_rows(rows, "Subtitles")
                if a == "ctx.pick-sub" and t is not None]
        check("subtitles_entries_live", bool(subs), "entries=%s" % (subs,))
        calls, orig = spy_on()
        act, want = subs[0]
        want = int(want)
        fired = w.activate_action(act, GLib.Variant("i", want))
        w.core.command = orig
        check("subtitles_command_issued",
              bool(fired) and ("set", "sid", str(want)) in calls,
              "activate=%s %s target=%s calls=%s"
              % (bool(fired), act, want, calls[:4]))
        return again(guard(lambda: wait_for(
            "subtitles_entry_fires",
            lambda: any(t.get("id") == want and t.get("selected")
                        for t in tracks("sub")),
            guard(s_playlist_arm))), 40)

    def s_playlist_arm():
        """Playlist: mpv gains a second item (the data the Playlist submenu
        reads), then the entry that jumps to it is fired."""
        win().core.command("loadfile", FILES[0], "append")
        return again(guard(lambda: wait_for(
            "playlist_has_two_items",
            lambda: len(win().core.snapshot.get("playlist") or []) >= 2,
            guard(s_playlist_fire))), 40)

    def s_playlist_fire():
        w = win()
        rows = ctxmenu.model_dump(ctxmenu.build(w.core.snapshot))
        pl = [(a, t) for a, t in live_rows(rows, "Playlist")
              if a == "ctx.goto-playlist" and t is not None]
        check("playlist_entries_live", len(pl) >= 2, "entries=%s" % (pl,))
        calls, orig = spy_on()
        act, want = pl[1]                       # the appended item, as clicked
        want = int(want)
        fired = w.activate_action(act, GLib.Variant("i", want))
        w.core.command = orig
        check("playlist_command_issued",
              bool(fired)
              and ("set", "playlist-index", str(want)) in calls,
              "activate=%s %s calls=%s" % (bool(fired), act, calls[:4]))

        def now_playing():
            pl = win().core.snapshot.get("playlist") or []
            if want >= len(pl) or not pl[want].get("current"):
                return False
            return (os.path.basename(
                str(win().core.snapshot.get("media-title")))
                == os.path.basename(FILES[0]))
        return again(guard(lambda: wait_for(
            # ground truth: mpv moved `current` AND the title followed
            "playlist_entry_fires", now_playing, guard(s_dismiss))), 40)

    def s_dismiss():
        w = win()
        w._open_context_menu(140, 110)
        return again(guard(lambda: wait_for(
            "menu_opens", lambda: w._ctx_pop.get_visible(),
            guard(s_opened_menu))), 150)

    def s_opened_menu():
        """The brief's wording, literally: the menu is OPEN (button press
        path, popover visible right now) and the seven entries are read out
        of the model THIS popover paints — `get_menu_model()`, not a freshly
        built copy — in paint order, each one carrying a real ctx.* action.
        A disabled entry is one bound to ctx.noop: present but inert, which
        is the design, never a dangling action."""
        w = win()
        model = w._ctx_pop.get_menu_model()
        check("opened_menu_is_live_popover_model",
              w._ctx_pop.get_visible() and model is not None,
              "visible=%s model=%s" % (w._ctx_pop.get_visible(),
                                       type(model).__name__))
        rows = ctxmenu.model_dump(model)
        labels = [r[0] for r in rows]
        check("opened_menu_has_all_7_entries_in_order",
              labels == ["perf_1080p30.mp4", "Audio", "Subtitles", "Repeat",
                         "Playlist", "Chapters", "Tracks"], str(labels))

        def leaves(rs):
            out = []
            for r in rs:
                out.extend(leaves(r[3]) if r[3]
                           else [r] if r[1] else [])
            return out
        lf = leaves(rows)
        inert = sorted({r[1] for r in lf if r[1] == ctxmenu.NOOP})
        check("opened_menu_entries_wired",
              len(lf) >= 9 and all(str(r[1]).startswith("ctx.")
                                    for r in lf),
              "leaves=%d actions=%s inert_rows=%s"
              % (len(lf), sorted({r[1] for r in lf})[:6], inert))
        return again(guard(s_escape), 60)

    def s_escape():
        w = win()
        w.present()
        state["xd"].sync()
        xkey("XK_Escape")
        return again(guard(lambda: wait_for(
            "menu_dismisses", lambda: not w._ctx_pop.get_visible(),
            guard(s_done))), 30)

    def s_done():
        check("window_alive_after_dismiss", win().get_realized(), "True")
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
        if not xmapped():
            if left <= 0:
                check("x_window_mapped", False, "no mapped REELSEE window")
                app.quit()
                return False
            return again(lambda: start(left - 1), 300)
        check("x_window_mapped", True, "mapped")
        w.present()
        state["xd"].sync()
        return again(guard(s_structural), 600)

    GLib.timeout_add(300, guard(start))


# Gtk is imported lazily (after reelsee.run bootstraps the gi versions)
def _gtk():
    import gi
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk
    return Gtk


Gtk = _gtk()


def main() -> int:
    if not ensure_chaptered_clip():
        check("chaptered_clip_generated", False, "ffmpeg unavailable")
    cap = _LogCapture()
    cap.setLevel(logging.DEBUG)
    logging.getLogger("reelsee").addHandler(cap)
    state["cap"] = cap
    rc = reelsee.run(["reelsee"] + FILES[:1], on_app=base_schedule)
    check("rc", rc == 0, str(rc))
    finish()
    return 0


if __name__ == "__main__":
    sys.exit(main())
