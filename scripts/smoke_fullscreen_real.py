#!/usr/bin/python3
"""R27 gate: the fullscreen cycle in the REAL APP, OUT OF PROCESS (§2).

Why this gate exists: `smoke_render_cycle.py` drives the exact same cycle
IN-PROCESS and PASSES (luma_after_double_toggle=131.8) while the real app —
launched through `reelsee.run()`'s production entry point from outside —
shows video windowed (luma ~58) and goes BLACK on fullscreen (~1.5) and is
STILL BLACK 15 s after exiting.  The difference is the harness shape:
every battery smoke runs inside `headless-smoke.sh` with REELSEE_HEADLESS=1
and the in-process `reelsee.run(argv, on_app=…)` seam, which never exercises
the real app lifecycle.  This gate is the missing shape:

  * a PRIVATE Xvfb :97 (1280x800x24) that this script starts and stops in a
    `finally` — never the operator's display;
  * the app launched as a real subprocess (`/usr/bin/python3 src/reelsee.py
    <clip>`) with NO REELSEE_HEADLESS, its own config/state dirs, vendored
    LD_LIBRARY_PATH;
  * the window FOUND BY WM_CLASS CONTAINING 'python3' (there is no WM under
    Xvfb: WM_NAME is empty, a title lookup finds nothing — measured);
  * fullscreen driven with the `f` keysym through XTEST — the real keymap
    path, never `win._actions`;
  * luma measured with `xwd -root -display :97` + ImageMagick gray mean;
  * TWO WITNESSES THAT MUST AGREE: the X capture AND the app's own `s`
    screenshot (mpv's own output path).  Windowed both are bright; on the
    broken code the toggle kills BOTH (luma ~1.5 and NO new shot file).

Clip: this gate generates its own 10-bit HEVC 1080p ~20 s clip with ffmpeg
(never any file from ~/Videos or ~/mnt).

Positive controls (a gate that cannot fail is not a gate): the app log must
contain its `re-attaching video track` line AFTER the toggle — proof the
XTEST key really drove a fullscreen action and the GL surface really was
re-created.  Without that line every other bright reading would be vacuous.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from smoke_harness import ROOT, SCRATCH, check, finish

PY = "/usr/bin/python3"
DISPLAY_NO = ":97"
VENDOR_LIB = ROOT / "vendor" / "usr" / "lib" / "x86_64-linux-gnu"

WORK = SCRATCH / "r27fs"
CFG_DIR = WORK / "cfg"
STATE_DIR = WORK / "state"
HOME_DIR = WORK / "home"
SHOT_DIR = WORK / "shots"
APP_LOG = WORK / "app.log"

# The clip: the brief mandates 10-bit HEVC (the operator's repro).  A
# dev-time env override exists ONLY as a differential-diagnosis knob
# (REELSEE_R27_CLIP=h264 swaps in an 8-bit h264 clip to prove the black
# outcome is format-specific); the battery gate always runs the HEVC form.
CLIP_CODEC = os.environ.get("REELSEE_R27_CLIP", "hevc")
#: 60 s, not 20: the RED path's waits (shot 15 s + restore 15 s) run past a
#: 20 s clip's EOF, and mpv's EOF quiescence would then contaminate every
#: later witness (and could fake-fail a fixed build).  The bug is not EOF:
#: the fullscreen sample went dark 10 s INTO playback, long before it.
CLIP_SECS = "60"
CLIP_SPECS = {
    "hevc": ("r27fs_clip_10hevc.mp4",
             ["-c:v", "libx265", "-preset", "veryfast",
              "-pix_fmt", "yuv420p10le", "-tag:v", "hvc1",
              "-x265-params", "log-level=none"]),
    "h264": ("r27fs_clip_8h264.mp4",
             ["-c:v", "libx264", "-preset", "veryfast",
              "-pix_fmt", "yuv420p"]),
}
CLIP = WORK / CLIP_SPECS[CLIP_CODEC][0]
XWD = WORK / "root.xwd"

# The windowed rect we seed into <state>/window.json: full screen-width at
# the video's 16:9 height under a 1280x800 screen, at the origin.  R25: this
# file is PHYSICAL pixels.  Seeding it makes the opening geometry
# deterministic (R10 restored-rect path) instead of the default 800x450.
SEEDED_RECT = [0, 0, 1280, 720]

CONFIG_JSON = (
    '{\n  "engine": {"decoder": "auto"},\n'
    '  "screenshot": {"directory": "%s"}\n}\n' % str(SHOT_DIR))

LUMA_MIN = 8.0            # same margin as smoke_render / smoke_render_cycle

# deadlines (the RED path costs the full budget; keep total < 180 s)
T_WINDOW = 30.0           # window mapped + WM_CLASS found
T_LUMA = 20.0             # video visible windowed
T_SHOT = 15.0             # a NEW mpv-shot file appears after pressing 's'
T_FS = 5.0                # settle/poll after the fullscreen edge
T_RESTORE = 15.0          # brief §0: broken code is STILL BLACK 15 s later


# ------------------------------------------------------------------ X layer
class XDrive:
    """Private-display driver: window lookup by WM_CLASS + XTEST keysyms.

    Lookup rule (brief §2, measured cost: several runs): under Xvfb there is
    NO window manager, so WM_NAME is EMPTY and a title lookup finds NOTHING.
    The reliable handle is WM_CLASS: the app runs as /usr/bin/python3, so its
    instance name contains 'python3'.  We accept a class/instance containing
    'python3' or 'reelsee' — on a private display this can only be our app.
    """

    def __init__(self):
        from Xlib import X, display as xdisplay     # system python3-xlib
        self._X = X
        self.d = xdisplay.Display(DISPLAY_NO)

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
                # this Xlib build exposes QueryTree on the WINDOW object
                # (not on Display), and the reply field is `children` — a
                # wrong name here raises AttributeError, which a generic
                # except used to swallow: the walk then silently stopped at
                # the root and the gate "could not find" a mapped window.
                stack.extend(w.query_tree().children)
            except Exception:                       # noqa: BLE001
                pass
        return None

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


# ------------------------------------------------------------- measurement
def luma() -> float:
    """Gray mean (0..255) of the WHOLE root of the private display."""
    with open(XWD, "wb") as fh:
        rc = subprocess.run(["xwd", "-root", "-silent", "-display", DISPLAY_NO],
                            stdout=fh, stderr=subprocess.DEVNULL).returncode
    if rc != 0:
        return -1.0
    out = subprocess.run(
        ["convert", str(XWD), "-colorspace", "gray", "-format",
         "%[fx:mean*255]", "info:"],
        capture_output=True, text=True)
    try:
        return float(out.stdout.strip())
    except ValueError:
        return -1.0


def shots() -> list[Path]:
    return sorted(SHOT_DIR.glob("mpv-shot*")) if SHOT_DIR.is_dir() else []


def shot_witness(x, win, deadline: float) -> tuple:
    """LIVENESS witness: press 's' through the real keymap path and demand
    a NEW screenshot file.  RETRIES every 4 s until the deadline, because
    mpv answers `screenshot` with an error while the video chain is mid-
    reinit (measured: the reply landed inside the VIDEO_RECONFIG burst
    after a fullscreen re-attach; the identical command succeeded 2 s
    later on a provably alive player).  Soundness is preserved: a DEAD
    playlist entry fails EVERY attempt — the frozen frame that fools the
    luma witness never gets a file written.  Returns (ok, attempts)."""
    attempts = 0
    while time.time() < deadline:
        attempts += 1
        n = len(shots())
        x.focus_and_key(win, "s")
        sub = min(time.time() + 4.0, deadline)
        while time.time() < sub:
            if len(shots()) > n:
                return True, attempts
            time.sleep(0.25)
    return False, attempts


def wait_luma(deadline: float) -> float:
    """Poll luma until it exceeds LUMA_MIN (return that value) or the
    deadline passes (return the LAST reading — the failure evidence)."""
    last = -1.0
    while time.time() < deadline:
        last = max(last, luma())
        if last > LUMA_MIN:
            return last
        time.sleep(1.0)
    return last


def realized_count() -> int:
    """Lifecycle edges seen by the app so far — the production INFO line
    `render context ready` (window._render_ready) fires on every realize
    that got a render context, in DEBUG *and* in battery (INFO) mode.
    Fullscreen enter AND leave each unrealize/re-realize the GLArea, so this
    is the EDGE the gate toggles on — brighter-than-black polling cannot
    distinguish fullscreen states under a WM-less Xvfb (the window keeps its
    size), and a MAX of luma samples even cannot tell live video from a
    frozen frame (measured: 106.9 after the file was dead)."""
    try:
        return APP_LOG.read_text(errors="replace").count("render context ready")
    except OSError:
        return 0


def wait_edge(before: int, deadline: float) -> bool:
    while time.time() < deadline:
        if realized_count() > before:
            return True
        time.sleep(0.1)
    return False


def luma_now(deadline: float) -> float:
    """ONE reading (retrying only xwd failures) — a point-in-time sample
    AFTER the edge, not a max across it."""
    while time.time() < deadline:
        v = luma()
        if v >= 0:
            return v
        time.sleep(0.5)
    return -1.0


def gen_clip() -> bool:
    """Own 10-bit HEVC 1080p30 ~20 s clip (testsrc2, deterministic); the
    h264 sibling exists for the differential-diagnosis knob only."""
    CLIP.parent.mkdir(parents=True, exist_ok=True)
    if CLIP.exists():
        CLIP.unlink()
    r = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc2=size=1920x1080:rate=30",
         "-t", CLIP_SECS]
        + CLIP_SPECS[CLIP_CODEC][1] + [str(CLIP)],
        capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stderr[:400])
    return CLIP.exists() and CLIP.stat().st_size > 100_000


# ------------------------------------------------------------------ driver
def main() -> int:
    # ---- scratch dirs (fresh per run: shared state is the classic gate race)
    if WORK.exists():
        shutil.rmtree(WORK, ignore_errors=True)
    for d in (CFG_DIR, STATE_DIR, HOME_DIR, SHOT_DIR):
        d.mkdir(parents=True)
    (CFG_DIR / "config.json").write_text(CONFIG_JSON)
    import json
    (STATE_DIR / "window.json").write_text(
        json.dumps({"rect": SEEDED_RECT}))
    check("clip_generated", gen_clip(), CLIP.name)

    # ---- private Xvfb :97, stopped in the finally below
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

        # ---- the REAL app, out of process, NO REELSEE_HEADLESS
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
            # capture-stability recipe from headless-smoke.sh (llvmpipe
            # determinism); the app itself is still NOT headless.
            "GSK_RENDERER": "gl", "GDK_GL": "egl",
            "LIBGL_ALWAYS_SOFTWARE": "1",
            "GSETTINGS_BACKEND": "memory",
        })
        logfh = open(APP_LOG, "wb")
        app = subprocess.Popen(
            [PY, str(ROOT / "src" / "reelsee.py"), str(CLIP)],
            cwd=str(ROOT), env=env, stdout=logfh, stderr=subprocess.STDOUT)

        try:
            run_cycle(app, x)
        finally:
            if app.poll() is None:
                app.terminate()
                try:
                    app.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    app.kill()
                    app.wait()
            logfh.close()
        # The needles below read the CLOSED log: terminate flushes handlers.
        log = APP_LOG.read_text(errors="replace") if APP_LOG.exists() else ""
        check("app_exited_cleanly", app.poll() is not None, str(app.poll()))
        check("fs_cycle_lifecycle_observed",
              "re-attaching video track" in log,
              f"log={APP_LOG} ({len(log)} chars)")
    finally:
        xvfb.terminate()
        try:
            xvfb.wait(timeout=5)
        except subprocess.TimeoutExpired:
            xvfb.kill()
    finish()
    return 0


def run_cycle(app: subprocess.Popen, x: XDrive) -> None:
    # ---- find the window by WM_CLASS (never by title — empty under a WM)
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

    # ---- witness 1: windowed video, both witnesses bright
    lum_w = wait_luma(time.time() + T_LUMA)
    check("windowed_luma", lum_w > LUMA_MIN, f"{lum_w:.1f}")
    ok_shot1, att1 = shot_witness(x, win, time.time() + T_SHOT)
    check("shot_before_fullscreen", ok_shot1, f"attempts={att1}")

    # ---- the measured cycle: ONE fullscreen on -> off via the 'f' keysym.
    # Each toggle is verified on its OWN lifecycle edge (GLArea re-realize),
    # then a SINGLE luma sample ("something bright is on screen now") plus a
    # FRESH screenshot ("the player is ALIVE — a frozen frame is also
    # bright", measured during the R27 RED).
    e1 = realized_count()
    x.focus_and_key(win, "f")
    ok_edge1 = wait_edge(e1, time.time() + T_FS)
    time.sleep(1.5)                       # let a live pump draw a few frames
    lum_fs = luma_now(time.time() + 5.0)
    ok_shot2, att2 = shot_witness(x, win, time.time() + T_SHOT)
    check("fullscreen_edge_observed", ok_edge1, "GLArea re-realized")
    check("fullscreen_luma", lum_fs > LUMA_MIN, f"{lum_fs:.1f}")
    check("shot_after_fullscreen", ok_shot2,
          f"attempts={att2} files_in={SHOT_DIR}")

    # ---- restore: the video must KEEP rendering afterwards — only the
    # fresh-shot witness can prove that, so this assert has no luma escape.
    e2 = realized_count()
    x.focus_and_key(win, "f")
    ok_edge2 = wait_edge(e2, time.time() + T_RESTORE)
    time.sleep(1.5)
    lum_rs = luma_now(time.time() + 5.0)
    ok_shot3, att3 = shot_witness(x, win, time.time() + T_SHOT)
    check("restore_edge_observed", ok_edge2, "GLArea re-realized")
    check("restored_luma", lum_rs > LUMA_MIN, f"{lum_rs:.1f}")
    check("still_rendering_after_restore", ok_shot3,
          f"attempts={att3} luma={lum_rs:.1f}")


if __name__ == "__main__":
    sys.exit(main())
