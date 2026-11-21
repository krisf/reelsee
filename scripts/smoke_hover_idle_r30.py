#!/usr/bin/python3
"""R30 gate: chrome retract under HiDPI fullscreen AND a stalling main loop
(references/ROUND30-BRIEF.md gates 6-7).

The report this round exists for: "the menu still does not retract in full
screen when the cursor is not moving" (user, 0.2.13 — on top of R29, which
closed the scale-1 windowed case).  Two surfaces get locked down here:

  leg 6 — HiDPI fullscreen (GDK_SCALE=2 on a private 2560x1440 Xvfb):
    fullscreen, pointer XTEST-PARKED in the bottom band (the park is the
    ONLY input), held still.  Chrome hidden AND the cursor blanked at the
    GTK seam (a Chrome._blank_cursor spy) within 2 x timeout + margin.
    GDK picks the scale up at display-open, so this leg is a re-exec of
    THIS file with R30_ROLE=hidpi: the real app through the production
    entry (reelsee.run) in a real GDK at scale 2, driving itself through
    GLib callbacks, witnesses in-process — the cursor seam cannot be seen
    from outside the process (R29: the X cursor-query route is a proven
    dead end).

  leg 7 — stalled main loop (in-process; the stall must run ON the app's
    main context, so the harness callback IS the stall): pointer parks,
    then the loop is deliberately stalled TWICE — time.sleep(2.5) from a
    GLib.timeout_add callback, i.e. exactly "a clock discontinuity".
    Pre-D2 EVERY gap > TICK_GAP_MAX_MS re-armed the countdown, so two
    2.5 s stalls bought a fresh timeout each and the chrome was still
    pinned at park + 2 x timeout + margin — RED today by D2.  With D2 the
    first stall is forgiven (R13 control: leg7_mid_visible proves ONE
    isolated stall hides NOTHING), the second consecutive gap does not
    re-arm, and the honest idle deadline fires.

Checks:
  xvfb_up_2560x1440      private screen at HiDPI substrate size
  clip_generated         pure-black 60 s witness clip
  leg6_child_rc          the HiDPI session exited clean
  leg6_child_no_traceback ... with no traceback
  leg6_fs_witness_name   WM name bracketed = fullscreen really happened
  leg6_fs_render_edges   render context re-attached (R27 witness)
  (leg6_child_* pass through the child's own SMOKE_CHECK lines:)
      _shown_parked / _hidden_bottom / _hidden_top / _cursor_blanked /
      _stay_hidden / _motion_reveals
  leg7_playing           app really started playing (hide is playing-only)
  leg7_mid_visible       ONE stall forgiven — chrome still shown (control)
  leg7_hidden_by_deadline hidden by park + 2*timeout + margin (RED pre-D2)
  leg7_cursor_blanked    blank=True edge at the GTK seam after the park
  leg7_stay_hidden       +3 s later still hidden (no resurrection)
  leg7_motion_reveals    genuine motion re-reveals (negative control)
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
VENDOR_LIB = ROOT / "vendor" / "usr" / "lib" / "x86_64-linux-gnu"
CLIP_SECS = "60"
IDLE_TIMEOUT_MS = 3000          # ui.chrome_timeout_ms seeded below
HIDE_GRACE = 1500               # fade tail + render latency (R29 value)
CEILING_DEADLINE = (2 * IDLE_TIMEOUT_MS + HIDE_GRACE) / 1000.0
MID_AFTER = 3.5                 # verdict moment: visible on BOTH trees
STAY_HIDDEN_S = 3.0
PARK_SETTLE = 0.9
POLL_S = 0.25
MEAN_SHOWN_MIN = 25.0           # bottom bar, measured (34.3 shown / 0.0 hidden)
MEAN_SHOWN_MIN_TOP = 12.0       # top strip at GDK_SCALE=2 is dimmer: measured
                                # 18.3 shown / 0.0 hidden (operator row-profile)
MEAN_HIDDEN_MAX = 6.0
STALL_S = 2.5
SCREEN = "2560x1440x24"
SCALE = 2

WORK = SCRATCH / "r30idle"
CFG = WORK / "cfg"
STATE6 = WORK / "state6"
STATE7 = WORK / "state7"
HOME = WORK / "home"
CLIP = WORK / "r30_black.mp4"
LEG6_LOG = WORK / "leg6.log"
for d in (WORK, CFG, STATE6, STATE7, HOME):
    d.mkdir(parents=True, exist_ok=True)
(CFG / "config.json").write_text(json.dumps(
    {"engine": {"decoder": "auto"},
     "ui": {"chrome_timeout_ms": IDLE_TIMEOUT_MS}}))

DISPLAY_NO = ":96"


# ============================================================ raw-X seam
class XDrive:
    """Raw Xlib + XTEST, verbatim contract from smoke_hover_idle_r29.py
    (no WM under Xvfb: WM_CLASS python3/reelsee is the reliable handle;
    XTEST MotionNotify is the REAL input pipeline, XWarpPointer is not)."""

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

    def root_origin(self, win):
        o = win.translate_coords(self.d.screen().root, 0, 0)
        return int(o.x), int(o.y)

    def size(self, win):
        g = win.get_geometry()
        return int(g.width), int(g.height)

    def wm_name(self, win) -> str:
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
            return self.d.get_text_property(
                win, self.d.intern_atom("WM_NAME"))[1] or ""
        except Exception:                           # noqa: BLE001
            return ""

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
        """A REAL MotionNotify through XTEST (root coordinates)."""
        from Xlib.ext import xtest
        xtest.fake_input(self.d, self._X.MotionNotify, x=int(rx), y=int(ry))
        self.d.sync()

    def band_mean(self, win, x: int, y: int, w: int, h: int) -> float:
        """Gray mean of a WINDOW-RELATIVE crop (ZPixmap); the clip is pure
        black, so a bright crop can only be chrome."""
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


def regions(win, x: XDrive):
    """Witness crops MEASURED by the operator at GDK_SCALE=2 on the private
    2560x1440 Xvfb (scratch/r30/diag_out5.txt, DIAG_GRID_SHOWN vs
    DIAG_GRID_HIDDEN_FADED).

    The fullscreen X window measures **1280x720 physical** at GDK_SCALE=2
    (GTK logical 640x360) -- NOT 2560x1440.  An earlier revision of this
    gate doubled the scale-1 row numbers on the assumption that the
    physical window doubles with the scale; it does not, so the top crop
    landed on black video and the leg aborted at its own positive control
    before ever testing retraction.  Measured contrast, shown -> hidden
    after the fade completes:

        top rows 24-52 (w440 centred)   18.32 -> 0.00   <- usable
        top rows 30-50 (w300 centred)   22.25 -> 0.00   <- usable
        bottom rows h-60..h-30          34.27 -> 0.00   <- usable
        top rows  0-12 / 4-24 / 8-20    49.0 / 15.1 / 6.17 UNCHANGED
                                        <- constant CSD edge, never fades:
                                           any crop touching it is vacuous

    Rows 0-24 are therefore excluded.  The hidden reading must be taken
    AFTER the fade-out (the gate polls to a deadline); immediately at the
    visible->hidden edge the bars still read ~28 because GTK is mid-fade.
    """
    w, h = x.size(win)
    bottom = (16, h - 60, w - 32, 30)
    top = (max(0, (w - 440) // 2), 24, 440, 28)
    return bottom, top, w, h


def blank_spy(recorder):
    """Class-level witness on THE GTK cursor seam (same pattern as the
    drag_settle_r29 cursor leg): Chrome._blank_cursor(True) is what blanks
    the pointer; record every edge."""
    import chrome as CH
    orig = CH.Chrome._blank_cursor

    def spy(self, blank: bool):
        recorder.append((time.monotonic(), bool(blank)))
        return orig(self, blank)

    CH.Chrome._blank_cursor = spy


def install_render_log(path: Path) -> None:
    """Mirror the app's logs (render context ready edges) into `path` so
    the fullscreen witness can count them in-process."""
    import logging

    class _Append(logging.Handler):
        def emit(self, record):
            try:
                with open(path, "a") as fh:
                    fh.write(self.format(record) + "\n")
            except OSError:
                pass

    h = _Append()
    h.setFormatter(logging.Formatter("%(message)s"))
    logging.getLogger().addHandler(h)
    logging.getLogger().setLevel(logging.INFO)


def render_edges(path: Path) -> int:
    try:
        return path.read_bytes().count(b"render context ready")
    except OSError:
        return 0


# ================================================== leg 6 (HiDPI, re-exec)

def leg6_main() -> int:
    """Runs in the re-exec'd child: GDK_SCALE=2 is already in the env;
    the app itself runs IN this process via reelsee.run (the production
    entry), driven by GLib callbacks; witnesses in-process."""
    fails: list[str] = []

    def chk(name, ok, value=""):
        print(f"SMOKE_CHECK:leg6_child_{name}={value}", flush=True)
        if not ok:
            fails.append(name)
        return bool(ok)

    import gi  # noqa: F401  (system interpreter; env prepared by parent)
    from gi.repository import GLib

    import reelsee
    import chrome as CH

    render_log = WORK / "leg6_render.log"
    render_log.unlink(missing_ok=True)
    install_render_log(render_log)
    blanks: list[tuple[float, bool]] = []
    blank_spy(blanks)

    xd = XDrive()
    st: dict = {}

    def probe(app):
        def wait_win(t=0):
            w = xd.find_app_window()
            if w is None:
                if t > 80:
                    chk("window_found", False, "no reelsee window on "
                        + DISPLAY_NO)
                    app.quit()
                    return False
                GLib.timeout_add(250, wait_win, t + 1)
                return False
            st["win"] = w
            chk("window_found", True, str(xd.size(w)))
            GLib.timeout_add(2500, fullscreen)
            return False

        def fullscreen():
            before = render_edges(render_log)
            st["edges0"] = before
            xd.focus_and_key(st["win"], "f")
            st["fs_t0"] = time.monotonic()
            GLib.timeout_add(250, wait_fs, 0)
            return False

        def wait_fs(t=0):
            name = xd.wm_name(st["win"])
            bracketed = name.startswith("[")
            if not bracketed and t < 60:
                GLib.timeout_add(250, wait_fs, t + 1)
                return False
            chk("fs_witness_name", bracketed, repr(name[:40]))
            chk("fs_render_edges", render_edges(render_log) > st["edges0"],
                f"{st['edges0']}->{render_edges(render_log)}")
            GLib.timeout_add(700, park)
            return False

        def park():
            win = st["win"]
            bottom, top, w, h = regions(win, xd)
            ox, oy = xd.root_origin(win)
            # physical px: h - 40 at scale 2 = logical h-20 → INSIDE the
            # BOTTOM_HOVER_PX=80 band (the hover surface the user parks on)
            xd.warp(ox + w // 2, oy + h - 40)
            st["t0"] = time.monotonic()
            GLib.timeout_add(int(PARK_SETTLE * 1000), shown, bottom, top)
            return False

        def shown(bottom, top):
            win = st["win"]
            # Anti-vacuity: record the scale the leg ACTUALLY runs at, and
            # the monitor vs window geometry.  A "HiDPI" leg that silently
            # ran at scale 1 would prove nothing about the user's desktop.
            try:
                from gi.repository import Gdk
                disp = Gdk.Display.get_default()
                # GTK4: get_monitors() is a GListModel (get_monitor() was
                # GTK3 and does not exist here -- measured probe_error).
                mons = disp.get_monitors() if disp else None
                mon = mons.get_item(0) if mons and mons.get_n_items() else None
                sc = mon.get_scale_factor() if mon else -1
                geo = mon.get_geometry() if mon else None
                ww, hh = xd.size(win)
                print(f"DIAG_HIDPI gtk_scale={sc} "
                      f"monitor={geo.width if geo else '?'}x"
                      f"{geo.height if geo else '?'} "
                      f"window_x={ww}x{hh} env_GDK_SCALE="
                      f"{os.environ.get('GDK_SCALE')}", flush=True)
            except Exception as e:                      # noqa: BLE001
                print(f"DIAG_HIDPI probe_error={e}", flush=True)
            b0 = xd.band_mean(win, *bottom)
            t0 = xd.band_mean(win, *top)
            ok_b = chk("shown_parked", b0 >= MEAN_SHOWN_MIN, f"{b0:.2f}")
            ok_t = chk("shown_top", t0 >= MEAN_SHOWN_MIN_TOP, f"{t0:.2f}")
            ok = ok_b and ok_t
            if not ok:
                # every later black reading would be vacuous -- dump the
                # measured row profile so a geometry drift is self-evident
                ww, hh = xd.size(win)
                prof = [(r, round(xd.band_mean(win, 0, r, ww, 2), 1))
                        for r in list(range(0, 90, 6))
                        + list(range(hh - 90, hh, 6))]
                print("DIAG_ROW_PROFILE=" + repr(prof), flush=True)
                print(f"DIAG_WIN_SIZE={ww}x{hh}", flush=True)
                GLib.timeout_add(200, lambda: (app.quit(), False)[1])
                return False
            st["deadline"] = st["t0"] + CEILING_DEADLINE
            GLib.timeout_add(500, wait_hidden)
            return False

        def wait_hidden():
            win = st["win"]
            bottom, top, bw, bh = regions(win, xd)
            b = xd.band_mean(win, *bottom)
            t = xd.band_mean(win, *top)
            hidden = b <= MEAN_HIDDEN_MAX and t <= MEAN_HIDDEN_MAX
            if not hidden and time.monotonic() < st["deadline"]:
                GLib.timeout_add(int(POLL_S * 1000), wait_hidden)
                return False
            chk("hidden_bottom", b <= MEAN_HIDDEN_MAX, f"{b:.2f}")
            chk("hidden_top", t <= MEAN_HIDDEN_MAX, f"{t:.2f}")
            if not hidden:
                chk("cursor_blanked", False, "never hid")
                GLib.timeout_add(200, lambda: (app.quit(), False)[1])
                return False
            dt = time.monotonic() - st["t0"]
            edges = [e for e in blanks if e[0] >= st["t0"]]
            blanks_after = [e for e in edges if e[1]]
            chk("cursor_blanked", bool(blanks_after),
                f"hidden@{dt:.2f}s blank_edges={len(blanks_after)}")
            st["stay_t"] = time.monotonic() + STAY_HIDDEN_S
            GLib.timeout_add(500, wait_stay)
            return False

        def wait_stay():
            if time.monotonic() < st["stay_t"]:
                GLib.timeout_add(250, wait_stay)
                return False
            win = st["win"]
            bottom, _, _, _ = regions(win, xd)
            b = xd.band_mean(win, *bottom)
            chk("stay_hidden", b <= MEAN_HIDDEN_MAX, f"{b:.2f}")
            ox, oy = xd.root_origin(win)
            _, _, w, h = regions(win, xd)
            xd.warp(ox + w // 2 + 50, oy + h - 40)  # a REAL motion, in band
            GLib.timeout_add(900, revealed)
            return False

        def revealed():
            win = st["win"]
            bottom, _, _, _ = regions(win, xd)
            b = xd.band_mean(win, *bottom)
            chk("motion_reveals", b >= MEAN_SHOWN_MIN, f"{b:.2f}")
            GLib.timeout_add(200, lambda: (app.quit(), False)[1])
            return False

        GLib.timeout_add(500, wait_win, 0)
        return False

    rc = reelsee.run(["reelsee", str(CLIP)], on_app=probe)
    chk("app_rc", rc == 0, str(rc))
    print("R30_HIDPI_DONE failures=" + (",".join(fails) or "none"),
          flush=True)
    return 1 if fails else 0


# ============================================ leg 7 (stalled loop, parent)

def leg7_main() -> None:
    import gi  # noqa: F401  (DISPLAY/env set by main() before this call)
    from gi.repository import GLib

    import reelsee
    import chrome as CH

    blanks: list[tuple[float, bool]] = []
    blank_spy(blanks)

    xd = XDrive()
    st: dict = {}

    def probe(app):
        def wait_playing(t=0):
            snap = app.windows[0].core.snapshot
            playing = (snap.get("pause") is False
                       and snap.get("idle-active") is False
                       and snap.get("eof-reached") is not True)
            if playing:
                check("leg7_playing", True, "pause=False idle-active=False")
                park()
                return False
            if t >= 80:
                check("leg7_playing", False, repr(snap)[:200])
                app.quit()
                return False
            GLib.timeout_add(250, wait_playing, t + 1)
            return False

        def park():
            w = xd.find_app_window()
            ox, oy = xd.root_origin(w)
            gw, gh = xd.size(w)
            xd.warp(ox + gw // 2, oy + gh - 30)     # bottom band, scale 1
            st["t0"] = time.monotonic()
            GLib.timeout_add(200, stall1)
            GLib.timeout_add(int(MID_AFTER * 1000), verdict)
            return False

        def stall1():
            # deliberate main-loop discontinuity #1 (the brief's recipe)
            time.sleep(STALL_S)
            GLib.timeout_add(5, stall2)             # no healthy tick between
            return False

        def stall2():
            time.sleep(STALL_S)                     # discontinuity #2
            return False

        def verdict():
            # fires DURING stall2's sleep → lands at ~5.2 s on BOTH trees;
            # ONE forgiven re-arm (fixed) or one unconditional re-arm
            # (unfixed) both leave the chrome legitimately SHOWN here —
            # this is the R13 control: a stall must never hide anything.
            c = app.windows[0].chrome
            check("leg7_mid_visible", bool(c.bars_visible or c.fading),
                  f"bars={c.bars_visible} fading={c.fading} "
                  f"dt={time.monotonic() - st['t0']:.2f}s")
            st["deadline"] = st["t0"] + CEILING_DEADLINE
            GLib.timeout_add(250, wait_hidden)
            return False

        def wait_hidden(t=0):
            c = app.windows[0].chrome
            if c.bars_visible or c.fading:
                if time.monotonic() < st["deadline"]:
                    GLib.timeout_add(int(POLL_S * 1000), wait_hidden, t + 1)
                    return False
                check("leg7_hidden_by_deadline", False,
                      f"still shown at {time.monotonic() - st['t0']:.2f}s "
                      f"(deadline {CEILING_DEADLINE:.1f}s — a stalled loop "
                      f"must not re-arm forever)")
                GLib.timeout_add(200, lambda: (app.quit(), False)[1])
                return False
            dt = time.monotonic() - st["t0"]
            check("leg7_hidden_by_deadline", True, f"hidden@{dt:.2f}s")
            check("leg7_reason_hidden", c.state.reason in ("idle", "ceiling"),
                  c.state.reason)
            edges = [e for e in blanks if e[0] >= st["t0"]]
            check("leg7_cursor_blanked", any(e[1] for e in edges),
                  f"blank_edges={sum(1 for e in edges if e[1])}")
            st["stay_t"] = time.monotonic() + STAY_HIDDEN_S
            GLib.timeout_add(500, wait_stay)
            return False

        def wait_stay():
            if time.monotonic() < st["stay_t"]:
                GLib.timeout_add(250, wait_stay)
                return False
            c = app.windows[0].chrome
            check("leg7_stay_hidden", not c.bars_visible,
                  f"bars={c.bars_visible}")
            w = xd.find_app_window()
            ox, oy = xd.root_origin(w)
            gw, gh = xd.size(w)
            xd.warp(ox + gw // 2 - 40, oy + gh - 30)  # REAL motion again
            GLib.timeout_add(500, revealed)
            return False

        def revealed():
            c = app.windows[0].chrome
            check("leg7_motion_reveals", bool(c.bars_visible),
                  f"bars={c.bars_visible}")
            GLib.timeout_add(200, lambda: (app.quit(), False)[1])
            return False

        GLib.timeout_add(800, wait_playing, 0)
        return False

    rc = reelsee.run(["reelsee", str(CLIP)], on_app=probe)
    check("leg7_app_rc", rc == 0, str(rc))


# ================================================================== main

def make_clip() -> bool:
    if CLIP.exists() and CLIP.stat().st_size > 10_000:
        return True
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return False
    r = subprocess.run([
        ffmpeg, "-y", "-f", "lavfi", "-i",
        f"color=c=black:s=1280x720:r=30:d={CLIP_SECS}",
        "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
        "-t", CLIP_SECS, "-c:v", "libx264", "-preset", "veryfast",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(CLIP)],
        stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    return r.returncode == 0


def base_env() -> dict:
    env = os.environ.copy()
    for drop in ("REELSEE_HEADLESS", "WAYLAND_DISPLAY",
                 "DBUS_SESSION_BUS_ADDRESS", "XDG_CURRENT_DESKTOP",
                 "XAUTHORITY", "DISPLAY", "GDK_SCALE", "GDK_BACKEND"):
        env.pop(drop, None)
    # python3-xlib may live in the USER site dir, which python resolves off
    # HOME — with a private HOME the child would lose it (measured: the
    # leg6 traceback).  Name it explicitly; isolation stays.
    import site
    usite = site.getusersitepackages()
    if usite and Path(usite).is_dir():
        env["PYTHONPATH"] = (f"{usite}{os.pathsep}"
                             + env.get("PYTHONPATH", "")).rstrip(os.pathsep)
    env.update({
        "DISPLAY": DISPLAY_NO, "GDK_BACKEND": "x11",
        "GSK_RENDERER": "gl", "GDK_GL": "egl",
        "LIBGL_ALWAYS_SOFTWARE": "1", "GSETTINGS_BACKEND": "memory",
        "HOME": str(HOME), "TMPDIR": str(WORK),
        "LD_LIBRARY_PATH": (f"{VENDOR_LIB}{os.pathsep}"
                            + env.get("LD_LIBRARY_PATH", "")
                            if VENDOR_LIB.is_dir()
                            else env.get("LD_LIBRARY_PATH", "")),
    })
    return env


def main() -> int:
    check("clip_generated", make_clip(), CLIP.name)

    xvfb_log = open(WORK / "xvfb.log", "wb")
    xvfb = subprocess.Popen(
        ["Xvfb", DISPLAY_NO, "-screen", "0", SCREEN, "-nolisten", "tcp"],
        stdout=xvfb_log, stderr=subprocess.STDOUT)
    try:
        xd = None
        end = time.time() + 10
        while time.time() < end and xd is None:
            try:
                xd = XDrive()
            except Exception:                       # noqa: BLE001
                time.sleep(0.2)
        check("xvfb_up_2560x1440", xd is not None, f"{DISPLAY_NO} {SCREEN}")
        if xd is None:
            finish()

        # ---- leg 6: HiDPI fullscreen, real app, re-exec of THIS file ----
        env = base_env()
        env.update({"R30_ROLE": "hidpi", "GDK_SCALE": str(SCALE),
                    "REELSEE_CONFIG_DIR": str(CFG),
                    "REELSEE_STATE_DIR": str(STATE6)})
        STATE6.mkdir(parents=True, exist_ok=True)
        (STATE6 / "window.json").write_text(
            json.dumps({"rect": [0, 0, 1280, 720]}))
        with open(LEG6_LOG, "w") as logfh:
            proc = subprocess.Popen(
                [PY, str(Path(__file__).resolve()), "--leg6-child"],
                cwd=str(ROOT), env=env, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True)
            for line in proc.stdout:
                sys.stdout.write(line)              # SMOKE_CHECKs pass thru
                sys.stdout.flush()
                logfh.write(line)
            rc6 = proc.wait()
        text = LEG6_LOG.read_text(errors="replace")
        check("leg6_child_rc", rc6 == 0, f"rc={rc6}")
        check("leg6_child_no_traceback", "Traceback" not in text,
              "clean" if "Traceback" not in text else "TRACEBACK (see log)")

        # ---- leg 7: stalled main loop, in-process ------------------------
        env7 = base_env()
        env7.update({"REELSEE_CONFIG_DIR": str(CFG),
                     "REELSEE_STATE_DIR": str(STATE7)})
        STATE7.mkdir(parents=True, exist_ok=True)
        (STATE7 / "window.json").write_text(
            json.dumps({"rect": [0, 0, 1280, 720]}))
        os.environ.clear()
        os.environ.update(env7)
        leg7_main()
    finally:
        xvfb.terminate()
        try:
            xvfb.wait(timeout=5)
        except subprocess.TimeoutExpired:
            xvfb.kill()
    finish()
    return 0


if __name__ == "__main__":
    if os.environ.get("R30_ROLE") == "hidpi":
        sys.path.insert(0, str(ROOT / "scripts"))
        sys.exit(leg6_main())
    sys.exit(main())
