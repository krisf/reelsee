#!/usr/bin/python3
"""R11-D pixel gate: crop-to-window really fills the window and really
clips — measured in pixels, not asserted from properties.

The mechanism rung is `panscan=1.0` with keepaspect=yes (the round-11
probe MEASURED cover zoom rendering through the embedded vo=libmpv
render API: tall-window top band went 100% black -> green); this script
repeats that truth end-to-end through the REAL toggle path (action-group
activation exactly like the palette), and adds the state machine:

  1. wide 16:9 clip in a TALL window (420x900): crop off => top band is
     black letterbox; crop on => same band <= 2% black and shows the
     video's GREEN middle (red/blue thirds clipped away);
  2. crop off restores the bars;
  3. vertical clip in a WIDE window (960x540): crop off => side columns
     black; crop on => columns full-bleed AND the top/bottom rows read
     GREEN — the video's red top and blue bottom are cropped out (the
     cover shows only the green middle);
  4. crop off restores those bars too;
  5. aspect interplay (§C): with the lock setting ON while crop is ON,
     a free-form resize does NOT snap (correction counter frozen);
     crop OFF re-arms and snaps the window to the video's aspect ONCE.

Every capture waits for a clean frame: chrome bars hidden (auto-hide is
only active while PLAYING), OSD and toast both hidden, the observed
snapshot matching the toggle, and the X11 rect matching the requested
size.  Clips are generated into SCRATCH (never testdata/).  Pauses
before quitting — the pre-existing shutdown flake (proven on pristine
HEAD, unrelated to crop) only bites a quit while rendering; since R13
the pause is OBSERVED before quitting and a printed verdict exits hard
(os._exit) so a hung teardown can never veto it.  The FINAL pixel
re-measure retries until the composited window is stable: R13 forensics
found the single-capture version failing 4/6 on PRISTINE HEAD (transient
repaint lag after the aspect snap), so this hardening predates and is
independent of the idle-chrome fix.
"""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))
from smoke_harness import SCRATCH, capture_xwd, check, finish  # noqa: E402

CFG = SCRATCH / "cropwin-cfg"
STATE = SCRATCH / "cropwin-state"
CFG.mkdir(parents=True, exist_ok=True)
STATE.mkdir(parents=True, exist_ok=True)
# R8's aspect machinery must not move the window during phases 1-4:
# the §C interplay re-enables the lock live, LATER, on purpose.
# loop-file: inf keeps playback genuinely RUNNING the whole gate (EOF in
# a 12 s clip would pause it, and ChromeState never hides chrome while
# paused — every clean-frame poll would then time out).
(CFG / "config.json").write_text(
    '{"ui": {"lock_aspect": false},\n'
    ' "playback": {"loop_file": "inf"}}')
for f in (STATE / "window.json", STATE / "resume.json"):
    if f.exists():
        f.unlink()
os.environ["REELSEE_CONFIG_DIR"] = str(CFG)
os.environ["REELSEE_STATE_DIR"] = str(STATE)

WIDE = SCRATCH / "wide_bands.mp4"     # 640x360: red | green 128..512 | blue
VERT = SCRATCH / "vert_bands.mp4"     # 270x480: red / green 96..384 / blue


def ensure_clips():
    if not (WIDE.exists() and WIDE.stat().st_size > 1024):
        subprocess.run(
            ["/usr/bin/ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
             "color=c=red:s=640x360:r=10:d=12",
             "-vf",
             "drawbox=x=128:y=0:w=384:h=360:color=green:t=fill,"
             "drawbox=x=512:y=0:w=128:h=360:color=blue:t=fill",
             "-pix_fmt", "yuv420p", "-c:v", "libx264", "-an", str(WIDE)],
            check=True, timeout=120, capture_output=True)
    if not (VERT.exists() and VERT.stat().st_size > 1024):
        subprocess.run(
            ["/usr/bin/ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
             "color=c=red:s=270x480:r=10:d=12",
             "-vf",
             "drawbox=x=0:y=96:w=270:h=288:color=green:t=fill,"
             "drawbox=x=0:y=384:w=270:h=96:color=blue:t=fill",
             "-pix_fmt", "yuv420p", "-c:v", "libx264", "-an", str(VERT)],
            check=True, timeout=120, capture_output=True)


