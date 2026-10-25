#!/usr/bin/env python3
"""Render the repo's documentation screenshots from the real app.

No display server on the build box has a screen-capture protocol this can
use (Wayland compositors here expose neither wlr-screencopy nor a portal
this process may call), so screenshots are produced by running the actual
application — real widgets, real CSS, real mpv frames — inside Xvfb and
capturing the X11 window:

    xvfb-run -a -s "-screen 0 1440x900x24" \\
        /usr/bin/python3 scripts/make_screenshots.py

Everything is generated: the clips are synthetic (ffmpeg lavfi sources), the
config/state HOME is a throwaway directory under scratch/, and nothing reads
a user's real config or media. PNGs land in docs/screenshots/.

Shot list:
  hero        playback with the transport bar and title bar revealed
  clean       chrome retracted after the idle timeout (video only)
  settings    the Adw preferences window (engine / GPU selector)

No context-menu shot: see the note in main() — GTK popups cannot be captured
under a compositor-less Xvfb, so that image would be a fiction.
"""
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "screenshots"
SCR = ROOT / "scratch" / "shots"
PY = sys.executable or "/usr/bin/python3"

from Xlib import X, XK, display                      # noqa: E402
from Xlib.ext import xtest                           # noqa: E402

_D = display.Display()


def windows_for_pid(pid: int):
    """Top-level windows belonging to `pid`, with root-relative geometry."""
    out = []
    root = _D.screen().root
    for w in root.query_tree().children:
        try:
            if not w.get_attributes().map_state == X.IsViewable:
                continue
        except Exception:
            continue
        try:
            got = w.get_full_property(_atom("_NET_WM_PID"), X.AnyPropertyType)
        except Exception:
            got = None
        if got is None or got.value is None or int(got.value[0]) != pid:
            continue
        g = w.get_geometry()
        abs_xy = w.translate_coords(root, 0, 0)
        out.append({"win": w, "x": -abs_xy.x, "y": -abs_xy.y,
                    "w": g.width, "h": g.height})
    return out


def _atom(name: str):
    return _D.intern_atom(name)


def capture(rect, path: Path, pad: int = 0):
    """Grab a root-region (so override-redirect menus are included)."""
    x = max(0, rect["x"] - pad)
    y = max(0, rect["y"] - pad)
    w = rect["w"] + 2 * pad
    h = rect["h"] + 2 * pad
    subprocess.run(f"xwd -root -screen -silent | convert xwd:- "
                   f"-crop {w}x{h}+{x}+{y} -define png:compression-level=9 "
                   f"'{path}'", shell=True, check=True)


def env(home: Path) -> dict:
    for d in ("config", "state", "cache"):
        (home / d).mkdir(parents=True, exist_ok=True)
    e = dict(os.environ)
    e.update(HOME=str(home), XDG_CONFIG_HOME=str(home / "config"),
             XDG_STATE_HOME=str(home / "state"),
             XDG_CACHE_HOME=str(home / "cache"),
             XDG_DATA_HOME=str(home / "data"),
             GDK_BACKEND="x11", GSETTINGS_BACKEND="memory",
             LIBGL_ALWAYS_SOFTWARE="1", NO_AT_BRIDGE="1",
             REELSEE_SCREENSHOT="1")
    e.pop("WAYLAND_DISPLAY", None)
    return e


def write_cfg(home: Path, overrides: dict) -> None:
    cfg_dir = home / "config" / "reelsee"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.json").write_text(json.dumps(overrides, indent=2))


def click_root(x: int, y: int, button: int = 1) -> None:
    xtest.fake_input(_D, X.MotionNotify, x=x, y=y)
    _D.sync()
    xtest.fake_input(_D, X.ButtonPress, button)
    _D.sync()
    time.sleep(0.06)
    xtest.fake_input(_D, X.ButtonRelease, button)
    _D.sync()


def warp(x: int, y: int) -> None:
    xtest.fake_input(_D, X.MotionNotify, x=x, y=y)
    _D.sync()


def key_combo(keysym: str, ctrl: bool = False, at=None) -> None:
    if at:
        warp(*at)          # no WM under Xvfb: keys follow the pointer
    ks = XK.string_to_keysym(keysym)
    if ks == 0:
        print(f"  ! unknown keysym {keysym!r}")
        return
    kc = _D.keysym_to_keycode(ks)
    ctl = _D.keysym_to_keycode(XK.XK_Control_L)
    if ctrl:
        xtest.fake_input(_D, X.KeyPress, ctl)
        _D.sync()
        time.sleep(0.03)
    xtest.fake_input(_D, X.KeyPress, kc)
    _D.sync()
    time.sleep(0.05)
    xtest.fake_input(_D, X.KeyRelease, kc)
    if ctrl:
        xtest.fake_input(_D, X.KeyRelease, ctl)
    _D.sync()


