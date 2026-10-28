#!/usr/bin/python3
"""R14-B gate: the aspect lock reaches the X SERVER as WM_NORMAL_HINTS.

The defect: the round-8 lock CORRECTED a wrong size after the fact (from
Gdk.Surface::layout, via set_default_size) but never TOLD the window
manager anything — no PAspect was ever written, so under a real WM a
drag-resize sailed through whatever shape the video had and the
compositor's mid-drag geometry always beat our post-hoc fix.

The proof, headless but honest: there is NO window manager in this
environment, so the gate reads the property back FROM THE X SERVER with
python-xlib — the same bytes a real WM would read — and asserts:

  * idle (no video): no PAspect at all;
  * 16:9 clip loaded: PAspect exactly (16,9), min-aspect == max-aspect
    (the pin), small integers, and the pair within 1 px of the observed
    dwidth/dheight at the window's current height;
  * 9:16 clip loaded next: the pair FLIPS to (9,16);
  * aspect-lock OFF clears PAspect, ON restores it; crop-to-window ON
    clears (R11: the crop needs a free box), OFF restores; fullscreen
    clears, windowed restores; stop (no video) clears;
  * a left drag STILL initiates the EWMH move (_NET_WM_MOVERESIZE with
    our xid at the root — the same protocol proof as the R14-A gate, the
    staged-EWMH recipe reused) — publishing hints must not turn a move
    into a resize;
  * the §R8-B correction coexists quietly: one off-aspect burst produces
    exactly ONE correction and no second wave, and the published pair
    survives the storm (correction and hints never fight);
  * FALLBACK: with x11pos.set_aspect_ratio/clear_aspect_ratio patched to
    return False and the property deleted from the server (the Wayland /
    hint-refusing case), the R8 path still fires and still lands the
    window on the video's shape — and nothing re-publishes behind it.

Checks: paspect_absent_idle, dwidth_wide_observed, paspect_wide_16_9,
paspect_wide_pinned, paspect_small_ints, paspect_1px_at_size,
dwidth_vert_observed, paspect_vert_9_16, paspect_cleared_lock_off,
paspect_restored_lock_on, paspect_cleared_crop_on,
paspect_restored_crop_off, paspect_cleared_fullscreen,
paspect_restored_windowed, left_drag_moveresize_sent, left_drag_no_pause,
storm_one_correction, storm_no_second_wave, paspect_survives_storm,
storm_correction_landed, paspect_cleared_no_video,
fallback_hints_stay_absent, fallback_r8_correction_fired,
fallback_r8_settles, fallback_no_republish, window_alive, run_rc.
"""
import os
import subprocess
import sys

from smoke_harness import SCRATCH, check, finish

CFG_DIR = SCRATCH / "aspecthints-cfg"
STATE_DIR = SCRATCH / "aspecthints-state"
for _d in (CFG_DIR, STATE_DIR):
    _d.mkdir(parents=True, exist_ok=True)
for _f in (CFG_DIR / "config.json", STATE_DIR / "resume.json",
           STATE_DIR / "window.json"):
    if _f.exists():
        _f.unlink()
os.environ["REELSEE_CONFIG_DIR"] = str(CFG_DIR)
os.environ["REELSEE_STATE_DIR"] = str(STATE_DIR)

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")

import reelsee  # noqa: E402
import aspect  # noqa: E402

WIDE = "testdata/perf_1080p30.mp4"           # 1920x1080 -> 16:9
VERT = SCRATCH / "vertical_270x480.mp4"      # 9:16 of the brief, generated

# ICCCM WM_SIZE_HINTS word indices (format 32, 18 words) — same table as
# src/x11pos.py; duplicated ON PURPOSE so the gate never inherits a bug in
# the parser it is supposed to be auditing.
_F_FLAGS, _F_MINASP, _F_MAXASP = 0, 11, 13
_PASPECT = 1 << 5

state: dict = {}


