#!/usr/bin/python3
"""R3-P1 operator matrix: SIGTERM/SIGINT x kill-at {2,3,5} s on a FRESH
state dir per run, resume_min_s=1, asserting a NON-EMPTY resume whose
pos sits within ~1 s of the kill time (below it by at most the startup
delay).  This is the exact matrix the operator measured broken in R2
(pos was only ever the 5 s tick value; kills before the first tick
persisted nothing).  Runs the real app 6x under Xvfb.
"""
import json
import os
import shutil
import signal
import subprocess
import sys
import time

from smoke_harness import ROOT, SCRATCH, check, finish

BAND_LO, BAND_HI = 1.5, 0.4         # pos in [kill-1.5, kill+0.4] (~1 s)


def one(signame: str, kill_at_s: float) -> None:
    tag = f"{signame.lower()}_{int(kill_at_s)}s"
    cfg = os.path.join(SCRATCH, f"mtx-{tag}-cfg")
    state = os.path.join(SCRATCH, f"mtx-{tag}-state")
    resume = os.path.join(state, "resume.json")
    for _d in (cfg, state):                       # FRESH dir per run
        shutil.rmtree(_d, ignore_errors=True)
        os.makedirs(_d)
    with open(os.path.join(cfg, "config.json"), "w",
              encoding="utf-8") as fh:
        fh.write('{"playback": {"resume_min_s": 1}}\n')

    env = dict(os.environ,
               REELSEE_CONFIG_DIR=cfg, REELSEE_STATE_DIR=state)
    t0 = time.monotonic()
    child = subprocess.Popen(
        [sys.executable, "src/reelsee.py", "testdata/perf_1080p30.mp4"],
        cwd=str(ROOT), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    while time.monotonic() - t0 < kill_at_s and child.poll() is None:
        time.sleep(0.02)
    if child.poll() is not None:
        check(f"mtx_{tag}_playing", False, "child died before kill")
        return
    kill_at = time.monotonic() - t0
    try:
        os.unlink(resume)          # only the shutdown flush may (re)write
    except OSError:
        pass
    os.kill(child.pid, getattr(signal, signame))
    try:
        rc = child.wait(timeout=10)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait()
        check(f"mtx_{tag}_graceful", False, "alive 10s after signal")
        return
    check(f"mtx_{tag}_exit0", rc == 0, f"rc={rc}")

    flushed = None
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        try:
            with open(resume, encoding="utf-8") as fh:
                flushed = json.load(fh)
            break
        except (OSError, ValueError):
            time.sleep(0.05)
    check(f"mtx_{tag}_resume_nonempty", bool(flushed),
          repr(flushed)[:120])
    pos = None
    if isinstance(flushed, dict) and flushed:
        entry = next(iter(flushed.values()))
        pos = entry.get("pos") if isinstance(entry, dict) else None
    ok = (isinstance(pos, (int, float))
          and kill_at - BAND_LO <= pos <= kill_at + BAND_HI)
    check(f"mtx_{tag}_pos_within_1s_of_kill", ok,
          f"pos={pos} kill_at={kill_at:.2f} "
          f"band=[{kill_at - BAND_LO:.2f},{kill_at + BAND_HI:.2f}]")


def main() -> int:
    for sig in ("SIGTERM", "SIGINT"):
        for t in (2.0, 3.0, 5.0):
            one(sig, t)
    finish()
    return 0


if __name__ == "__main__":
    sys.exit(main())