def synth_clips() -> dict:
    """Deterministic, clearly synthetic demo clips (never user media)."""
    SCR.mkdir(parents=True, exist_ok=True)
    clips = {}
    specs = {
        "fractal.mp4": ["-f", "lavfi", "-i",
                        "mandelbrot=size=1280x720:rate=30:maxiter=120",
                        "-t", "14"],
        "bars_1080.mp4": ["-f", "lavfi", "-i",
                          "testsrc2=size=1920x1080:rate=30",
                          "-t", "12"],
    }
    for name, src in specs.items():
        p = SCR / name
        if not p.exists():
            subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel",
                            "error", *src, "-f", "lavfi", "-i",
                            "sine=frequency=440:duration=14",
                            "-shortest", "-c:v", "libx264", "-preset",
                            "veryfast", "-pix_fmt", "yuv420p", "-c:a",
                            "aac", str(p)], check=True)
        clips[name] = p
    return clips


def _title(w) -> str:
    for atom in ("_NET_WM_NAME", "WM_NAME"):
        try:
            p = w.get_full_property(_atom(atom), X.AnyPropertyType)
        except Exception:
            p = None
        if p is not None and p.value:
            v = p.value
            return v.decode("utf-8", "replace") if isinstance(v, bytes) else str(v)
    return ""


def pick_win(wins, want: str):
    """Largest window, or the one whose title contains `want`."""
    if want and want != "largest":
        named = [w for w in wins if want.lower() in _title(w["win"]).lower()]
        if named:
            return max(named, key=lambda w: w["w"] * w["h"])
    return max(wins, key=lambda w: w["w"] * w["h"])


def shot(name: str, clip: Path, cfg: dict, act=None, warm: float = 4.0,
         pad: int = 0, pick: str = "largest", reveal: bool = False,
         act_delay: float = 1.0) -> bool:
    home = SCR / f"home-{name}"
    shutil.rmtree(home, ignore_errors=True)
    home.mkdir(parents=True)
    write_cfg(home, cfg)
    proc = subprocess.Popen([PY, str(ROOT / "src" / "reelsee.py"), str(clip)],
                            env=env(home), stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    try:
        time.sleep(warm)
        wins = windows_for_pid(proc.pid)
        if not wins:
            print(f"  {name}: NO WINDOW (pid {proc.pid})")
            return False
        rect = pick_win(wins, pick)
        if reveal:
            # Chrome is motion-driven: without a real pointer move the bars
            # stay retracted and the shot is just video.
            warp(rect["x"] + rect["w"] // 2, rect["y"] + rect["h"] - 60)
            time.sleep(0.6)
        if act:
            act(rect)
            time.sleep(act_delay)
            wins = windows_for_pid(proc.pid)
            if wins:
                rect = pick_win(wins, pick)
        out = OUT / f"{name}.png"
        capture(rect, out, pad=pad)
        print(f"  {name}: {rect['w']}x{rect['h']} -> {out.name}")
        return out.exists() and out.stat().st_size > 20_000
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    clips = synth_clips()
    slow = {"ui": {"chrome_timeout_ms": 120000}}
    fast = {"ui": {"chrome_timeout_ms": 500}}
    ok = {}
    print("shots:")
    ok["hero"] = shot("hero", clips["fractal.mp4"], slow, reveal=True)
    ok["clean"] = shot("clean", clips["fractal.mp4"], fast, warm=6.0)

    # No context-menu screenshot: GTK pops are override-redirect and are
    # dismissed on focus-out the instant there is no window manager to keep
    # the grab, which is exactly the Xvfb case. The window does appear in the
    # X tree for a frame, but it is never visible in a grabbed image. Claiming
    # otherwise would mean shipping a picture of something that is not there.

    def open_settings(rect):
        key_combo("comma", ctrl=True,
                  at=(rect["x"] + rect["w"] // 2, rect["y"] + rect["h"] // 2))

    ok["settings"] = shot("settings", clips["bars_1080.mp4"], slow,
                          act=open_settings, pad=0, pick="Settings",
                          reveal=True)

    print("summary:", json.dumps(ok))
    return 0 if all(ok.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