def measure(xwd: Path, geo: str) -> tuple[float, float, float, float]:
    """(black %, mean r, g, b) over the root-screen crop region."""
    out = subprocess.run(
        ["convert", str(xwd), "-crop", geo, "+repage", "-colorspace", "gray",
         "-threshold", "8%", "-format", "%[fx:mean]", "info:"],
        capture_output=True, text=True, check=True)
    black = 100.0 * (1.0 - float(out.stdout.strip()))
    out = subprocess.run(
        ["convert", str(xwd), "-crop", geo, "+repage", "-format",
         "%[fx:mean.r] %[fx:mean.g] %[fx:mean.b]", "info:"],
        capture_output=True, text=True, check=True)
    r, g, b = (255.0 * float(x) for x in out.stdout.split())
    return black, r, g, b


import reelsee  # noqa: E402
import x11pos  # noqa: E402

# Every region is derived from the video widget's LIVE surface allocation
# (GTK ships the surface with CSD margins: xwd root coords != X rect
# coords, measured).  Fractions keep the sample well inside the video
# rect, clear of OSD (centre-left), toast (top-centre) and any bar.


progress = {"final": False}


def sched(app):
    from gi.repository import GLib

    state = {"settles": 0, "hover_releases": 0}

    def win():
        return app.windows[0]

    def rect(w):
        return x11pos.get_rect(w)

    def prev_rect(w):
        """The video (GLArea) rect in root coords (Xvfb surface at 0,0).

        CSD margins make root coords != X rect coords (measured), so the
        live allocation is the only honest frame of reference; the GTK
        version here predates get_surface_allocation."""
        a = w.view.area
        r = a.compute_bounds(w)          # toplevel-widget space = surface
        if isinstance(r, tuple):         # older bindings return (ok, rect)
            ok, r = r
            assert ok
        assert r is not None
        return (int(r.get_x()), int(r.get_y()),
                int(r.get_width()), int(r.get_height()))

    def reg(p, fx, fy, fw, fh):
        """Fractional sub-rect of a (x, y, w, h) region."""
        return "%dx%d+%d+%d" % (int(p[2] * fw), int(p[3] * fh),
                                p[0] + int(p[2] * fx),
                                p[1] + int(p[3] * fy))

    def snap_f(w, key):
        try:
            return float(w.core.snapshot.get(key) or 0.0)
        except (TypeError, ValueError):
            return 0.0

    def clean(w, panscan=None, dwh=None, wh=None):
        """One quiet frame: chrome/OSD/toast hidden + observed truth.

        A synthetic resize can leave GTK with the bottom bar "hovered"
        (measured: set_default_size → bottom_hover True sticks), and
        ChromeState never hides chrome while hovered — settle releases
        the phantom hover (capped, so a genuinely parked pointer cannot
        re-arm the idle countdown forever) exactly like a real leave."""
        st = w.chrome.state
        if st.bottom_hover:
            if state["hover_releases"] >= 8:
                check("hover_never_releases", False, "stuck hover")
                return True               # measure anyway; gate already failed
            state["hover_releases"] += 1
            st.set_bottom_hover(False)
        if w.chrome.bars_visible or w.chrome.osd.get_visible() \
                or w.chrome.toast.get_visible():
            return False
        if dwh is not None and (int(snap_f(w, "dwidth")),
                                int(snap_f(w, "dheight"))) != dwh:
            return False
        if panscan is not None \
                and abs(snap_f(w, "panscan") - panscan) > 1e-6:
            return False
        if wh is not None:
            r = rect(w)
            if r is None or abs(r[2] - wh[0]) > 2 or abs(r[3] - wh[1]) > 2:
                return False
        return True

    def settle(w, then, panscan=None, dwh=None, wh=None, left=200):
        """Poll to one clean frame, then capture + measure."""
        state["settles"] += 1
        if clean(w, panscan, dwh, wh):
            then()
            return False
        if left <= 0:
            check("settle_%d" % state["settles"], False,
                  "bars=%s osd=%s toast=%s snap=%s rect=%s"
                  % (w.chrome.bars_visible, w.chrome.osd.get_visible(),
                     w.chrome.toast.get_visible(),
                     (w.core.snapshot.get("dwidth"),
                      w.core.snapshot.get("dheight")), rect(w)))
            then()                          # keep measuring; check failed
            return False
        GLib.timeout_add(150, lambda: settle(w, then, panscan, dwh, wh,
                                             left - 1))
        return False

    def cap(tag, px=None):
        p = SCRATCH / ("cropwin_%s.xwd" % tag)
        capture_xwd(p)
        if px is not None:
            print("SMOKE_PX %s=%s,%s,%s,%s" % (tag, *px), flush=True)
        return p

    def toggle(on):
        # exactly the palette path: widget-level activate_action resolves
        # the "win" group and toggles the STATEFUL action from there
        w = win()
        if w._crop_action.get_state().get_boolean() != on:
            w.activate_action("win.crop-to-window")
        return w

    # ------------------------------------------------------------- phases
    def p_tall(app):
        w = win()
        w.set_default_size(420, 900)
        GLib.timeout_add(600, lambda: base_wide(app))
        return False

    def base_wide(app):
        w = win()
        settle(w, lambda: base_wide_2(app), panscan=0.0, dwh=(640, 360),
               wh=(420, 900))
        return False

    def base_wide_2(app):
        w = win()
        px = prev_rect(w)
        p = cap("wide_off", px)
        top = measure(p, reg(px, 0.06, 0.04, 0.88, 0.18))
        mid = measure(p, reg(px, 0.22, 0.46, 0.54, 0.08))
        #   ^ GREEN slice only (x .20-.80 of the wide clip): deep blue's
        #     luma sits BELOW the 8% black threshold (measured), so a band
        #     that grazed the blue third would read 14% "black" (r46 run)
        check("wide_off_top_letterbox", top[0] >= 95.0,
              "top black=%.1f%% rgb=(%.0f,%.0f,%.0f)"
              % (top[0], top[1], top[2], top[3]))
        check("wide_off_strip_visible", mid[0] <= 2.0 and mid[2] > 60.0,
              "mid black=%.1f%% rgb=(%.0f,%.0f,%.0f)"
              % (mid[0], mid[1], mid[2], mid[3]))
        GLib.timeout_add(250, lambda: crop_on_wide(app))
        return False

    def crop_on_wide(app):
        w = toggle(True)
        check("wide_crop_on_persist",
              w.cfg.get("video.crop_to_window") is True
              and w._crop_action.get_state().get_boolean())
        GLib.timeout_add(400, lambda: crop_on_wide_2(app))
        return False

    def crop_on_wide_2(app):
        w = win()
        settle(w, lambda: wide_on_measure(app), panscan=1.0, dwh=(640, 360),
               wh=(420, 900))
        return False

    def wide_on_measure(app):
        w = win()
        px = prev_rect(w)
        p = cap("wide_on", px)
        top = measure(p, reg(px, 0.06, 0.04, 0.88, 0.18))
        check("wide_on_top_full_bleed", top[0] <= 2.0,
              "top black=%.1f%%" % top[0])
        check("wide_on_top_is_green_middle", top[2] > 60.0 and top[3] < 60.0,
              "rgb=(%.0f,%.0f,%.0f) — the cover shows the GREEN middle, "
              "red/blue thirds clipped" % (top[1], top[2], top[3]))
        check("wide_on_rect_untouched", rect(w)[2:] == (420, 900),
              str(rect(w)))   # the crop moved PIXELS, never the window
        toggle(False)
        GLib.timeout_add(400, lambda: back_to_bars_wide(app))
        return False

    def back_to_bars_wide(app):
        w = win()
        settle(w, lambda: go_vert(app), panscan=0.0, dwh=(640, 360),
               wh=(420, 900))
        return False

    def go_vert(app):
        w = win()
        px = prev_rect(w)
        p = cap("wide_off2", px)
        top = measure(p, reg(px, 0.06, 0.04, 0.88, 0.18))
        check("wide_off_bars_restored", top[0] >= 95.0,
              "top black=%.1f%%" % top[0])
        w.set_default_size(960, 540)
        w._load_resolved(str(VERT), "replace")   # file change WITH crop off
        GLib.timeout_add(700, lambda: base_vert(app))
        return False

    def base_vert(app):
        w = win()
        settle(w, lambda: crop_on_vert(app), panscan=0.0, dwh=(270, 480),
               wh=(960, 540))
        return False

    def crop_on_vert(app):
        w = win()
        px = prev_rect(w)
        p = cap("vert_off", px)
        colL = measure(p, reg(px, 0.04, 0.20, 0.16, 0.60))
        colR = measure(p, reg(px, 0.80, 0.20, 0.16, 0.60))
        check("vert_off_side_cols_black", colL[0] >= 95.0 and
              colR[0] >= 95.0,
              "L black=%.1f%% R black=%.1f%%" % (colL[0], colR[0]))
        toggle(True)
        GLib.timeout_add(400, lambda: crop_on_vert_2(app))
        return False

    def crop_on_vert_2(app):
        w = win()
        settle(w, lambda: vert_on_measure(app), panscan=1.0, dwh=(270, 480),
               wh=(960, 540))
        return False

    def vert_on_measure(app):
        w = win()
        px = prev_rect(w)
        p = cap("vert_on", px)
        colL = measure(p, reg(px, 0.04, 0.20, 0.16, 0.60))
        colR = measure(p, reg(px, 0.80, 0.20, 0.16, 0.60))
        check("vert_on_cols_full_bleed",
              colL[0] <= 2.0 and colR[0] <= 2.0,
              "L black=%.1f%% R black=%.1f%%" % (colL[0], colR[0]))
        top = measure(p, reg(px, 0.30, 0.12, 0.40, 0.07))
        # cover scale 950/270≈3.5 only ever shows video rows 166..314 —
        # every screen row is inside the GREEN middle; the margins below
        # are strictly anti-contamination, not arithmetic
        bot = measure(p, reg(px, 0.30, 0.89, 0.40, 0.07))
        # visible = green middle only: video's RED top / BLUE bottom clipped
        check("vert_on_top_crops_red",
              top[0] <= 2.0 and top[2] > 60.0 and top[1] < 100.0,
              "top rgb=(%.0f,%.0f,%.0f) black=%.1f%%"
              % (top[1], top[2], top[3], top[0]))
        check("vert_on_bottom_crops_blue",
              bot[0] <= 2.0 and bot[2] > 60.0 and bot[3] < 100.0,
              "bottom rgb=(%.0f,%.0f,%.0f) black=%.1f%%"
              % (bot[1], bot[2], bot[3], bot[0]))
        toggle(False)
        GLib.timeout_add(400, lambda: back_to_bars_vert(app))
        return False

    def back_to_bars_vert(app):
        w = win()
        settle(w, lambda: aspect_interplay(app), panscan=0.0,
               dwh=(270, 480), wh=(960, 540))
        return False

    def aspect_interplay(app):
        w = win()
        px = prev_rect(w)
        p = cap("vert_off2", px)
        colL = measure(p, reg(px, 0.04, 0.20, 0.16, 0.60))
        check("vert_off_bars_restored", colL[0] >= 95.0,
              "L black=%.1f%%" % colL[0])
        # §C: lock ON *while crop ON* — suspend, not fight
        toggle(True)
        w.cfg.set("ui.lock_aspect", True)
        w.on_setting_changed("ui.lock_aspect", True)
        GLib.timeout_add(900, lambda: aspect_resize(app))
        return False

    def aspect_resize(app):
        w = win()
        c0 = w._aspect_corrections
        state["c0"] = c0
        check("crop_on_lock_on_no_refit",
              c0 == 0 and rect(w)[2:] == (960, 540),
              "corrections=%d rect=%s" % (c0, rect(w)))
        w.set_default_size(500, 750)          # a free-form crop-shaped box
        GLib.timeout_add(700, lambda: aspect_snap(app))
        return False

    def aspect_snap(app):
        w = win()
        check("crop_on_free_resize_no_snap",
              w._aspect_corrections == state["c0"]
              and rect(w)[2:] == (500, 750),
              "corrections=%d rect=%s" % (w._aspect_corrections, rect(w)))
        toggle(False)                          # crop OFF re-arms...
        GLib.timeout_add(900, lambda: aspect_done(app))
        return False

    def aspect_done(app):
        w = win()
        r = rect(w)
        want = 270.0 / 480.0
        got = r[2] / float(r[3] or 1)
        check("crop_off_rearms_snaps_once",
              w._aspect_corrections >= state["c0"] + 1
              and abs(got - want) / want < 0.015 and r[2:] != (500, 750),
              "corrections=%d rect=%s aspect=%.3f (video %.3f)"
              % (w._aspect_corrections, r, got, want))
        # a corrected resize flashes the GL surface black (P5 paint quirk):
        # wait for one clean frame at the NEW size before measuring
        settle(w, lambda: aspect_pixels(app, r), panscan=0.0,
               dwh=(270, 480), wh=(r[2], r[3]))
        return False

    def aspect_pixels(app, r, attempt=0):
        w = win()
        px = prev_rect(w)
        p = cap("final", px)
        # the snapped box IS the video's shape: an aspect fit now fills
        # the preview top-to-bottom — its top band (the video's RED rows,
        # red stays above the 8% black threshold) must carry NO letterbox
        band = measure(p, reg(px, 0.20, 0.03, 0.60, 0.12))
        if band[0] > 2.0 and attempt < 8:
            # A corrected resize can still leave the composited window
            # mid-repaint at xwd time (R13 forensics: a sharp ~5% top
            # band of root-black over red — a transient, present with or
            # without chrome motion: 4/6 failures on pristine HEAD, so
            # NOT an R13 regression).  A single "clean frame" state poll
            # does not prove the PIXELS are stable; re-measure until the
            # steady state shows, but a PERSISTENT letterbox still fails
            # after the capped retries — the gate's actual truth.
            print("SMOKE_RETRY final band black=%.1f%% attempt=%d"
                  % (band[0], attempt), flush=True)
            GLib.timeout_add(250, lambda: aspect_pixels(app, r,
                                                        attempt + 1))
            return False
        check("final_aspect_fit_fills", band[0] <= 2.0,
              "top band black=%.1f%% rgb=(%.0f,%.0f,%.0f) attempts=%d"
              % (band[0], band[1], band[2], band[3], attempt))
        # the pre-existing shutdown flake (see module docstring): pause,
        # and WAIT for the pause to be OBSERVED — quitting while a frame
        # is still in flight is what bites (R13 battery forensics: one
        # pass hit it even through the old fixed 500 ms guess).
        w.core.command("set", "pause", "yes")
        GLib.timeout_add(500, lambda: quit_when_still(app, 30))
        return False

    def quit_when_still(app, left):
        w = win()
        if w.core.snapshot.get("pause") is True or left <= 0:
            GLib.timeout_add(300, lambda: quit_now(app))
            return False
        GLib.timeout_add(100, lambda: quit_when_still(app, left - 1))
        return False

    def quit_now(app):
        progress["final"] = True       # the whole phase chain completed
        app.quit()
        return False

    GLib.timeout_add(3800, lambda: p_tall(app))
    return False


def main():
    ensure_clips()
    rc = reelsee.run(["reelsee", str(WIDE)], on_app=sched)
    # an exception inside a phase timer kills the chain but lets the app
    # run to the harness timeout — ASSERTS_PASSED must never be printed
    # over an unfinished run (observed in development, do not re-ship)
    check("phase_chain_complete", progress["final"], str(rc))
    check("rc", rc == 0, str(rc))
    finish()                    # prints the verdict; returns only on pass
    # the shutdown flake (module docstring) can also hang/crash the
    # interpreter teardown AFTER the verdict was printed (R13 battery
    # forensics: one pass killed by the wrapper's timeout over a clean
    # log).  Once every SMOKE_* line is on the wire, exit HARD: the
    # printed verdict is the contract, teardown must not veto it.
    sys.stdout.flush()
    os._exit(0)


if __name__ == "__main__":
    sys.exit(main())
