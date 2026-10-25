#!/usr/bin/python3
"""R2-P4(N1)/R3-P2: SIGTERM/SIGINT must take the GRACEFUL quit path AND
persist the CURRENT position, deterministically.

The R2 version was not deterministic: it killed at a fixed 7 s (so the
outcome raced the 5 s resume tick) and BOTH signal phases shared ONE
state dir — the SIGINT phase inherited the SIGTERM entry, restored it,
ran to EOF (which legitimately clears resume) and flushed {}, so the
phase could only pass by accident (operator measured 0/3).

Fixes (R3-P2):
  * fresh config dir AND state dir PER PHASE — no cross-signal leakage;
  * kill at 3.0 s: strictly BEFORE the first 5 s tick can fire, so a
    resume entry can ONLY exist if the shutdown path persisted it;
  * the entry must exist AND carry the position at kill time: with the
    snapshot contract (event-thread time-pos, freshness << 1 s) pos must
    sit within ~1 s below the measured wall-clock kill offset (the slack
    is process startup — playback cannot have started before Popen).
    A stale/tick-rounded value outside that band FAILS.

Signal handler chain under test: GLibUnix.signal_add -> _graceful ->
app.quit() -> do_shutdown (close-request is NOT fired on this path) ->
capture_resume_position() -> resume_store.persist().  Child must exit 0
(NOT -15/-2), promptly, with no Traceback and no GtkGizmo warnings.
"""
import json
import os
import shutil
import signal
import subprocess
import sys
import time

from smoke_harness import ROOT, SCRATCH, check, finish

KILL_AT = 3.0                       # s after Popen; < 5 s tick boundary
BAND_LO, BAND_HI = 1.5, 0.4         # pos in [kill-1.5, kill+0.4] (~1 s)


def run_one(signame: str) -> None:
    tag = signame.lower()
    cfg = os.path.join(SCRATCH, f"sig-{tag}-cfg")
    state = os.path.join(SCRATCH, f"sig-{tag}-state")
    resume = os.path.join(state, "resume.json")
    for _d in (cfg, state):                       # R3-P2: FRESH per phase
        shutil.rmtree(_d, ignore_errors=True)
        os.makedirs(_d)
    with open(os.path.join(cfg, "config.json"), "w",
              encoding="utf-8") as fh:
        # default resume_min_s=300 would gate the RESTORE; recording band
        # for the 10 s clip is 1..9 s
        fh.write('{"playback": {"resume_min_s": 1}}\n')

    log_path = os.path.join(SCRATCH, f"signal_{tag}_app.log")
    env = dict(os.environ,
               REELSEE_CONFIG_DIR=cfg, REELSEE_STATE_DIR=state)
    t0 = time.monotonic()
    child = subprocess.Popen(
        [sys.executable, "src/reelsee.py", "testdata/perf_1080p30.mp4"],
        cwd=str(ROOT), env=env,
        stdout=open(log_path, "w"), stderr=subprocess.STDOUT)
    while time.monotonic() - t0 < KILL_AT and child.poll() is None:
        time.sleep(0.02)
    alive = child.poll() is None
    check(f"{tag}_app_playing", alive, f"after {KILL_AT}s")
    if not alive:
        check(f"{tag}_run_aborted", False, "child died before playing")
        return
    kill_at = time.monotonic() - t0
    try:                            # flush-proof: only the signal can write
        os.unlink(resume)
    except OSError:
        pass
    os.kill(child.pid, getattr(signal, signame))
    try:
        rc = child.wait(timeout=10)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait()
        check(f"{tag}_exits_gracefully", False, "alive 10s after signal")
        return
    check(f"{tag}_exit_code_0", rc == 0,
          f"rc={rc} ({'signal-killed' if rc < 0 else 'clean'})")

    flushed = None
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        try:
            with open(resume, encoding="utf-8") as fh:
                flushed = json.load(fh)
            break
        except (OSError, ValueError):
            time.sleep(0.05)
    # 1) the shutdown path wrote it (killed before the first 5 s tick —
    #    no other writer ran)
    check(f"{tag}_resume_flushed", bool(flushed), repr(flushed)[:120])
    # 2) and it holds the CURRENT position, not a stale/tick value
    pos = None
    if isinstance(flushed, dict) and flushed:
        entry = next(iter(flushed.values()))
        pos = entry.get("pos") if isinstance(entry, dict) else None
    ok = (isinstance(pos, (int, float))
          and kill_at - BAND_LO <= pos <= kill_at + BAND_HI)
    check(f"{tag}_resume_pos_matches_kill", ok,
          f"pos={pos} kill_at={kill_at:.2f} "
          f"band=[{kill_at - BAND_LO:.2f},{kill_at + BAND_HI:.2f}]")
    try:
        with open(log_path, encoding="utf-8") as fh:
            log = fh.read()
        check(f"{tag}_no_traceback", "Traceback" not in log, "log clean")
        check(f"{tag}_no_gizmo_warn", "reported min" not in log,
              "no GtkGizmo measure warnings (R3-P3/N4)")
    except OSError as exc:
        check(f"{tag}_no_traceback", False, repr(exc))


def main() -> int:
    for sig in ("SIGTERM", "SIGINT"):
        run_one(sig)
    finish()
    return 0


if __name__ == "__main__":
    sys.exit(main())
