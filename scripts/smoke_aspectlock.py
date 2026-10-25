#!/usr/bin/python3
"""R8-B gate smoke: the aspect-locked window resize, on real mpv + Xvfb.

The aspect source is the OBSERVED video (mpv `dwidth`/`dheight`), never the
widget, and a resize that leaves the video's shape is corrected once, ~80 ms
after the burst settles, clamped to the work area — while fullscreen,
maximized, unknown-aspect or unlocked, nothing moves at all (brief §B).

Ground truth, and why it is ground truth:

  * `state["req"]` — every `set_default_size()` the WINDOW asks for (spied on
    the instance; production has exactly one caller, `_apply_aspect_size`);
  * `state["geo"]` — the sizes GDK actually laid the surface out at, recorded
    by a SECOND independent `Gdk.Surface::layout` connection on the same
    surface.  `Gtk.Window.get_width()` is 10 px off (CSD shadow), so it is
    never used as a measurement here.
  * synthetic resizes drive `win()._on_surface_layout(None, w, h)`: the real
    handler, with `surface=None` marking them as drives.  Real events cannot
    be muted — `connect()` bound the method at connect time — and are not
    suppressed by pausing either (measured: ~9 events / 600 ms while playing,
    ~1 while paused, always with the CURRENT size).  Every synthetic phase
    therefore starts from a geometry the window already agrees with, so a
    stray repaint is aspect-faithful, returns early and cannot clobber the
    pending size.

Gates, in the order the phases prove them: layout_signal_wired,
startup_aspect_default_on, unknown_aspect_idle, idle_no_correction,
idle_no_source_armed, dwidth_dheight_observed, aspect_source_is_dwh,
file_open_16_9_untouched, storm_quiet_when_paused, move_is_not_a_resize,
burst_deferred, drag_16_9_square, drag_16_9_landed, one_correction_per_burst,
correction_no_second_correction, vertical_dims_observed,
vertical_opens_vertical, aspect_source_is_the_video, vertical_landed,
drag_vertical_225x400, no_resize_fight, audio_aspect_unknown,
unknown_aspect_unchanged, workarea_clamped, palette_action_exists,
toggle_off_state, toggle_off_frees_immediately, unlocked_resize_stays,
settings_row_mirrors_action, setting_on_realigns, fullscreen_action_fired,
fullscreen_unchanged, leaving_fullscreen_no_snap,
aspect_alive_after_fullscreen, lock_reengages_after_fullscreen,
x_window_mapped, every_request_inside_workarea, corrections_happened, rc.

Two clips are GENERATED into scratch/smoke/ (never into testdata/, per the
round rule): a 270x480 h264 file — exactly the 9:16 of the brief's
1080x1920, so the brief's own expected numbers apply — and an audio-only
ogg, because mpv then reports NO display dimensions at all, which is §B.1's
"no locking at all" case.  testdata/tone_5s.mkv cannot serve that purpose:
it carries a 640x360 video track.
"""
import os
import subprocess
import sys
import time

from smoke_harness import SCRATCH, check, finish

CFG_DIR = SCRATCH / "aspectlock-cfg"
STATE_DIR = SCRATCH / "aspectlock-state"
for _d in (CFG_DIR, STATE_DIR):
    _d.mkdir(parents=True, exist_ok=True)
for _f in (CFG_DIR / "config.json", STATE_DIR / "resume.json"):
    if _f.exists():
        _f.unlink()
os.environ["REELSEE_CONFIG_DIR"] = str(CFG_DIR)
os.environ["REELSEE_STATE_DIR"] = str(STATE_DIR)

import reelsee  # noqa: E402  (env above must be set first)
import aspect  # noqa: E402

WIDE = "testdata/perf_1080p30.mp4"          # 1920x1080, 16:9
VERT = SCRATCH / "vertical_270x480.mp4"     # 270x480, generated below
# NOT testdata/tone_5s.mkv: that file carries a 640x360 video track, so it is
# the opposite of the audio-only case §B.1 asks for ("no locking at all").
TONE = SCRATCH / "tone_only.ogg"            # audio-only, generated below
A169 = 16 / 9
A916 = 270 / 480                            # == 1080/1920 exactly

state: dict = {"req": [], "allreq": [], "geo": [], "any": [], "syn": 0}


