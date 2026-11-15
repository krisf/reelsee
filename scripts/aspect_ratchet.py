#!/usr/bin/python3
"""R28 repro/gate: crop OFF + aspect lock ON must open at the seeded rect
and STAY — and the saved rect must equal the live one.

Adapted from the operator's aspect_ratchet.py (the tool that MEASURED the
collapse: 1200x900 -> 370x230 -> 420x258 -> 54x54, and the tiny rect that
then got persisted as the user's "missing window").  Two deliberate
differences from the original:

  * no systemd — the app is a Popen child whose PID is recorded, and the
    ONLY processes this script ever signals are PIDs it started itself
    (SIGTERM first: the graceful quit path flushes window.json; KILL only
    if it ignores TERM);
  * state/config live in a fresh /tmp directory every run — the operator's
    real ~/.config/reelsee and ~/.local/state/reelsee are never touched.

usage: aspect_ratchet.py <clip> <crop 0|1> <lock 0|1> <secs>
env:   AR_SEED=WxH        seeded window.json size (default 1200x900)
       AR_ORIGIN=X,Y      seeded position (default 200,140)
       AR_SRC=DIR         src/ override on PYTHONPATH (old-code control)
       AR_LAUNCH=FILE     entry script instead of src/reelsee.py (diag)
       AR_DIAG_LOG=FILE   passed through to the launched script
       AR_SCALE=N         GDK_SCALE (default 1)
       DISPLAY/XAUTHORITY passed through (operator runs: DISPLAY=:0)
prints one line per size change (t=secs w x h), then
  LIVE_FINAL=<w>x<h>  SAVED_AFTER_CLOSE=<x,y,w,h>  VERDICT=...
"""
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
VENDOR = REPO / "vendor" / "usr" / "lib" / "x86_64-linux-gnu"


def main():
    clip, crop, lock, secs = sys.argv[1], sys.argv[2], sys.argv[3], float(sys.argv[4])
    seed = os.environ.get("AR_SEED", "1200x900")
    sw, sh = (int(v) for v in re.findall(r"\d+", seed)[:2])
    ox, oy = (int(v) for v in re.findall(r"\d+", os.environ.get("AR_ORIGIN", "200,140"))[:2])
    tmp = Path(tempfile.mkdtemp(prefix="r28-ar-", dir="/tmp"))
    state, cfg = tmp / "state", tmp / "cfg"
    state.mkdir(); cfg.mkdir()
    (state / "window.json").write_text(json.dumps({
        "version": 1, "rect": {"x": ox, "y": oy, "w": sw, "h": sh},
        "monitor": None}))
    (cfg / "config.json").write_text(json.dumps({
        "video": {"crop_to_window": crop == "1"},
        "ui": {"lock_aspect": lock == "1", "remember_geometry": True}}))

    env = dict(os.environ)
    env.update({
        "GDK_BACKEND": "x11",
        "REELSEE_DEBUG": "1",
        "GDK_SCALE": os.environ.get("AR_SCALE", "1"),
        "REELSEE_STATE_DIR": str(state),
        "REELSEE_CONFIG_DIR": str(cfg),
    })
    src = os.environ.get("AR_SRC", str(REPO / "src"))
    env["PYTHONPATH"] = src + os.pathsep + env.get("PYTHONPATH", "")
    if VENDOR.is_dir():
        env["LD_LIBRARY_PATH"] = str(VENDOR) + os.pathsep + env.get("LD_LIBRARY_PATH", "")
    launch = os.environ.get("AR_LAUNCH", str(REPO / "src" / "reelsee.py"))
    c = Path(clip)
    if not c.is_absolute():
        c = (REPO / c).resolve()
    # the child's OWN stdout/stderr (mpv init errors included — DEVNULL
    # is how a "no video loaded" run became unspecifiable): a file we
    # name, tailed into our stdout when the run is over.
    applog = os.environ.get("AR_APPLOG", "/tmp/r28-app.log")
    Path(applog).write_text("")
    proc = subprocess.Popen(["/usr/bin/python3", launch, str(c)], env=env,
                            stdout=open(applog, "w"),
                            stderr=subprocess.STDOUT)
    sys.stderr.write(f"pid={proc.pid} seed={sw}x{sh} crop={crop} lock={lock}\n")
    sys.stderr.flush()

    from Xlib import display as xd

    def find_geom(d):
        def walk(w, depth=0):
            if depth > 7:
                return None
            try:
                cls = w.get_wm_class()
            except Exception:
                cls = None
            try:
                if w.get_attributes().map_state and (cls and any(
                        k in cls[0].lower() + cls[1].lower()
                        for k in ("reelsee", "python3"))):
                    g = w.get_geometry()
                    return (g.width, g.height)
            except Exception:
                pass
            for k in w.query_tree().children:
                r = walk(k, depth + 1)
                if r:
                    return r
            return None
        try:
            return walk(d.screen().root)
        except Exception:
            return None

    d = xd.Display()
    t0 = time.monotonic()
    last = None
    timeline = []
    while time.monotonic() - t0 < secs:
        if proc.poll() is not None:
            sys.stderr.write(f"app exited early rc={proc.returncode}\n")
            break
        g = find_geom(d)
        t = round(time.monotonic() - t0, 2)
        if g and g != last:
            sys.stderr.write(f"t={t} {g[0]}x{g[1]}\n")
            sys.stderr.flush()
            timeline.append((t, *g))
            last = g
        time.sleep(0.2)
    live = last
    # SIGTERM = graceful quit (capture_geometry + flush); ONLY our pid.
    if proc.poll() is None:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=6)
        except subprocess.TimeoutExpired:
            sys.stderr.write("TERM ignored -> KILL (recorded pid only)\n")
            proc.kill()
            proc.wait(timeout=5)
    try:
        saved = json.loads((state / "window.json").read_text()).get("rect")
    except Exception:
        saved = None
    print(f"LIVE_FINAL={live[0]}x{live[1]}" if live else "LIVE_FINAL=none")
    print(f"SAVED_AFTER_CLOSE={saved['x']},{saved['y']},{saved['w']},{saved['h']}"
          if saved else "SAVED_AFTER_CLOSE=none")
    ok = bool(live) and live == (sw, sh)
    print(f"VERDICT={'HELD' if ok else 'DRIFTED'} seed={sw}x{sh} "
          f"timeline={timeline}")
    shutil.rmtree(tmp, ignore_errors=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
