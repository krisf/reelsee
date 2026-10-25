#!/usr/bin/env python3
"""R4-P3 smoke: the friendly failure path END TO END under Xvfb.

Starts reelsee.py against a libmpv that cannot exist (negctl shim: ctypes
find_library -> None, every candidate path masked) with REELSEE_HEADLESS
UNSET and DISPLAY set — the real desktop situation — and asserts:
  * exit code 2 (LIBMPV_FAIL_EXIT contract), not 1-from-traceback
  * stderr carries the diagnosis: every searched path + both remedies
  * NO Python traceback anywhere (that is what Apport used to catch)
  * the GTK error dialog really painted: screenshot taken WHILE the
    dialog is open (delayed xwd) shows non-black pixels
  * REELSEE_ERROR_DIALOG_TIMEOUT auto-closes it — the run finishes well
    inside its wall clock, proving no hang
"""
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from smoke_harness import ROOT, SCRATCH, check, finish

SHIM = Path(SCRATCH) / "dialog-shim"


def build_shim() -> dict:
    """Negative-control host: no system libmpv (masked + ctypes dead) and
    no dev-repo vendor (masked too — this smoke runs the DEV tree)."""
    import mpvlib
    shutil.rmtree(SHIM, ignore_errors=True)
    (SHIM / "bin").mkdir(parents=True)
    for tool in ("cc", "gcc", "ld", "ldconfig", "objdump"):
        p = SHIM / "bin" / tool
        p.write_text("#!/bin/sh\nexit 1\n")
        p.chmod(0o755)
    mask = list(mpvlib.SYSTEM_PATHS)
    mask.append(str(mpvlib.REPO / mpvlib.VENDOR_REL / "libmpv.so.2"))
    (SHIM / "py").mkdir()
    (SHIM / "py" / "sitecustomize.py").write_text(
        "import ctypes.util, os, pathlib\n"
        "ctypes.util.find_library = lambda name: None\n"
        f'_MASK = [m for m in "{os.pathsep.join(mask)}".split(os.pathsep) if m]\n'
        "_orig = pathlib.Path.exists\n"
        "def _exists(self, follow_symlinks=True):\n"
        "    s = os.path.abspath(str(self))\n"
        "    if any(s == m or s.startswith(m.rstrip('/') + '/') for m in _MASK):\n"
        "        return False\n"
        "    return _orig(self, follow_symlinks=follow_symlinks)\n"
        "pathlib.Path.exists = _exists\n")
    home = Path(SCRATCH) / "dialog-home"
    shutil.rmtree(home, ignore_errors=True)
    for sub in (".config", ".local/state", ".local/share", ".cache"):
        (home / sub).mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    for k in ("LD_LIBRARY_PATH", "REELSEE_LIBMPV", "REELSEE_CONFIG_DIR",
              "REELSEE_STATE_DIR", "REELSEE_HEADLESS"):   # ← NOT headless
        env.pop(k, None)
    env.update(HOME=str(home), XDG_CONFIG_HOME=str(home / ".config"),
               XDG_STATE_HOME=str(home / ".local/state"),
               XDG_CACHE_HOME=str(home / ".cache"),
               XDG_DATA_HOME=str(home / ".local/share"),
               PATH=str(SHIM / "bin"), PYTHONPATH=str(SHIM / "py"),
               REELSEE_ERROR_DIALOG_TIMEOUT="3")
    return env


def imgstat(args: list[str]) -> float:
    out = subprocess.run(["convert", *args], capture_output=True, text=True)
    try:
        return float(out.stdout.strip())
    except ValueError:
        return -1.0


def main() -> int:
    if not os.environ.get("DISPLAY"):
        print("SMOKE_SKIP:dialog no DISPLAY (run inside Xvfb)")
        finish()
        return 0
    env = build_shim()
    xwd = os.path.join(SCRATCH, "dialog.xwd")
    png = os.path.join(SCRATCH, "dialog.png")
    for f in (xwd, png):
        if os.path.exists(f):
            os.remove(f)

    # shot lands ~1.5s after launch: dialog opens ~1s in, lives ~3s
    shot = subprocess.Popen(
        f"sleep 1.5; xwd -root -silent > {xwd}", shell=True, env=os.environ)
    t0 = time.time()
    r = subprocess.run(
        ["/usr/bin/python3", os.path.join(ROOT, "src", "reelsee.py"),
         os.path.join(ROOT, "testdata", "smoke_360p.mp4")],
        env=env, capture_output=True, text=True, timeout=60, cwd=SCRATCH)
    elapsed = time.time() - t0
    try:
        shot.wait(timeout=15)
    except subprocess.TimeoutExpired:
        shot.kill()
    subprocess.run(["convert", xwd, png], capture_output=True)

    out = (r.stdout or "") + (r.stderr or "")
    check("err_exit_2", r.returncode == 2, f"rc={r.returncode}")
    check("err_stderr_diagnosis",
          "libmpv not found" in out and "install libmpv2" in out
          and "REELSEE_LIBMPV" in out
          and "/usr/lib/x86_64-linux-gnu/libmpv.so.2" in out,
          f"len={len(out)}")
    check("err_no_traceback", "Traceback" not in out, "clean")
    check("err_dialog_logged", "error dialog shown" in out,
          "logged" if "error dialog shown" in out else "missing")
    # whole-frame evidence (no WM under Xvfb → dialog position unknown):
    # fraction of non-black pixels after fuzzing near-black away
    nonblack = imgstat([png, "-fuzz", "5%", "-fill", "black",
                        "-opaque", "black", "-colorspace", "gray",
                        "-format", "%[fx:mean*255]", "info:"]) \
        if os.path.exists(png) else -1
    colors = imgstat([png, "-format", "%k", "info:"]) \
        if os.path.exists(png) else -1
    check("err_dialog_painted", nonblack > 0.5 and colors > 50,
          f"nonblack={nonblack:.2f} colors={colors:.0f}")
    check("err_dialog_autoclose", elapsed < 30, f"elapsed={elapsed:.1f}s")
    finish()
    return 0


if __name__ == "__main__":
    sys.exit(main())