def ensure_vertical_clip() -> bool:
    """270x480 h264 (== 9:16, the brief's 1080x1920 shrunk), idempotent,
    OUTSIDE testdata/ — the recipe smoke_aspectlock.py proved, with two
    deliberate differences:

    * SIXTY SECONDS long — this gate's phase chain (two clips, every
      lock/crop/fullscreen edge, drag, storm, fallback) spans tens of
      seconds; a clip that ENDS mid-chain idles the core and every late
      phase then reads a dead video (a 1.2 s clip passed the early
      phases and failed the late ones exactly like a real bug would).
    * a silent AUDIO track — fullscreen re-parents the GLArea (R5-D),
      its GL context dies, and under this headless software GL the mpv
      render context RE-CREATION fails (pre-existing: printed once as
      `mpv_render_context_create: something happened`, reproducible on
      pre-R14 HEAD, and the 16:9 clip hits it too).  mpv then drops a
      VIDEO-ONLY file outright (idle-active — the storm of blanked
      dwidth/dheight at the fullscreen edge), so the fullscreen-leave
      phase would assert hints for a file that no longer exists.  With
      a track the core survives the context loss exactly like the 16:9
      clip does, and the leave edge has a live video to declare."""
    if VERT.exists() and VERT.stat().st_size > 1024:
        try:    # keep the cache honest about DURATION, not just size
            out = subprocess.run(
                ["/usr/bin/ffprobe", "-v", "error", "-show_entries",
                 "format=duration", "-of", "csv=p=0", str(VERT)],
                check=True, timeout=30, capture_output=True, text=True)
            if float(out.stdout.strip()) >= 30.0:
                return True
        except Exception:                                # noqa: BLE001
            pass                                          # regen below
    try:
        subprocess.run(
            ["/usr/bin/ffmpeg", "-v", "error", "-y",
             "-f", "lavfi", "-i", "testsrc2=size=270x480:rate=10",
             "-f", "lavfi", "-i", "anullsrc=r=8000:cl=mono",
             "-t", "60", "-pix_fmt", "yuv420p", "-c:v", "libx264",
             "-c:a", "aac", "-shortest", str(VERT)],
            check=True, timeout=180, capture_output=True)
    except Exception as exc:                            # noqa: BLE001
        print("VERTCLIP failed: %r" % (exc,), flush=True)
        return False
    return VERT.exists() and VERT.stat().st_size > 1024


def stage_wm_presence():
    """Xvfb has no window manager; GDK refuses begin_move unless the
    screen advertises one (the check window is cached at screen init).
    Stage the textbook-minimal EWMH presence BEFORE the app opens its
    display — the R14-A gate's proven recipe — so the drag phase can
    prove that publishing size hints does not break window MOVES."""
    from Xlib import X
    from Xlib import display as xdisplay
    d = xdisplay.Display(os.environ.get("DISPLAY", ":0"))
    root = d.screen().root
    wmwin = root.create_window(0, 0, 1, 1, 0, X.CopyFromParent,
                               window_class=X.InputOutput,
                               override_redirect=1, event_mask=0)
    wmwin.map()
    xa_window = d.intern_atom("WINDOW")
    xa_atom = d.intern_atom("ATOM")
    sup = d.intern_atom("_NET_SUPPORTING_WM_CHECK")
    wmwin.change_property(sup, xa_window, 32, [wmwin.id], X.PropModeReplace)
    root.change_property(sup, xa_window, 32, [wmwin.id], X.PropModeReplace)
    root.change_property(d.intern_atom("_NET_SUPPORTED"), xa_atom, 32,
                         [d.intern_atom("_NET_WM_MOVERESIZE")],
                         X.PropModeReplace)
    d.flush()
    d.sync()
    return d, wmwin