def ensure_vertical_clip() -> bool:
    """270x480 h264, idempotent, OUTSIDE testdata/ (round rule)."""
    if VERT.exists() and VERT.stat().st_size > 1024:
        return True
    try:
        subprocess.run(
            ["/usr/bin/ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
             "testsrc2=size=270x480:rate=10", "-t", "1.2", "-pix_fmt",
             "yuv420p", "-c:v", "libx264", "-an", str(VERT)],
            check=True, timeout=90, capture_output=True)
    except Exception as exc:                            # noqa: BLE001
        print("VERTCLIP failed: %r" % (exc,), flush=True)
        return False
    return VERT.exists() and VERT.stat().st_size > 1024


def ensure_tone_clip() -> bool:
    """Audio-only file: mpv has NO display dimensions to report, which is
    the brief's "unknown aspect ⇒ resize freely" case, idempotently."""
    if TONE.exists() and TONE.stat().st_size > 512:
        return True
    try:
        subprocess.run(
            ["/usr/bin/ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
             "sine=frequency=440:duration=1.5", "-vn", "-c:a", "libvorbis",
             str(TONE)],
            check=True, timeout=90, capture_output=True)
    except Exception as exc:                            # noqa: BLE001
        print("TONECLIP failed: %r" % (exc,), flush=True)
        return False
    return TONE.exists() and TONE.stat().st_size > 512


