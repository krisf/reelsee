#!/usr/bin/python3
"""R15-C gate: the top chrome sits FLUSH with the window's top edge.

Three claims, three measurements:

1. GEOMETRY (authoritative): every margin of the top bar is 0; the
   bar's bounds inside the overlay start at (0,0); its width equals the
   window's client width (±1 px).  Margins are read with
   get_margin_*, position via compute_bounds(overlay) — no allocation
   accessors exist on this GTK build.

2. LAYOUT INVARIANT (round 12 contract, re-checked with flush chrome):
   revealing and hiding the chrome must not move the video by one
   pixel — (view-area, overlay, surface) numbers byte-identical.

3. PIXELS ("no transparent rim at the top row"): root screenshots of
   the SAME video rows with chrome shown vs hidden.  Hidden rows 0..2
   show bare video (bright); with the flush bar revealed they must drop
   to the dark chrome veil (≤ 45 % of the hidden brightness).  While
   the old 4/6 margins were in place those rows were transparent
   overlay over video and the ratio sat at ≈1.0 — the gate's own
   negative control.  The crop's root position is CALIBRATED at
   runtime (motion spy: warp, read overlay-local coords, back out the
   overlay's root origin — the CSD border offset is never hardcoded).

Deliberately AVOIDS fullscreen: under Xvfb + EGL, entering/leaving
fullscreen breaks XGetImage/xwd capture of the GL area afterwards
(established by bisect capture in round 15 — probe scratch/probe_r15c
chain), and fullscreen is not what this gate is about.  Chrome hides
the honest way: playing + no pointer motion + a seeded short
ui.chrome_timeout_ms.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))
from smoke_harness import SCRATCH, check, finish  # noqa: E402

CFG = SCRATCH / f"r15top-cfg-{os.getpid()}"
STATE = SCRATCH / f"r15top-state-{os.getpid()}"
SHOT = SCRATCH / f"r15top-{os.getpid()}"
CLIP = "testdata/perf_1080p30.mp4"
S: dict = {}


def mean(xwd: str, crop: str) -> float:
    out = subprocess.run(
        ["convert", xwd, "-crop", crop, "+repage", "-colorspace", "gray",
         "-format", "%[fx:mean*255]", "info:"],
        capture_output=True, text=True)
    return float(out.stdout.strip() or 0)


def main() -> int:
    for d in (CFG, STATE, SHOT):
        d.mkdir(parents=True, exist_ok=True)
    (CFG / "config.json").write_text(json.dumps(
        {"ui": {"chrome_timeout_ms": 900}}), encoding="utf-8")
    os.environ["REELSEE_CONFIG_DIR"] = str(CFG)
    os.environ["REELSEE_STATE_DIR"] = str(STATE)

    import gi
    gi.require_version("Gtk", "4.0")
    from gi.repository import GLib

    import reelsee

    def schedule(app):
        S["app"] = app

        def win():
            return app.windows[0]

        def guard(fn):
            def run(*a):
                try:
                    return fn(*a)
                except Exception as exc:            # noqa: BLE001
                    import traceback
                    traceback.print_exc()
                    check(fn.__name__ + "_crashed", False, repr(exc))
                    app.quit()
                    return False
            run.__name__ = fn.__name__
            return run

        def again(fn, ms=80):
            GLib.timeout_add(ms, fn)
            return False

        def wait_for(desc, pred, then, left=60):
            try:
                val = pred()
            except Exception:                       # noqa: BLE001
                val = None
            if val:
                check(desc, True, "ok" if val is True else str(val))
                return again(then)
            if left <= 0:
                check(desc, False, "timeout")
                app.quit()
                return False
            return again(lambda: wait_for(desc, pred, then, left - 1), 120)

        def fades():
            w = win()
            return (w.chrome.bottom.get_opacity(),
                    w.top_bar.get_opacity())

        def opac():
            return max(fades())

        def retracted():
            return max(fades()) < 0.05

        def video_alloc():
            w = win()
            surf = w.get_surface()
            return (w.view.area.get_width(), w.view.area.get_height(),
                    w.overlay.get_width(), w.overlay.get_height(),
                    surf.get_width(), surf.get_height())

        # -- X plumbing for the pixel phase ----------------------------
        def xopen():
            import Xlib.display
            S["xd"] = Xlib.display.Display(os.environ.get("DISPLAY", ":0"))

        def xfind():
            from Xlib import X
            d = S["xd"]
            root = d.screen().root
            found = [None]
            big = [None, 0]

            def name_of(w):
                try:
                    n = w.get_wm_name()
                    return n.decode("utf-8", "replace") if isinstance(
                        n, bytes) else (n or None)
                except Exception:                   # noqa: BLE001
                    return None

            def mapped(w):
                try:
                    return w.get_attributes().map_state == X.IsViewable
                except Exception:                   # noqa: BLE001
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
                    except Exception:               # noqa: BLE001
                        area = 0
                    if g.width >= 800 and g.height >= 400 and (
                            big[0] is None or area > big[1]):
                        big[0], big[1] = w, area
                try:
                    kids = w.query_tree().children
                except Exception:                   # noqa: BLE001
                    kids = []
                for k in kids:
                    walk(k, depth + 1)
            walk(root)
            return found[0] or big[0]

        def wpos():
            g = S["winx"].get_geometry()
            tr = S["winx"].translate_coords(S["xd"].screen().root, 0, 0)
            return tr.x, tr.y, g.width, g.height

        def warp(x, y):
            S["xd"].screen().root.warp_pointer(x, y)
            S["xd"].sync()

        def xwd(name):
            p = SHOT / f"{name}.xwd"
            with open(p, "wb") as fh:
                subprocess.run(["xwd", "-root", "-silent"], stdout=fh,
                               check=True)
            return str(p)

        # -- calibration: where is widget (0,0) on screen? --------------
        def calib_attach():
            from gi.repository import Gtk
            ov = win().overlay
            ctl = Gtk.EventControllerMotion()

            def on_motion(_ctl, x, y):
                S["mo"] = (x, y)
            ctl.connect("motion", on_motion)
            ov.add_controller(ctl)
            S["ov_ctl"] = ctl            # keep referenced
            return again(guard(calib_warp1), 150)

        def calib_warp1(left=40):
            if "xd" not in S:
                xopen()
            w = xfind()
            if w is None:
                if left <= 0:
                    check("x_window_found", False, "no X window")
                    app.quit()
                    return False
                return again(lambda: calib_warp1(left - 1), 250)
            if "winx" not in S:
                check("x_window_found", True, str(w.id))
            S["winx"] = w
            wx, wy, _, _ = wpos()
            S["warp"] = (wx + 500, wy + 300)
            warp(*S["warp"])
            return again(guard(calib_read), 200)

        def calib_read(left=25):
            if "mo" not in S:
                warp(*S["warp"])        # event may predate the handler
                return again(lambda: calib_read(left - 1), 160)
            lx, ly = S["mo"]
            rx, ry = S["warp"]
            S["ov_root"] = (rx - lx, ry - ly)
            wx, wy, _, _ = wpos()
            ox, oy = S["ov_root"]
            # the overlay origin must be within a handful of px of the
            # X window origin (invisible CSD border); sanity only
            check("calib_overlay_root_sane",
                  abs(ox - wx) <= 8 and abs(oy - wy) <= 8,
                  f"win=({wx},{wy}) ov_root=({ox},{oy})")
            return again(guard(start_video))

        # -- the measurement run -----------------------------------------
        def start_video():
            w = win()
            margins = (w.top_bar.get_margin_top(),
                       w.top_bar.get_margin_start(),
                       w.top_bar.get_margin_end(),
                       w.top_bar.get_margin_bottom())
            check("c_margins_all_zero", margins == (0, 0, 0, 0),
                  f"top={margins[0]} start={margins[1]} end={margins[2]} "
                  f"bottom={margins[3]}")
            r = w.top_bar.compute_bounds(w.overlay)
            rect = r[1]
            ax, ay = rect.get_x(), rect.get_y()
            S["acc"] = (ax, ay)
            check("c_bar_at_overlay_origin",
                  abs(ax) <= 1.0 and abs(ay) <= 1.0,
                  f"({ax:.1f},{ay:.1f})")
            bw = w.top_bar.get_width()
            check("c_bar_width_is_window_width",
                  abs(bw - w.get_width()) <= 1,
                  f"bar={bw} window={w.get_width()}")
            check("c_bar_width_is_overlay_width",
                  abs(bw - w.overlay.get_width()) <= 1,
                  f"bar={bw} overlay={w.overlay.get_width()}")
            check("c_bar_reaches_both_edges",
                  abs(ax + bw - w.overlay.get_width()) <= 1
                  and abs(ax) <= 1.0,
                  f"x0={ax:.1f} w={bw} ov_w={w.overlay.get_width()}")
            w._load_path(CLIP)
            return again(guard(playing))

        def playing(left=80):
            s = win().core.snapshot
            if s.get("pause") is False and (s.get("time-pos") or 0) > 0.5:
                # brightness lands on NEW frames only — set it while the
                # video is actually moving, then pause the boosted frame
                win().core.command("set", "brightness", "80")
                return again(guard(boosted), 400)
            if left <= 0:
                check("c_playing", False, "timeout")
                app.quit()
                return False
            return again(lambda: playing(left - 1), 120)

        def boosted():
            win().core.set_property("pause", True)
            return again(guard(revealed), 350)

        def revealed():
            w = win()
            w.chrome.motion(w.overlay.get_width() / 2.0, 300.0,
                            float(w.overlay.get_width()),
                            float(w.overlay.get_height()))
            return wait_for("c_revealed", lambda: (
                w.top_bar.get_opacity() > 0.95), guard(shown_shot),
                left=40)

        def shown_shot():
            S["alloc_shown"] = video_alloc()
            S["shown"] = xwd("shown")
            return again(guard(park_and_play), 200)

        def park_and_play():
            wx, wy, _, _ = wpos()
            warp(wx + 400, wy + 20)     # park over the (now revealed)
            win().core.set_property("pause", False)   # header: idle rule
            return wait_for("c_retracted", retracted, guard(hidden_shot),
                            left=70)

        def hidden_shot():
            S["alloc_hidden"] = video_alloc()
            S["hidden"] = xwd("hidden")
            check("c_video_alloc_identical",
                  S["alloc_shown"] == S["alloc_hidden"],
                  f"shown={S['alloc_shown']} hidden={S['alloc_hidden']}")
            return again(guard(rim))

        def rim():
            ox, oy = S["ov_root"]
            ax, ay = S["acc"]
            w = win()
            left = int(ox + 30)
            # the strip is the WINDOW's top rows (widget y 0..2) — NOT
            # the bar's own rows: with the old margins the bar sat at
            # y=4 and rows 0..2 were transparent overlay over video,
            # which is exactly what this check must catch
            top = int(round(oy))
            crop_top = f"{w.top_bar.get_width() - 60}x3+{left}+{top}"
            crop_mid = f"{w.top_bar.get_width() - 60}x20+{left}+" \
                       f"{int(round(oy + ay + 8))}"   # inside the bar
            h_top = mean(S["hidden"], crop_top)
            s_top = mean(S["shown"], crop_top)
            h_mid = mean(S["hidden"], crop_mid)
            s_mid = mean(S["shown"], crop_mid)
            print(f"RIM: top3 hidden={h_top:.1f} shown={s_top:.1f} "
                  f"crop={crop_top}")
            print(f"RIM: bar rows hidden={h_mid:.1f} shown={s_mid:.1f} "
                  f"crop={crop_mid}")
            check("c_hidden_rows_bright", h_top >= 30.0,
                  f"hidden top mean {h_top:.1f} (capture dead?)")
            check("c_no_rim_top_row", h_top > 0 and s_top <= 0.45 * h_top,
                  f"shown {s_top:.1f} vs hidden {h_top:.1f} "
                  f"(ratio {s_top / h_top:.3f})")
            check("c_veil_darkens_bar_rows", h_mid > 0
                  and s_mid <= 0.6 * h_mid,
                  f"shown {s_mid:.1f} vs hidden {h_mid:.1f}")
            app.quit()
            return False

        def start(left=60):
            try:
                w = win()
            except IndexError:
                w = None
            if w is None or not w.get_mapped():
                if left <= 0:
                    check("window_realized", False, "never realized")
                    app.quit()
                    return False
                return again(lambda: start(left - 1), 200)
            check("window_realized", True)
            return again(guard(calib_attach), 600)

        GLib.timeout_add(400, guard(start))

    rc = reelsee.run(["reelsee", CLIP], on_app=schedule)
    check("run_rc", rc == 0, str(rc))
    for d in (CFG, STATE, SHOT):
        import shutil
        shutil.rmtree(d, ignore_errors=True)
    finish()
    return 0


if __name__ == "__main__":
    sys.exit(main())
