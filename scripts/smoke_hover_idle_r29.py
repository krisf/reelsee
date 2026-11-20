#!/usr/bin/python3
"""R29 gate: a STATIONARY pointer parked in the bottom hover band must still
lose the chrome (and the cursor) at the idle timeout — in the REAL APP, OUT
OF PROCESS (references/ROUND29-BRIEF.md §B1).

Measured defect (D1): `ChromeState.tick`'s `elif self.bottom_hover:
_show("hover")` branch bypasses the idle test entirely, so a pointer resting
in the bottom 80 px band — exactly where a cursor parks in fullscreen — pins
the chrome AND (downstream) the cursor visible forever.  Operator evidence:
`vis=True reason=hover idle_ms=13822 … fs=True`, never blanking; the centre
control hides at 3000 ms.

The in-process gates cannot see this class of bug (the R27 lesson): they all
drive the pure state machine or the in-app seam.  This gate is the real
shape:

  * its OWN private Xvfb :98 (1280x800x24) started here, stopped in finally;
    the battery's headless-smoke.sh display is NEVER touched;
  * the REAL binary: /usr/bin/python3 src/reelsee.py <clip>, NO REELSEE_HEADLESS,
    private config/state dirs, vendored LD_LIBRARY_PATH;
  * an ffmpeg PURE-BLACK clip (color=c=black + silent anullsrc, libx264
    yuv420p) so any non-black pixel inside the window IS chrome;
  * window found via WM_CLASS (never by title), window rect READ from X
    (get_geometry + translate_coords — never assumed);
  * fullscreen via the XTEST `f` keysym, witnessed by _NET_WM_NAME/WM_NAME
    starting with `[` (the R23 `reelsee.run(argv)` bracketed-title contract:
    `[file]` fullscreen vs `file` windowed — measured empty WM_NAME/UTF-8
    _NET_WM_NAME pair under this exact Xvfb build, so the witness reads the
    ICCCM name pair, preferring _NET_WM_NAME) plus the app log's
    `render context ready` EDGE count (R27's proven witness);
  * pointer parked with XTEST MotionNotify at window-relative
    (w//2, height-20) — inside BOTTOM_HOVER_PX=80;
  * witnesses are Xlib GetImage means of two window-relative crops: the
    bottom-bar band and the top-strip centre.

CHECKS per leg (fullscreen leg + windowed leg):
  leg_shown_*            — the parking motion REVEALED the chrome (positive
                           control: a clip that never paints is caught here)
  leg_hidden_in_band_*   — at timeout+1500 ms with NO input at all both
                           crops are BLACK again  (THE pre-fix RED: the
                           hover branch pins them forever)
  leg_stay_hidden_*      — 3 s later still black (R13: idleness never
                           resurrects)
  leg_motion_reshows_*   — a small real XTEST motion in the band re-reveals

The cursor is NOT witnessed over X (XFixes GetCursorName reports the theme
cursor regardless of the app's blanking — a proven dead end); the cursor
witness is the _blank_cursor spy inside scripts/smoke_drag_settle_r29.py.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from smoke_harness import ROOT, SCRATCH, check, finish

PY = "/usr/bin/python3"
DISPLAY_NO = ":98"
VENDOR_LIB = ROOT / "vendor" / "usr" / "lib" / "x86_64-linux-gnu"

WORK = SCRATCH / "r29hi"
CFG_DIR = WORK / "cfg"
STATE_DIR = WORK / "state"
HOME_DIR = WORK / "home"
APP_LOG = WORK / "app.log"
CLIP = WORK / "r29_blackclip.mp4"
SEEDED_RECT = [0, 0, 1280, 720]

CLIP_SECS = "60"          # every leg + the RED path's full budget stays far
#                           inside playback: an EOF flips playing=False and the
#                           chrome would then legitimately stay up (the gate
#                           must never test against a finished file)

IDLE_TIMEOUT_MS = 3000    # ui.chrome_timeout_ms default (config.json seeds
#                           nothing else; a config override is not this gate's
#                           subject — the DEFAULT is the user's desktop)

# pixel thresholds, measured on this exact Xvfb (rows probed in the R29 dev
# pass): hidden band ≈ 1.0-2.5 (pure-black video ± the pointer sprite),
# revealed bar band ≈ 15-25 mean, revealed top-strip centre ≈ 6-15.  Margins
# on both sides of every threshold:
MEAN_SHOWN_MIN = 6.0
MEAN_HIDDEN_MAX = 3.5

# timing margins
T_WINDOW = 30.0           # app window mapped + found
T_NAME = 8.0              # WM-name edge after the keypress
PARK_SETTLE = 0.9         # fade (180 ms) + tick cadence after a motion
HIDE_GRACE = 1500.0       # the brief's own margin past the timeout
STAY_HIDDEN_S = 3.0
POLL_S = 0.25


# ------------------------------------------------------------------ X layer
class XDrive:
    """Private-display driver: WM_CLASS lookup, XTEST keys + pointer warps,
    window-relative GetImage means.  Same lookup contract as
    smoke_fullscreen_real.py (no WM under Xvfb: WM_NAME-based LOOKUP finds
    nothing; WM_CLASS containing python3/reelsee is the reliable handle)."""

    def __init__(self):
        from Xlib import X, display as xdisplay     # system python3-xlib
        self._X = X
        self.d = xdisplay.Display(DISPLAY_NO)
        self._net_wm_name = self.d.intern_atom("_NET_WM_NAME")

    def find_app_window(self):
        root = self.d.screen().root
        stack, tried = [root], 0
        while stack and tried < 256:
            w = stack.pop()
            tried += 1
            try:
                cls = w.get_wm_class()
            except Exception:                       # noqa: BLE001
                continue
            if cls and any(needle in (cls[0] + " " + cls[1]).lower()
                           for needle in ("python3", "reelsee")):
                try:
                    if w.get_attributes().map_state == self._X.IsViewable:
                        return w
                except Exception:                   # noqa: BLE001
                    continue
            try:
                stack.extend(w.query_tree().children)
            except Exception:                       # noqa: BLE001
                pass
        return None

    # ---- window facts, READ from the server (never assumed) --------------
    def root_origin(self, win):
        o = win.translate_coords(self.d.screen().root, 0, 0)
        return int(o.x), int(o.y)

    def size(self, win):
        g = win.get_geometry()
        return int(g.width), int(g.height)

    def wm_name(self, win) -> str:
        """The ICCCM name a WM would display: prefer _NET_WM_NAME (this GTK
        build leaves the legacy STRING WM_NAME empty under Xvfb — measured),
        fall back to WM_NAME.  decode() tolerates a surrogate-laced str
        reply from python3-xlib."""
        try:
            p = win.get_full_property(self._net_wm_name, self._X.AnyPropertyType)
            if p is not None and p.value:
                raw = p.value
                if isinstance(raw, bytes):
                    return raw.decode("utf-8", "replace")
                if not isinstance(raw, str):
                    raw = bytes(raw).decode("utf-8", "replace")
                return raw
        except Exception:                           # noqa: BLE001
            pass
        try:
            n = win.get_wm_name()
        except Exception:                           # noqa: BLE001
            return ""
        if isinstance(n, bytes):
            return n.decode("utf-8", "replace")
        return str(n or "")

    # ---- input, exclusively through XTEST ---------------------------------
    def focus_and_key(self, win, keysym_name: str) -> None:
        from Xlib import XK
        from Xlib.ext import xtest
        kc = self.d.keysym_to_keycode(XK.string_to_keysym(keysym_name))
        if not kc:
            raise RuntimeError(f"no keycode for keysym {keysym_name!r}")
        self.d.set_input_focus(win, self._X.RevertToParent, self._X.CurrentTime)
        self.d.sync()
        time.sleep(0.05)
        xtest.fake_input(self.d, self._X.KeyPress, kc)
        xtest.fake_input(self.d, self._X.KeyRelease, kc)
        self.d.sync()

    def warp(self, rx: int, ry: int) -> None:
        """A REAL MotionNotify through XTEST (the XFixes route is a proven
        dead end for cursor witnesses; motion witnesses need the real event
        pipeline: GDK → the window's motion controller → ChromeState)."""
        from Xlib.ext import xtest
        xtest.fake_input(self.d, self._X.MotionNotify, x=int(rx), y=int(ry))
        self.d.sync()

    # ---- pixel witnesses --------------------------------------------------
    def band_mean(self, win, x: int, y: int, w: int, h: int) -> float:
        """Gray mean of a WINDOW-RELATIVE crop via Xlib GetImage (ZPixmap).
        The clip is PURE BLACK, so a bright crop can only be chrome."""
        try:
            img = win.get_image(x, y, w, h, self._X.ZPixmap, 0xFFFFFFFF)
        except Exception:                           # noqa: BLE001
            return -1.0
        data = img.data
        if isinstance(data, str):
            data = data.encode("latin-1", "ignore")
        b = memoryview(data).cast("B")
        stride = 4 * 5                              # every 5th px (32bpp)
        tot = cnt = 0
        for i in range(0, len(b) - 2, stride):
            tot += b[i] + b[i + 1] + b[i + 2]
            cnt += 1
        return tot / (cnt * 3.0) if cnt else -1.0


# ---------------------------------------------------------------- geometry
def regions(win, x: XDrive):
    """The two witness crops, in WINDOW-RELATIVE px, derived from the size
    READ from the server.  (rows measured on this build: the reveal bar
    occupies y ∈ [h-29, h-19], the top strip y ∈ [5, 32]; rows 0-4 and the
    outer 4 px are a CONSTANT CSD edge that never fades, so every crop
    excludes them.)"""
    w, h = x.size(win)
    bottom = (8, h - 48, w - 16, 36)
    top = ((w - 440) // 2, 6, 440, 28)
    return bottom, top, w, h


def leg(x: XDrive, win, tag: str) -> None:
    """Brief §B1 steps 3-7 for the CURRENT window state (fullscreen leg and
    the windowed leg run the identical body)."""
    bottom, top, w, h = regions(win, x)
    ox, oy = x.root_origin(win)
    park_x, park_y = ox + w // 2, oy + h - 20      # window-relative h-20:
    #                                                INSIDE BOTTOM_HOVER_PX=80
    x.warp(park_x, park_y)
    time.sleep(PARK_SETTLE)
    b0 = x.band_mean(win, *bottom)
    t0 = x.band_mean(win, *top)
    ok = check(f"{tag}_shown_bottom", MEAN_SHOWN_MIN <= b0, f"{b0:.2f}") and \
        check(f"{tag}_shown_top", MEAN_SHOWN_MIN <= t0, f"{t0:.2f}")
    if not ok:
        # The parking motion must reveal BOTH groups; without that every
        # later "black" reading is vacuous — stop this leg.
        return

    # NO INPUT AT ALL from here.  The sample lands at timeout+1500 ms from
    # the parking motion (+0.4 s for the 180 ms fade tail; the 0.9 s settle
    # already spent counts): the brief's own deadline.
    time.sleep((IDLE_TIMEOUT_MS + HIDE_GRACE) / 1000.0 + 0.4 - PARK_SETTLE)
    b1 = x.band_mean(win, *bottom)
    t1 = x.band_mean(win, *top)
    hid = b1 <= MEAN_HIDDEN_MAX and t1 <= MEAN_HIDDEN_MAX
    if not hid:                                     # small poll for CI
        end = time.time() + 1.5                     # jitter tolerance only
        while time.time() < end and not hid:
            time.sleep(POLL_S)
            b1 = x.band_mean(win, *bottom)
            t1 = x.band_mean(win, *top)
            hid = b1 <= MEAN_HIDDEN_MAX and t1 <= MEAN_HIDDEN_MAX
    check(f"{tag}_hidden_in_band_bottom", b1 <= MEAN_HIDDEN_MAX, f"{b1:.2f}")
    check(f"{tag}_hidden_in_band_top", t1 <= MEAN_HIDDEN_MAX, f"{t1:.2f}")

    # STAYS hidden 3 s more (idleness must never resurrect — R13)
    time.sleep(STAY_HIDDEN_S)
    b2 = x.band_mean(win, *bottom)
    t2 = x.band_mean(win, *top)
    check(f"{tag}_stay_hidden_bottom", b2 <= MEAN_HIDDEN_MAX, f"{b2:.2f}")
    check(f"{tag}_stay_hidden_top", t2 <= MEAN_HIDDEN_MAX, f"{t2:.2f}")

    # a small REAL motion (still inside the band) re-reveals everything
    x.warp(park_x + 25, park_y)
    time.sleep(PARK_SETTLE)
    b3 = x.band_mean(win, *bottom)
    t3 = x.band_mean(win, *top)
    check(f"{tag}_motion_bottom", MEAN_SHOWN_MIN <= b3, f"{b3:.2f}")
    check(f"{tag}_motion_top", MEAN_SHOWN_MIN <= t3, f"{t3:.2f}")


# ------------------------------------------------------------------ driver
def gen_clip() -> bool:
    """PURE-BLACK 1280x720 clip with SILENT AUDIO (brief §B1): any non-black
    pixel inside the window during a leg can then only be chrome."""
    CLIP.parent.mkdir(parents=True, exist_ok=True)
    if CLIP.exists():
        CLIP.unlink()
    r = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "color=c=black:s=1280x720:r=30",
         "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
         "-t", CLIP_SECS, "-c:v", "libx264", "-preset", "veryfast",
         "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(CLIP)],
        capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stderr[:400])
    return CLIP.exists() and CLIP.stat().st_size > 10_000


def main() -> int:
    if WORK.exists():
        shutil.rmtree(WORK, ignore_errors=True)
    for d in (CFG_DIR, STATE_DIR, HOME_DIR):
        d.mkdir(parents=True)
    (CFG_DIR / "config.json").write_text(
        '{\n  "engine": {"decoder": "auto"}\n}\n')
    (STATE_DIR / "window.json").write_text(json.dumps({"rect": SEEDED_RECT}))
    check("clip_generated", gen_clip(), CLIP.name)

    xvfb = subprocess.Popen(
        ["Xvfb", DISPLAY_NO, "-screen", "0", "1280x800x24", "-nolisten", "tcp"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        x = None
        deadline = time.time() + 8.0
        while time.time() < deadline and x is None:
            try:
                x = XDrive()
            except Exception:                       # noqa: BLE001
                time.sleep(0.2)
        check("xvfb_up", x is not None, DISPLAY_NO)
        if x is None:
            finish()

        # ---- the REAL app, out of process, NO headless switch -------------
        env = os.environ.copy()
        for drop in ("REELSEE_HEADLESS", "WAYLAND_DISPLAY",
                     "DBUS_SESSION_BUS_ADDRESS", "XDG_CURRENT_DESKTOP",
                     "XAUTHORITY", "DISPLAY"):
            env.pop(drop, None)
        env.update({
            "DISPLAY": DISPLAY_NO,
            "GDK_BACKEND": "x11",
            "REELSEE_STATE_DIR": str(STATE_DIR),
            "REELSEE_CONFIG_DIR": str(CFG_DIR),
            "HOME": str(HOME_DIR),
            "TMPDIR": str(WORK),
            "LD_LIBRARY_PATH": (f"{VENDOR_LIB}{os.pathsep}"
                                + env.get("LD_LIBRARY_PATH", "")
                                if VENDOR_LIB.is_dir()
                                else env.get("LD_LIBRARY_PATH", "")),
            "GSK_RENDERER": "gl", "GDK_GL": "egl",
            "LIBGL_ALWAYS_SOFTWARE": "1",
            "GSETTINGS_BACKEND": "memory",
        })
        logfh = open(APP_LOG, "wb")
        app = subprocess.Popen(
            [PY, str(ROOT / "src" / "reelsee.py"), str(CLIP)],
            cwd=str(ROOT), env=env, stdout=logfh, stderr=subprocess.STDOUT)
        try:
            run_legs(app, x)
        finally:
            if app.poll() is None:
                app.terminate()
                try:
                    app.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    app.kill()
                    app.wait()
            logfh.close()
        check("app_exited_cleanly", app.poll() is not None, str(app.poll()))
    finally:
        xvfb.terminate()
        try:
            xvfb.wait(timeout=5)
        except subprocess.TimeoutExpired:
            xvfb.kill()
    finish()
    return 0


def edges() -> int:
    try:
        return APP_LOG.read_bytes().count(b"render context ready")
    except OSError:
        return 0


def run_legs(app, x: XDrive) -> None:
    win = None
    deadline = time.time() + T_WINDOW
    while time.time() < deadline and win is None:
        if app.poll() is not None:
            break
        win = x.find_app_window()
        if win is None:
            time.sleep(0.25)
    check("window_found", win is not None,
          "WM_CLASS python3/reelsee mapped on " + DISPLAY_NO)
    if win is None or app.poll() is not None:
        print((APP_LOG.read_text(errors="replace")
               if APP_LOG.exists() else "")[-2000:])
        return
    time.sleep(2.5)                                 # first file resolves

    # ---- brief step 2: the windowed name must NOT be bracketed ------------
    name0 = x.wm_name(win)
    check("name_unbracketed_windowed",
          bool(name0) and not name0.startswith("["), repr(name0))

    # ---- fullscreen via the XTEST keysym, TWO witnesses --------------------
    before = edges()
    x.focus_and_key(win, "f")
    end = time.time() + T_NAME
    name1 = ""
    while time.time() < end:
        name1 = x.wm_name(win)
        if name1.startswith("["):
            break
        time.sleep(0.2)
    check("name_bracketed_fullscreen", name1.startswith("["), repr(name1))
    e1 = edges()
    check("fs_render_edge_observed", e1 > before, f"{before}->{e1}")
    # (the WM-less Xvfb keeps the window size — the name edge is the only
    #  fullscreen witness that can distinguish the two states here)

    # ---- brief steps 3-8: the fullscreen leg -------------------------------
    leg(x, win, "fs")

    # ---- brief step 9: the windowed leg (leave fullscreen, same body) ------
    before2 = edges()
    x.focus_and_key(win, "f")
    end = time.time() + T_NAME
    name2 = name1
    while time.time() < end:
        name2 = x.wm_name(win)
        if name2 and not name2.startswith("["):
            break
        time.sleep(0.2)
    check("name_unbracketed_after_restore",
          bool(name2) and not name2.startswith("["), repr(name2))
    check("restore_render_edge_observed", edges() > before2,
          f"{before2}->{edges()}")
    time.sleep(1.0)                                 # the restore edge settles
    leg(x, win, "win")


if __name__ == "__main__":
    sys.exit(main())