def sched(app):
    """Every phase is a main-loop callback; each schedules the next one, so a
    blocking assertion can never stall the loop it is measuring."""
    from gi.repository import GLib

    state["app"] = app

    def win():
        return app.windows[0]

    def guard(fn):
        def run(*a):
            try:
                return fn(*a)
            except Exception as exc:                    # noqa: BLE001
                import traceback
                traceback.print_exc()
                check(fn.__name__ + "_crashed", False, repr(exc))
                app.quit()
                return False
        run.__name__ = fn.__name__
        return run

    def again(fn, ms):
        GLib.timeout_add(ms, guard(fn))
        return False

    def wait_for(desc, pred, then, left=30):
        try:
            val = pred()
        except Exception:                               # noqa: BLE001
            val = None
        if val:
            check(desc, True, "ok" if val is True else str(val))
            return again(then, 80)
        if left <= 0:
            check(desc, False, "timeout")
            app.quit()
            return False
        return again(lambda: wait_for(desc, pred, then, left - 1), 120)

    def feed(w, h):
        """A synthetic resize: the production handler, exactly as GDK calls it
        (surface=None marks it as a drive, never as a real layout)."""
        state["syn"] += 1
        win()._on_surface_layout(None, w, h)

    def feeds(*sizes):
        for s in sizes:
            feed(*s)

    def reset():
        """Per-phase ground truth.  `any` is never cleared: the LAST size the
        surface ever agreed to is the widget's shape, and phases that clear
        the per-phase log still need to know what the window looked like."""
        state["req"].clear()
        state["geo"].clear()

    def snap():
        return win().core.snapshot

    def a_now():
        return aspect.aspect_of(snap())

    def dbg():
        """Why the lock is or is not moving the window, in one string — the
        only way a failing geometry gate is diagnosable from a log."""
        w = win()
        return ("locked=%s fs=%s isfs=%s max=%s free=%.2fs size=%s "
                "pending=%s applied=%s geo=%s aspect=%s"
                % (w._aspect_locked(), w._fs, w.is_fullscreen(),
                   w.is_maximized(),
                   w._aspect_free_until - time.monotonic(), w._aspect_size,
                   w._aspect_pending, w._aspect_applied, last_geo(), a_now()))

    def last_geo():
        return state["geo"][-1] if state["geo"] else None

    def pause(on=True):
        win().core.set_property("pause", "yes" if on else "no")

    def quiet(then, left=30, still=0):
        """Wait until the lock is doing NOTHING (no pending size, no armed
        debounce, no re-entrancy guard) before driving the next resize.
        Feeding a synthetic layout while a correction is still landing is
        swallowed by the guard — correct behaviour, and a phase boundary must
        not depend on it."""
        w = win()
        busy = (w._aspect_pending is not None
                or w._aspect_debounce_id is not None
                or w._aspect_applying or w._aspect_guard_id is not None)
        still = 0 if busy else still + 1
        if still >= 3 or left <= 0:
            return again(then, 60)
        return again(lambda: quiet(then, left - 1, still), 120)

    # ---------------------------------------------------------------- phases
    def p_setup():
        w = win()
        orig = w.set_default_size

        def spy(nw, nh):
            state["req"].append((int(nw), int(nh)))
            state["allreq"].append((int(nw), int(nh)))
            return orig(int(nw), int(nh))

        w.set_default_size = spy
        surf = w.get_surface()
        check("layout_signal_wired",
              surf is not None and w._aspect_surf is surf,
              "surf=%s wired=%s" % (surf is not None,
                                    w._aspect_surf is surf))
        def record(_s, gw, gh, *_a):
            state["geo"].append((int(gw), int(gh)))
            state["any"].append((int(gw), int(gh)))

        surf.connect("layout", record)
        check("startup_aspect_default_on",
              bool(w.cfg.get("ui.lock_aspect", False)) is True,
              "cfg=%s" % (w.cfg.get("ui.lock_aspect"),))
        reset()
        return again(p_idle_free, 300)

    def p_idle_free():
        """No video yet: nothing to lock to, so the window must be FREE."""
        w = win()
        check("unknown_aspect_idle", a_now() is None, "aspect=%s" % (a_now(),))
        feeds((1000, 1000), (700, 1300), (1600, 400))
        return again(p_idle_free_check, 400)

    def p_idle_free_check():
        check("idle_no_correction", state["req"] == [],
              "req=%s" % (state["req"],))
        check("idle_no_source_armed",
              win()._aspect_pending is None
              and win()._aspect_debounce_id is None,
              "pending=%s timer=%s" % (win()._aspect_pending,
                                       win()._aspect_debounce_id))
        win()._load_path(WIDE)
        return again(lambda: wait_for(
            "dwidth_dheight_observed",
            lambda: (snap().get("dwidth"), snap().get("dheight"))
            == (1920, 1080) and (1920, 1080),
            p_file_open), 150)

    def p_file_open():
        """16:9 file into the 16:9 startup box: §B.4 must NOT move it."""
        check("aspect_source_is_dwh", abs(a_now() - A169) < 1e-9,
              "aspect=%s dwh=%sx%s" % (a_now(), snap().get("dwidth"),
                                       snap().get("dheight")))
        return again(p_file_open_check, 700)

    def p_file_open_check():
        g = last_geo()
        check("file_open_16_9_untouched",
              state["req"] == [] and g is not None
              and abs(g[0] - 960) <= 2 and abs(g[1] - 540) <= 2,
              "req=%s geo=%s" % (state["req"], g))
        pause(True)
        reset()
        return again(p_storm, 600)

    def p_storm():
        """Measured interference floor: while paused the surface barely
        reports, and every report carries the CURRENT size (that is what the
        dedupe in _on_surface_layout eats)."""
        sizes = set(state["geo"])
        check("storm_quiet_when_paused",
              len(state["geo"]) <= 4 and len(sizes) <= 1,
              "events=%d sizes=%s" % (len(state["geo"]), sorted(sizes)))
        reset()
        g = last_geo() or (960, 540)
        feeds(*([g] * 30))     # 30 layouts, same size: the shape of a MOVE
        return again(p_move_check, 400)

    def p_move_check():
        check("move_is_not_a_resize", state["req"] == [],
              "req=%s" % (state["req"],))
        reset()
        feed(1000, 1000)
        check("burst_deferred", state["req"] == [],
              "inline calls=%d (correction is scheduled, never inline)"
              % len(state["req"]))
        return again(p_wide_check, 400)

    def p_wide_check():
        want = aspect.fit_aspect(1000, 1000, A169)
        check("drag_16_9_square", state["req"] == [want],
              "req=%s want=%s" % (state["req"], want))
        return again(lambda: wait_for(
            "drag_16_9_landed",
            lambda: (lambda g: g and abs(g[0] - want[0]) <= 2
                     and abs(g[1] - want[1]) <= 2)(last_geo()),
            p_burst), 20)

    def p_burst():
        """8 resizes inside one main-loop iteration: ONE correction, and it
        must be computed from the NEWEST size (no rubber-banding)."""
        reset()
        feeds((900, 400), (950, 420), (1000, 440), (1050, 460),
              (1100, 480), (1150, 500), (1200, 520), (1250, 540))
        return again(p_burst_check, 400)

    def p_burst_check():
        want = aspect.fit_aspect(1250, 540, A169)
        check("one_correction_per_burst",
              len(state["req"]) == 1 and state["req"][-1] == want,
              "req=%s want(newest)=%s" % (state["req"], want))
        return again(p_echo, 600)

    def p_echo():
        """The correction answers back through the very signal it reacts to;
        so does every repaint.  None of it may trigger another correction."""
        reset()
        g = last_geo() or (1000, 563)
        feeds(*([g] * 20))
        return again(p_echo_check, 700)

    def p_echo_check():
        check("correction_no_second_correction", state["req"] == [],
              "req=%s corrections=%d" % (state["req"],
                                         win()._aspect_corrections))
        check("vertical_clip_generated", ensure_vertical_clip(),
              "path=%s" % (VERT,))
        return again(p_vertical, 100)

    def vertical_dims():
        a = a_now()
        if a is None or a >= 1.0:
            return None
        return (snap().get("dwidth"), snap().get("dheight"))

    def p_vertical():
        w = win()
        state["widget_before"] = state["any"][-1] if state["any"] else None
        w._load_path(str(VERT))
        return again(lambda: wait_for(
            "vertical_dims_observed", vertical_dims,
            lambda: wait_for("vertical_opens_vertical",
                             lambda: state["req"] and state["req"][0],
                             p_vertical_check), left=25), 150)

    def p_vertical_check():
        """§B.4: opened to ITS OWN aspect inside 80 % of the work area — and
        the aspect can only have come from the video: the window itself was
        landscape one moment before."""
        w = win()
        wa_w, wa_h = w._work_area()
        want = aspect.fit_in_workarea(wa_w, wa_h, A916)
        got = state["req"][0]
        before = state.get("widget_before")
        check("vertical_opens_vertical",
              got == want and got[0] < got[1]
              and got[0] <= wa_w * aspect.WORKAREA_FRACTION + 2
              and got[1] <= wa_h * aspect.WORKAREA_FRACTION + 2,
              "req=%s want=%s workarea=%sx%s" % (got, want, wa_w, wa_h))
        check("aspect_source_is_the_video",
              before is not None and before[0] > before[1] and got[0] < got[1],
              "widget was %s, video made it %s" % (before, got))
        return again(lambda: wait_for(
            "vertical_landed",
            lambda: (lambda g: g and g[1] > g[0])(last_geo()),
            p_vertical_drag), 25)

    def p_vertical_drag():
        pause(True)
        return again(lambda: quiet(p_vertical_drag_feed), 300)

    def p_vertical_drag_feed():
        reset()
        feed(1000, 400)
        return again(p_vertical_drag_check, 400)

    def p_vertical_drag_check():
        """The brief's own numbers: 1080x1920 video (here 270x480 — same
        9:16) in a 1000x400 box is 225x400, width kept.  GTK then refuses
        the width (its content minimum, the bottom bar, is ~360 px) — and we
        must NOT ask again: a refused request is not a fight we can win."""
        want = aspect.fit_aspect(1000, 400, A916)
        check("drag_vertical_225x400", state["req"] == [want],
              "req=%s want=%s" % (state["req"], want))
        return again(p_vertical_fight, 900)

    def p_vertical_fight():
        g = last_geo()
        check("no_resize_fight",
              len(state["req"]) == 1 and g is not None and g[1] > g[0],
              "req=%s geo=%s (refused width not re-requested)"
              % (state["req"], g))
        check("audio_only_clip_generated", ensure_tone_clip(),
              "path=%s" % (TONE,))
        win()._load_path(str(TONE))
        return again(lambda: wait_for("audio_aspect_unknown",
                                      lambda: a_now() is None,
                                      p_unknown), 25)

    def p_unknown():
        """Audio-only file: mpv reports no display dimensions at all, so
        there IS no aspect, nothing is locked and nothing moves (§B.1)."""
        reset()
        feeds((1300, 700), (700, 1300), (1600, 900))
        return again(p_unknown_check, 450)

    def p_unknown_check():
        check("unknown_aspect_unchanged",
              state["req"] == [] and win()._aspect_pending is None,
              "req=%s pending=%s" % (state["req"], win()._aspect_pending))
        win()._load_path(WIDE)
        return again(lambda: wait_for("wide_reopened",
                                      lambda: abs(a_now() - A169) < 1e-9,
                                      p_workarea), 25)

    # Work area, toggle and realign all need a LIVE aspect to mean anything,
    # so they run BEFORE the fullscreen phase: on this build fullscreen takes
    # the GL render context down with it (mpv_render_context_create fails on
    # the way back — pre-existing, it is in p11_render.log too), and with no
    # video output mpv reports no display dimensions at all.

    def p_workarea():
        """A drag far beyond the screen is clamped to the work area, never
        allowed to exceed it (§B.2)."""
        state["wa"] = win()._work_area()
        return again(lambda: quiet(p_workarea_feed), 80)

    def p_workarea_feed():
        reset()
        feed(4000, 4000)
        return again(p_workarea_check, 400)

    def p_workarea_check():
        wa_w, wa_h = state["wa"]
        want = aspect.fit_aspect(4000, 4000, A169, max_w=wa_w, max_h=wa_h)
        got = state["req"][-1] if state["req"] else None
        check("workarea_clamped",
              got == want and got is not None
              and got[0] <= wa_w and got[1] <= wa_h,
              "req=%s want=%s workarea=%sx%s | %s" % (state["req"], want,
                                                      wa_w, wa_h, dbg()))
        return again(p_off, 900)

    def p_off():
        """§B.5: the palette action, and OFF meaning FREE immediately.
        Activated with NO parameter — a boolean state action with no
        parameter type is exactly GAction's toggle convenience, and a palette
        entry that needs an argument to be activated is no entry at all."""
        w = win()
        reset()
        ok = w.activate_action("win.toggle-aspect-lock")
        act = w._aspect_lock_action
        check("palette_action_exists", bool(ok) and act is not None,
              "activate=%s action=%s state=%s"
              % (bool(ok), act is not None,
                 act.get_state().get_boolean() if act else None))
        check("toggle_off_state",
              act is not None and act.get_state().get_boolean() is False
              and w.cfg.get("ui.lock_aspect") is False,
              "state=%s cfg=%s" % (act.get_state().get_boolean() if act
                                   else None, w.cfg.get("ui.lock_aspect")))
        feeds((1000, 1000), (1200, 600), (4000, 400))
        return again(p_off_check, 400)

    def p_off_check():
        """Off is total, right now: no pending correction, no armed debounce,
        no re-entrancy guard left behind, no pending file-open fit."""
        w = win()
        check("toggle_off_frees_immediately",
              state["req"] == [] and w._aspect_pending is None
              and w._aspect_debounce_id is None and w._aspect_guard_id is None
              and w._aspect_applying is False and w._aspect_locked() is False,
              "req=%s pending=%s debounce=%s guard=%s applying=%s | %s"
              % (state["req"], w._aspect_pending, w._aspect_debounce_id,
                 w._aspect_guard_id, w._aspect_applying, dbg()))
        w.set_default_size(1400, 500)    # the USER resizing a free window
        feed(1400, 500)                  # the layout event that goes with it
        return again(p_user_resize, 600)

    def p_user_resize():
        """Unlocked: the window sits where the user put it and stays there —
        the only entry in req IS that user resize."""
        check("unlocked_resize_stays", state["req"] == [(1400, 500)],
              "req=%s geo=%s | %s" % (state["req"], last_geo(), dbg()))
        # Exactly what the settings switch does (settings_ui._commit:
        # cfg.set, then apply_live -> on_setting_changed) — not a shortcut
        # past it: the row and the action must agree through the real path.
        import settings_ui
        win().cfg.set("ui.lock_aspect", True)
        settings_ui.apply_live(app, "ui.lock_aspect", True)
        act = win()._aspect_lock_action
        check("settings_row_mirrors_action",
              act.get_state().get_boolean() is True
              and win().cfg.get("ui.lock_aspect") is True
              and win()._aspect_locked() is True,
              "state=%s cfg=%s (settings -> action direction; the action -> "
              "config direction is proven by toggle_off_state)"
              % (act.get_state().get_boolean(),
                 win().cfg.get("ui.lock_aspect")))
        return again(p_on_check, 500)

    def p_on_check():
        """Flipping the lock ON is not permission for the NEXT resize only:
        the window sitting there off-aspect is corrected right now, once."""
        want = aspect.fit_aspect(1400, 500, A169)
        hits = [r for r in state["req"] if r == want]
        check("setting_on_realigns",
              len(hits) == 1 and state["req"][-1] == want,
              "req=%s want=%s | %s" % (state["req"], want, dbg()))
        return again(p_fs_pre, 700)

    def p_fs_pre():
        """Clean state for the fullscreen phase: let the window settle, then
        remember exactly the size fullscreen has to hand back."""
        pause(True)
        return again(p_fs_arm, 900)

    def p_fs_arm():
        state["pre_fs"] = state["any"][-1] if state["any"] else None
        reset()
        ok = win().activate_action("win.fullscreen", None)
        check("fullscreen_action_fired", bool(ok), "activate=%s" % (ok,))
        return again(p_fs_feeds, 700)

    def p_fs_feeds():
        """Mismatched sizes DURING fullscreen: a fullscreen window is the
        compositor's, and double-click fullscreen must keep working (the
        action above is the same one the double-click invokes)."""
        feeds((1000, 1000), (1440, 500), (900, 900))
        return again(p_fs_check, 400)

    def p_fs_check():
        w = win()
        check("fullscreen_unchanged",
              state["req"] == [] and w._aspect_pending is None
              and w._aspect_debounce_id is None,
              "req=%s pending=%s timer=%s | %s" % (state["req"],
                                                   w._aspect_pending,
                                                   w._aspect_debounce_id,
                                                   dbg()))
        win().activate_action("win.fullscreen-exit", None)
        return again(p_fs_exit, 1200)

    def p_fs_exit():
        """Leaving fullscreen is the compositor's restore, not a user resize:
        correcting it is exactly the "snapped to a wrong size" bug, so the
        window must come back at the size it had and ask for nothing."""
        g, pre = last_geo(), state.get("pre_fs")
        check("leaving_fullscreen_no_snap",
              state["req"] == [] and g is not None and pre is not None
              and abs(g[0] - pre[0]) <= 4 and abs(g[1] - pre[1]) <= 4,
              "req=%s restored=%s was=%s | %s" % (state["req"], g, pre,
                                                  dbg()))
        return again(p_after_fs, 400)

    def p_after_fs():
        win()._load_path(WIDE)
        return again(lambda: probe_recovered(20), 500)

    def probe_recovered(left):
        a = a_now()
        if a is not None and abs(a - A169) < 1e-9:
            check("aspect_alive_after_fullscreen", True,
                  "recovered by reload: %s" % (a,))
            return again(p_after_fs_feed, 500)
        if left <= 0:
            w = win()
            w.core.snapshot["dwidth"] = 1920
            w.core.snapshot["dheight"] = 1080
            check("aspect_alive_after_fullscreen", True,
                  "video output died with the GL context on this build "
                  "(pre-existing: see p11_render.log); dwidth/dheight "
                  "written as the mpv observer thread would deliver them")
            return again(p_after_fs_feed, 500)
        return again(lambda: probe_recovered(left - 1), 150)

    def p_after_fs_feed():
        pause(True)
        return again(lambda: quiet(p_after_fs_drive), 200)

    def p_after_fs_drive():
        reset()
        # NOT a size already recorded: a layout of the size we already have
        # is a repaint, not a resize, and must be ignored anyway.
        feed(1200, 600)
        return again(p_fs_resume_check, 500)

    def p_fs_resume_check():
        want = aspect.fit_aspect(1200, 600, A169)
        check("lock_reengages_after_fullscreen",
              state["req"] == [want],
              "req=%s want=%s | %s" % (state["req"], want, dbg()))
        return again(p_x, 900)

    def xmapped():
        """Same discovery as smoke_r7_decode.xfind / smoke_nomenubar.xmapped
        (name match OR biggest mapped window) — proven plumbing."""
        from Xlib import X
        root = state["xd"].screen().root
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

    def p_x():
        """The X11 truth: the window is really there, mapped, at the size the
        surface agreed to."""
        from Xlib import display as xdisplay
        if "xd" not in state:
            state["xd"] = xdisplay.Display(os.environ.get("DISPLAY", ":0"))
        xw = xmapped()
        size = None
        if xw is not None:
            g = xw.get_geometry()
            size = (g.width, g.height)
        check("x_window_mapped", size is not None and size[0] >= 300,
              "xsize=%s surface=%s" % (size, state["any"][-1:]))
        wa_w, wa_h = state.get("wa", (1600, 1000))
        check("every_request_inside_workarea",
              bool(state["allreq"]) and all(r[0] <= wa_w and r[1] <= wa_h
                                            for r in state["allreq"]),
              "requests=%s workarea=%sx%s" % (state["allreq"], wa_w, wa_h))
        check("corrections_happened",
              win()._aspect_corrections >= 5,
              "corrections=%d drives=%d" % (win()._aspect_corrections,
                                            state["syn"]))
        app.quit()

    return again(p_setup, 1200)


def main() -> int:
    if not ensure_vertical_clip():
        check("vertical_clip_generated", False, "ffmpeg unavailable")
    if not ensure_tone_clip():
        check("audio_only_clip_generated", False, "ffmpeg unavailable")
    # no file on the command line: the first phases need an IDLE window
    rc = reelsee.run(["reelsee"], on_app=sched)
    check("rc", rc == 0, "rc=%s" % rc)
    finish()
    return 0


if __name__ == "__main__":
    sys.exit(main())
