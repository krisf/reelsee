#!/usr/bin/python3
"""R13 gate smoke (idlechrome): the chrome must STAY hidden while idle.

The bug this gate exists to kill: ChromeState.tick() used to measure dt
from the LAST INPUT and read dt > timeout*4 (3000*4 = 12000 ms) as a
forward clock jump, force-revealing the chrome — so plain idleness
resurrected the menus on an endless 12 s cycle (hide 3 s, back 12 s, hide
15 s, back 24 s, ...) with no pointer ever entering the window.

Method: drive the REAL window with a FAST-FORWARDED clock (20x — the
window's own 10 ms tick stream drives ChromeState, so the healthy tick
gaps stay ~200 ms, far below the 2000 ms discontinuity threshold).  Zero
input for 60 simulated seconds; sample the APPLIED chrome opacity (both
reveal groups, R12) every simulated second; assert:

  * hidden (opacity ~0, click-through) by simulated second 5 and at
    EVERY sample from the first hidden one through 44 — not one
    resurrection, not one fade (the old build showed `shown` at seconds
    12/13/14, 24/25/26, 36/37/38 here);
  * the VIDEO's allocation is byte-identical at every sample (a
    resurrect in the old build moved nothing but is exactly what the
    opacity assertion catches; allocation pins the R12 contract);
  * POSITIVE CONTROL: pointer motion at simulated second 45 RE-REVEALS —
    the decision is applied on the first tick (can_target flips True at
    second 46; the ~180 ms opacity fade runs on the main loop and may
    land one sample later, so full opacity is asserted at 47) — then the
    chrome re-hides after the 3 s timeout and stays hidden through
    second 60: motion still works, idleness never shows.

Prints the whole second-by-second timeline as `R13_TL ...` lines for the
report.
"""
import json
import os
import sys
import time

from smoke_harness import SCRATCH, check, finish

SPEED = 20.0                 # simulated ms per real ms
SAMPLE_REAL_MS = int(1000 / SPEED)   # one sample per simulated second
N_SAMPLES = 61               # simulated seconds 0..60
IDLE_END = 44                # idle stretch samples up to here must be hidden
MOTION_S = 45                # positive-control motion at simulated second
CTRL_DECISION = 46           # reveal DECISION (can_target) by second 46
CTRL_SHOWN = 47              # fully opaque by second 47
REHIDDEN_FROM = 50           # 45 + 3 s timeout + fade slack => hidden

CFG = SCRATCH / "idlechrome-cfg"
CFG.mkdir(parents=True, exist_ok=True)
(CFG / "config.json").write_text(json.dumps(
    {"ui": {"chrome_timeout_ms": 3000, "osd_ms": 1200}}))
os.environ["REELSEE_CONFIG_DIR"] = str(CFG)
os.environ["REELSEE_STATE_DIR"] = str(SCRATCH / "idlechrome-state")

import reelsee  # noqa: E402  (after env, like the real entry)

state: dict = {"samples": []}


def chrome_fade(win):
    return (float(win.chrome.bottom.get_opacity()),
            float(win.top_bar.get_opacity()))


def classify(win):
    ob, ot = chrome_fade(win)
    tgt = (win.chrome.bottom.get_can_target(),
           win.top_bar.get_can_target())
    if ob < 0.05 and ot < 0.05 and not any(tgt):
        return "hidden", ob, ot, tgt
    if ob > 0.95 and ot > 0.95 and all(tgt):
        return "shown", ob, ot, tgt
    return "fading", ob, ot, tgt


def video_size(win):
    return (win.view.area.get_width(), win.view.area.get_height())


def playing(win) -> bool:
    snap = win.core.snapshot
    return (snap.get("pause") is False
            and snap.get("idle-active") is False
            and snap.get("eof-reached") is not True)


def probe_schedule(app):
    from gi.repository import GLib
    state["app"] = app

    def win():
        return app.windows[0]

    def wait_playing(tries=0):
        """The idle stretch needs OBSERVED playing (the hide rule is
        playing-only) — never assume, always read the snapshot."""
        if playing(win()):
            return start()
        if tries >= 40:                      # 4 s ceiling
            check("file_started_playing", False, repr(win().core.snapshot))
            app.quit()
            return False
        GLib.timeout_add(100, wait_playing, tries + 1)
        return False

    def start():
        check("file_started_playing", True, "pause=False idle=False")
        st = win().chrome.state
        t_wall0 = time.monotonic()
        # Fast-forwarded clock: ChromeState (and the reveal fades) run on
        # it; the window's real 10 ms tick stream now moves virtual time
        # ~200 ms per tick — a HEALTHY gap, never a discontinuity.
        st._last_tick_ms = None              # fresh tick baseline
        st.clock = lambda: (time.monotonic() - t_wall0) * 1000.0 * SPEED
        win().chrome.motion(400, 300, 960, 540)   # anchor + reveal, ZERO
        state["alloc"] = video_size(win())        # input for the rest
        GLib.timeout_add(SAMPLE_REAL_MS, sample, 0)
        return False

    def sample(i):
        w = win()
        kind, ob, ot, tgt = classify(w)
        alloc = video_size(w)
        state["samples"].append((i, kind, ob, ot, tgt, alloc))
        print(f"R13_TL i={i:02d} t={i:02d}s state={kind:6s} "
              f"op={ob:.2f}/{ot:.2f} target={tgt[0]}/{tgt[1]} "
              f"alloc={alloc[0]}x{alloc[1]}", flush=True)
        if i == MOTION_S:
            w.chrome.motion(400, 300, 960, 540)   # POSITIVE CONTROL
        if i < N_SAMPLES - 1:
            GLib.timeout_add(SAMPLE_REAL_MS, sample, i + 1)
            return False
        verdicts()
        app.quit()
        return False

    def verdicts():
        s = {i: (kind, tgt, alloc) for i, kind, _ob, _ot, tgt, alloc
             in state["samples"]}
        first_hidden = next((i for i in range(0, 9) if s[i][0] == "hidden"),
                            None)
        check("hidden_by_second_5", first_hidden is not None
              and first_hidden <= 5, f"first_hidden={first_hidden}")
        lo = first_hidden if first_hidden is not None else 3
        viol = [i for i in range(lo, IDLE_END + 1) if s[i][0] != "hidden"]
        check("idle_never_resurrects", not viol,
              f"violations={viol} first_hidden={lo}")
        a0 = state["alloc"]
        bad = [i for i in sorted(s) if s[i][2] != a0]
        check("video_alloc_unchanged_60s", not bad,
              f"base={a0} offenders={bad}")
        check("motion_reveals_decision",
              s[CTRL_DECISION][1] == (True, True),
              f"can_target@{CTRL_DECISION}={s[CTRL_DECISION][1]}")
        check("motion_positive_control_shown", s[CTRL_SHOWN][0] == "shown",
              f"state@{CTRL_SHOWN}={s[CTRL_SHOWN][0]}")
        viol2 = [i for i in range(REHIDDEN_FROM, N_SAMPLES)
                 if s[i][0] != "hidden"]
        check("rehidden_after_control_stays_hidden", not viol2,
              f"violations={viol2}")

    GLib.timeout_add(800, wait_playing)
    return False


def main() -> int:
    # 10s clip: 60 SIMULATED seconds are only ~3 real seconds, so EOF
    # (which legitimately reveals) is far away.
    rc = reelsee.run(["reelsee", "testdata/perf_1080p30.mp4"],
                     on_app=probe_schedule)
    check("app_rc", rc == 0, str(rc))
    finish()
    return 0


if __name__ == "__main__":
    sys.exit(main())
