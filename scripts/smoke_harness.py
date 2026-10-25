"""Shared helpers for the live smoke scripts (P2+ gates).

Assumptions: runs INSIDE scripts/headless-smoke.sh (Xvfb + vendored
LD_LIBRARY_PATH + REELSEE_HEADLESS=1).  Window maps at 0,0 under WM-less
Xvfb, so crops use 640x360+0+0 (§0.10).
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

SCRATCH = ROOT / "scratch" / "smoke"
SCRATCH.mkdir(parents=True, exist_ok=True)
CROP = "640x360+0+0"

failures: list[str] = []


def check(name: str, ok: bool, value="") -> bool:
    print(f"SMOKE_CHECK:{name}={value}", flush=True)
    if not ok:
        failures.append(name)
        print(f"SMOKE_FAIL:{name}", flush=True)
    return bool(ok)


def finish() -> None:
    if failures:
        print("SMOKE_FAILURES: " + ",".join(failures))
        sys.exit(1)
    print("SMOKE_ASSERTS_PASSED")


def capture_xwd(path: Path) -> bool:
    with open(path, "wb") as fh:
        return subprocess.run(["xwd", "-root", "-silent"], stdout=fh,
                              stderr=subprocess.STDOUT).returncode == 0


def image_mean(xwd: Path, crop: str = CROP) -> float:
    out = subprocess.run(
        ["convert", str(xwd), "-crop", crop, "+repage", "-colorspace", "gray",
         "-format", "%[fx:mean*255]", "info:"],
        capture_output=True, text=True)
    try:
        return float(out.stdout.strip())
    except ValueError:
        return -1.0


def image_colors(xwd: Path, crop: str = CROP) -> int:
    out = subprocess.run(
        ["convert", str(xwd), "-crop", crop, "+repage", "-format", "%k",
         "info:"], capture_output=True, text=True)
    try:
        return int(out.stdout.strip())
    except ValueError:
        return -1


class Heartbeat:
    """Main-loop responsiveness probe (P8/P9): a 10 ms source; records the
    drift between scheduled and actual firing.  p95() in ms."""

    def __init__(self, interval_ms: int = 10):
        import time
        self.time = time
        self.interval = interval_ms / 1000.0
        self.lats: list[float] = []
        self._next = None
        self._id = None

    def start(self):
        from gi.repository import GLib
        self._next = self.time.monotonic() + self.interval
        self._id = GLib.timeout_add(int(self.interval * 1000), self._tick)
        return self

    def _tick(self):
        now = self.time.monotonic()
        self.lats.append(now - self._next)
        self._next = now + self.interval
        return True

    def p95(self) -> float:
        if not self.lats:
            return 0.0
        s = sorted(self.lats)
        return s[min(len(s) - 1, int(len(s) * 0.95))] * 1000.0

    def stop(self):
        from gi.repository import GLib
        if self._id is not None:
            GLib.source_remove(self._id)
            self._id = None