def sched(app):
    from gi.repository import GLib
    import x11pos

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

    def wait_for(desc, pred, then, left=40):
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

    # ------------------------------------------------------------ X plumbing
    def xopen():
        from Xlib import display as xdisplay
        state["xd"] = xdisplay.Display(os.environ.get("DISPLAY", ":0"))

    def xfind():
        from Xlib import X
        d = state["xd"]
        root = d.screen().root
        found = [None]

        def name_of(w):
            try:
                n = w.get_wm_name()
                return n.decode("utf-8", "replace") if isinstance(
                    n, bytes) else (n or None)
            except Exception:                           # noqa: BLE001
                return None

        def walk(w, depth=0):
            if found[0] is not None or depth > 6:
                return
            nm = name_of(w)
            try:
                mapped = (w.get_attributes().map_state == X.IsViewable)
            except Exception:                           # noqa: BLE001
                mapped = False
            if nm and "REELSEE" in nm and mapped and w != root:
                found[0] = w
                return
            try:
                kids = w.query_tree().children
            except Exception:                           # noqa: BLE001
                kids = []
            for k in kids:
                walk(k, depth + 1)
        walk(root)
        return found[0]

    def hints_now():
        """THE SERVER'S live WM_NORMAL_HINTS, parsed by this gate's own
        ICCCM code (never src/x11pos — the auditor may not borrow the
        auditee's parser).  None = property absent."""
        from Xlib import X
        w = state["winx"]
        state["xd"].sync()                # flush OUR pending reads;
        # get_full_property itself round-trips, so the reply reflects the
        # latest server state (the app's sync on its own connection has
        # already ordered its write on the server).
        prop = w.get_full_property(state["xd"].intern_atom(
            "WM_NORMAL_HINTS"), X.AnyPropertyType)
        if prop is None or prop.format != 32 or prop.value is None:
            return None
        arr = [int(v) & 0xFFFFFFFF for v in prop.value]
        arr += [0] * (18 - len(arr)) if len(arr) < 18 else []
        return arr[:18]

    def paspect_now():
        """(min_num, min_den, max_num, max_den) if PAspect is SET, else
        None — read back from the server on every call, never cached."""
        arr = hints_now()
        if arr is None or not (arr[_F_FLAGS] & _PASPECT):
            return None
        return (arr[_F_MINASP], arr[_F_MINASP + 1],
                arr[_F_MAXASP], arr[_F_MAXASP + 1])

    def pinned(n, d):
        def pred():
            p = paspect_now()
            return p if p == (n, d, n, d) else None
        return pred

    def cleared():
        return paspect_now() is None

    def fake_button(kind, x, y, button=1):
        from Xlib import X
        from Xlib.ext import xtest
        d = state["xd"]
        d.screen().root.warp_pointer(x, y)
        d.sync()
        et = X.ButtonPress if kind == "press" else X.ButtonRelease
        xtest.fake_input(d, et, button)
        d.sync()

    def wgeom():
        g = state["winx"].get_geometry()
        return g.width, g.height

    def paused():
        v = win().core.snapshot.get("pause")
        return None if v is None else bool(v)

    def feed(w, h):
        win()._on_surface_layout(None, w, h)

    def quiet(then, left=30, still=0):
        """R8 machinery idle (aspectlock's helper): never feed a burst
        while a correction is still landing."""
        w = win()
        busy = (w._aspect_pending is not None
                or w._aspect_debounce_id is not None
                or w._aspect_applying or w._aspect_guard_id is not None)
        still = 0 if busy else still + 1
        if still >= 3 or left <= 0:
            return again(then, 60)
        return again(lambda: quiet(then, left - 1, still), 120)

    def on_aspect(gw, gh, ratio):
        return abs(gh - gw * ratio) <= 2.5

    # ---------------------------------------------------------------- phases
    def s_idle():
        check("paspect_absent_idle", cleared(),
              f"paspect={paspect_now()}")
        win()._load_path(WIDE)
        return again(guard(s_wide_dwh), 200)

    def s_wide_dwh():
        return wait_for("dwidth_wide_observed",
                        lambda: win().core.snapshot.get("dwidth") == 1920,
                        s_wide_hints, left=50)

    def s_wide_hints():
        def pred():
            p = paspect_now()
            return p if p and p[0] > 0 else None
        return wait_for("paspect_wide_16_9",
                        lambda: paspect_now() == (16, 9, 16, 9),
                        s_wide_extra, left=60)

    def s_wide_extra():
        p = paspect_now()
        check("paspect_wide_pinned", p[0:2] == p[2:4], f"words={p}")
        check("paspect_small_ints",
              all(v <= aspect.PASPECT_MAX_DEN for v in p), f"words={p}")
        # the 1 px contract AT THE CURRENT SIZE: pair vs the video's true
        # ratio, expressed in pixels of the window's live height.
        gw, gh = wgeom()
        off = abs(16 / 9 - 1920 / 1080) * gh
        check("paspect_1px_at_size", off <= 1.0 + 1e-9,
              f"off_px={off:.3f} size={gw}x{gh}")
        win()._load_path(str(VERT))
        return again(guard(s_vert_dwh), 200)

    def s_vert_dwh():
        return wait_for("dwidth_vert_observed",
                        lambda: win().core.snapshot.get("dwidth") == 270,
                        s_vert_hints, left=50)

    def s_vert_hints():
        return wait_for("paspect_vert_9_16",
                        lambda: paspect_now() == (9, 16, 9, 16),
                        s_lock_off, left=60)

    def s_lock_off():
        win()._aspect_lock_action.activate(None)        # palette edge
        return wait_for("paspect_cleared_lock_off", cleared, s_lock_on,
                        left=50)

    def s_lock_on():
        win()._aspect_lock_action.activate(None)        # ... and back on
        return wait_for("paspect_restored_lock_on",
                        pinned(9, 16), s_crop_on, left=50)

    def s_crop_on():
        win().activate_action("win.crop-to-window", None)
        return wait_for("paspect_cleared_crop_on", cleared, s_crop_off,
                        left=50)

    def s_crop_off():
        win().activate_action("win.crop-to-window", None)
        return wait_for("paspect_restored_crop_off",
                        pinned(9, 16), s_fs, left=60)

    def s_fs():
        w = win()
        w._act_fullscreen()                             # enter
        return wait_for("paspect_cleared_fullscreen",
                        lambda: bool(w._fs) and cleared(), s_windowed,
                        left=50)

    def s_windowed():
        w = win()
        w._act_fullscreen_exit()                        # leave
        # The clip carries a silent audio track precisely so the core
        # survives this edge under the headless renderer (see
        # ensure_vertical_clip for the measured context-loss story).
        # The vo re-init at ENTER blanks dwidth/dheight and they are
        # never re-emitted while playback continues — measured on
        # pre-R14 HEAD as well as here — so this assertion drives the
        # LAST-OBSERVED-PAIR fallback: exactly the path that must keep
        # the window manager honouring the ratio for a user returning
        # from fullscreen.
        return wait_for("paspect_restored_windowed",
                        lambda: not w._fs and paspect_now()
                        == (9, 16, 9, 16), s_drag_move, left=60)

    # ------------------------------------------------------------ drag phase
    def s_drag_move():
        # Same protocol proof as the R14-A gate: GDK's begin_move sends
        # _NET_WM_MOVERESIZE to the ROOT (EWMH presence was staged before
        # the app started).  PUBLISHING SIZE HINTS must not turn a MOVE
        # drag into a resize attempt — the message still has to leave.
        from Xlib import X
        d = state["xd"]
        root = d.screen().root
        root.change_attributes(event_mask=X.SubstructureNotifyMask)
        d.sync()
        while d.pending_events():
            d.next_event()
        state["mt"] = d.intern_atom("_NET_WM_MOVERESIZE")
        state["xid"] = state["winx"].id
        g = state["winx"].get_geometry()
        tr = state["winx"].translate_coords(root, 0, 0)
        cx, cy = tr.x + g.width // 2, tr.y + g.height // 2
        state["pause0"] = paused()
        fake_button("press", cx, cy, button=1)

        def step(i):
            if i <= 6:
                root.warp_pointer(cx + 90 * i // 6, cy + 60 * i // 6)
                d.sync()
                return again(guard(lambda: step(i + 1)), 35)
            root.warp_pointer(cx + 90, cy + 60)
            d.sync()
            fake_button("release", cx + 90, cy + 60, button=1)
            return again(guard(s_drag_check), 450)
        return again(guard(lambda: step(1)), 60)

    def s_drag_check():
        from Xlib import X
        d = state["xd"]
        seen = False
        while d.pending_events():
            ev = d.next_event()
            if (getattr(ev, "type", None) == X.ClientMessage
                    and getattr(ev, "client_type", None) == state["mt"]):
                wv = getattr(ev, "window", None)
                if getattr(wv, "id", wv) == state["xid"]:
                    seen = True
        check("left_drag_moveresize_sent", seen,
              "_NET_WM_MOVERESIZE with our xid at root")
        check("left_drag_no_pause", paused() is state["pause0"],
              f"pause={paused()}")
        return again(guard(s_burst), 250)

    # --------------------------------------------------------- storm phase
    def s_burst():
        win().core.set_property("pause", "yes")
        return quiet(s_burst_reload)

    def s_burst_reload():
        # The storm phase must exercise the R8-B correction, and the
        # correction reads the LIVE video ratio.  The fullscreen edge
        # earlier in this chain blanked dwidth/dheight for the rest of
        # the file (headless context loss; see ensure_vertical_clip),
        # so this phase starts from the same real edge the fallback
        # phase uses: a RELOAD, at which dwidth re-arrives.
        win()._load_path(str(VERT))
        return wait_for("storm_video_reloaded",
                        lambda: win().core.snapshot.get("dwidth") == 270,
                        s_burst_settle, left=50)

    def s_burst_settle():
        # let the reload's own fit land (its set_default_size must NOT
        # be counted as the burst's correction — req is cleared after)
        return quiet(s_burst_feed)

    def s_burst_feed():
        state["req"].clear()
        state["storm_base"] = (tuple(state["geo"][-1])
                               if state["geo"] else (0, 0))
        for _ in range(3):                              # one drag burst
            feed(1200, 1200)                            # square: off 9:16
        return again(guard(s_burst_check), 600)

    def s_burst_check():
        reqs = list(state["req"])
        check("storm_one_correction", len(reqs) == 1, f"req={reqs}")
        return again(guard(s_burst_later), 800)

    def s_burst_later():
        reqs = list(state["req"])
        check("storm_no_second_wave", len(reqs) == 1, f"req={reqs}")
        check("paspect_survives_storm",
              paspect_now() == (9, 16, 9, 16),
              f"paspect={paspect_now()}")
        return again(guard(s_burst_landed), 250)

    def s_burst_landed():
        def pred():
            g = state["geo"][-1] if state["geo"] else None
            if not g or g == state["storm_base"]:
                return None                             # must have MOVED
            return g if on_aspect(g[0], g[1], 480 / 270) else None
        return wait_for("storm_correction_landed", pred, s_stop,
                        left=50)

    # ----------------------------------------------------------- stop phase
    def s_stop():
        win()._act_stop()
        return wait_for("paspect_cleared_no_video", cleared, s_fallback,
                        left=50)

    # ------------------------------------------------------ fallback phase
    def s_fallback():
        # Simulate the no-hints world (Wayland, or a server/WM that
        # refuses): both seam entries return False, and the property is
        # deleted from the server.  R8's post-hoc correction must still
        # fire and still land the shape — and NOTHING may re-publish.
        x11pos.set_aspect_ratio = lambda *a, **k: False
        x11pos.clear_aspect_ratio = lambda *a, **k: False
        state["winx"].delete_property(
            state["xd"].intern_atom("WM_NORMAL_HINTS"))
        state["xd"].sync()
        win()._load_path(str(VERT))
        return again(guard(s_fallback_loaded), 200)

    def s_fallback_loaded():
        return wait_for("fallback_hints_stay_absent",
                        lambda: win().core.snapshot.get("dwidth") == 270
                        and cleared(), s_fallback_burst, left=60)

    def s_fallback_burst():
        return quiet(s_fallback_feed)

    def s_fallback_feed():
        state["req"].clear()
        for _ in range(3):
            feed(1200, 1200)
        return again(guard(s_fallback_check), 600)

    def s_fallback_check():
        reqs = list(state["req"])
        check("fallback_r8_correction_fired", len(reqs) >= 1,
              f"req={reqs}")
        check("fallback_no_republish", cleared(),
              f"paspect={paspect_now()}")
        return again(guard(s_fallback_landed), 250)

    def s_fallback_landed():
        def pred():
            g = state["geo"][-1] if state["geo"] else None
            return g if g and on_aspect(g[0], g[1], 480 / 270) else None
        return wait_for("fallback_r8_settles", pred, s_fin, left=50)

    def s_fin():
        check("window_alive", win().get_realized(), "True")
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
        x = xfind()
        if x is None:
            if left <= 0:
                check("x_window_found", False, "no REELSEE window")
                app.quit()
                return False
            return again(lambda: start(left - 1), 300)
        check("x_window_found", True, "mapped")
        state["winx"] = x
        # ground truth: every set_default_size the window asks for, plus
        # the sizes the SURFACE really reached (second, independent layout
        # connection — the aspectlock gate's measurement), plus every
        # decision the R14-B seam takes in-process.
        state["req"] = []
        state["geo"] = []
        state["pubs"] = []
        orig_sds = w.set_default_size

        def sds(ww, hh):
            state["req"].append((ww, hh))
            return orig_sds(ww, hh)
        w.set_default_size = sds
        # getattr defaults so the SAME script also runs against a
        # pre-R14-B tree (gate discrimination): the seam simply never
        # fires there and the real checks below fail where they belong.
        orig_set = getattr(x11pos, "set_aspect_ratio",
                           lambda *a, **k: False)
        orig_clear = getattr(x11pos, "clear_aspect_ratio", lambda *a: True)

        def spy_set(wid, num, den):
            ok = orig_set(wid, num, den)
            state["pubs"].append(("set", num, den, ok))
            return ok

        def spy_clear(wid):
            ok = orig_clear(wid)
            state["pubs"].append(("clear", ok))
            return ok
        x11pos.set_aspect_ratio = spy_set
        x11pos.clear_aspect_ratio = spy_clear
        surf = w.get_surface()
        if surf is not None:
            surf.connect("layout",
                         lambda s, ww, hh, *a: state["geo"].append(
                             (ww, hh)))
        w.present()
        state["xd"].sync()
        return again(guard(lambda: wait_for(
            "window_mapped", lambda: w.get_mapped(), s_idle)), 400)

    GLib.timeout_add(300, guard(start))


def main() -> int:
    if not ensure_vertical_clip():
        check("vert_clip_generated", False, "ffmpeg failed")
        finish()
        return 1
    state["wmdisp"], state["wmwin"] = stage_wm_presence()
    rc = reelsee.run(["reelsee"], on_app=sched)   # NO file: idle first
    check("run_rc", rc == 0, str(rc))
    finish()
    return 0


if __name__ == "__main__":
    sys.exit(main())
