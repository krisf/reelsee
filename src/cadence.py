"""R16-A: the adaptive tick-cadence ladder.  PURE (no gi, no GLib, no mpv):
every interval DECISION lives here and is unit-tested in
tests/test_cadence.py; ``window.py`` only applies the answers to GLib
sources and reads their clocks.

Why this exists
---------------
Every window used to run ONE 10 ms (100 Hz) ``GLib.timeout_add`` forever —
reveal decision, render pump, mpv event drain, URL-resolver drain —
whether or not anything could be seen.  With N windows that is N x 100 Hz
of Python through a single GIL, in the same process that runs the decode
pipeline.  The ladder keeps the fast cadence only where a human can
actually see a difference:

``10 ms``
    a reveal/hide fade is in flight, an OSD or toast still has an ARMED
    auto-hide deadline (only ``Chrome.tick()`` retires those, and it retires
    them once the deadline has ALREADY passed — see
    ``Chrome.transient_pending``), or we are inside the ``TICK_HOLD_MS``
    window that follows pointer activity or a state change (play/pause,
    focus, new file, menu open/close);
``100 ms``
    chrome visible and nothing moving: the idle-hide countdown keeps
    tens-of-ms resolution and every drain stays responsive;
``None`` (SUSPENDED)
    the chrome source has NO source attached at all while the chrome is
    hidden, nothing animates and no transient is pending.  Re-arming is the
    job of the input paths (motion/click/hover/key/menu/focus/playing
    changed), which call the driver's ``arm`` BEFORE doing any visual work.

Why the driver is TWO sources
-----------------------------
Suspension is only legal because the work was split.  The CHROME source may
stop; the PUMP source (mpv ``render_context_update`` drain + mpv event
queue drain + resolver/probe drains) must never stop while the window
exists, or video would freeze, ``end_file``/playlist advance would stall
and resolver results would never land.  The pump runs at 100 ms whenever
nothing is producing frames and drops back to 10 ms the moment mpv is.

The gap guard
-------------
``ChromeState.tick()`` (R13) reads a tick-to-tick gap on its OWN clock
larger than ``chrome.TICK_GAP_MAX_MS`` as a clock discontinuity and
re-arms the idle countdown.  100 ms of real time is 100 ms of state clock
for a wall clock (harmless), but ``scripts/smoke_idlechrome.py`` runs
ChromeState 20x fast-forwarded, where the same interval is a 2000 ms gap —
exactly the threshold, and a jitter-prone way to keep chrome visible
forever.  So the driver measures the state clock's RATE and clamps every
chrome interval so the gap as SEEN BY THE STATE MACHINE stays under
``GAP_GUARD_SIM_MS``.  With a wall clock the clamp is inert (100 ms of
interval is 100 ms of gap); with a fast-forwarded clock the ladder scales
down — 20x yields 10/12 ms, i.e. essentially the pre-R16 cadence — and R13's
contract keeps holding exactly as before.
"""
from __future__ import annotations

TICK_FAST_MS = 10           # something is moving (or just did)
TICK_SLOW_MS = 100          # visible and stable
TICK_HOLD_MS = 400.0        # how long "just moved" stays true (state-clock ms)
PUMP_FRAME_WINDOW_MS = 200.0  # "mpv is producing frames" memory (real time)
# Worst state-clock gap we will ever hand ChromeState.  It has to be far
# BELOW the machine's discontinuity threshold (chrome.TICK_GAP_MAX_MS =
# 2000), and far below the idle timeout a fast-forwarded test drives: the
# 20x clock of scripts/smoke_idlechrome.py (timeout 3000 SIMULATED ms) also
# runs on a main loop that starves timers by 5-10x under software 1080p
# decode, so a 100 ms nominal interval was measured firing ~100-150 ms apart
# — 2000-3000 SIMULATED ms, which re-armed the countdown before it could
# ever elapse and left the chrome shown for 21 real seconds (the gate needs
# 5).  At 250 sim ms the 20x clock gets 10/12 ms — i.e. essentially the
# pre-R16 cadence, which is exactly right: the ladder is a REAL-TIME
# optimisation, and a harness that fast-forwards the state machine is not
# asking to have its tick budget cut.  With a wall clock (rate 1) the clamp
# is inert and the ladder is the advertised 10/100/suspended.
GAP_GUARD_SIM_MS = 250.0
MAX_RATE = 200.0            # sanity band: a faster "clock" is a swapped clock
PIN_ENV = "REELSEE_TICK_PIN_MS"


def pin_from_env(environ) -> int | None:
    """Baseline knob: ``REELSEE_TICK_PIN_MS=10`` pins EVERY source to the
    old fixed cadence (R16 behaviour off, faithfully).  ``scripts/
    smoke_tickrate.py`` measures the ladder against the pre-R16 cadence in
    the SAME process using this, so the baseline number is a real run and
    not an assertion.  The pin is deliberately NOT gap-guarded: it
    reproduces the old build exactly."""
    raw = str(environ.get(PIN_ENV) or "").strip()
    if not raw:
        return None
    try:
        ms = int(float(raw))
    except ValueError:
        return None
    return ms if ms > 0 else None


def clock_rate(state_gap_ms, real_gap_ms) -> float | None:
    """Observed speed of the injected ChromeState clock vs. real time.

    ``None`` = no usable sample (too short to measure, or a backwards
    state clock); the caller keeps its previous estimate."""
    if state_gap_ms is None or real_gap_ms is None:
        return None
    if real_gap_ms < 5.0 or state_gap_ms < 0.0:
        return None
    r = float(state_gap_ms) / float(real_gap_ms)
    # MAX_RATE is a REJECT band, not a clamp: the one garbage sample a test
    # harness produces when it swaps the clock out from under us mid-run (a
    # 5 s wall gap measured against a freshly installed 20x clock reads
    # ~10000x) would otherwise pin the ladder at its floor for an interval.
    # Returning None keeps the previous estimate, which was fine a moment ago.
    if r > MAX_RATE:
        return None
    return max(1.0, r)


def clamp_gap(interval_ms, rate=None, guard_ms: float = GAP_GUARD_SIM_MS):
    """Fastest-wins: never let the STATE-CLOCK gap between two chrome
    ticks exceed ``guard_ms`` (see GAP_GUARD_SIM_MS above for why 250).
    Inert for a wall clock (rate <= 1)."""
    if interval_ms is None:
        return None
    r = float(rate) if (rate or 0.0) > 1.0 else 1.0
    return max(1, min(int(interval_ms), int(guard_ms / r)))


def chrome_interval_ms(now_ms, hold_until_ms, animating: bool,
                       visible: bool, rate=None, pin_ms=None) -> int | None:
    """The ladder for the CHROME source.  ``None`` means: no source at all.

    ``now_ms``/``hold_until_ms`` are on the ChromeState clock (that is what
    a "state change" is stamped with); ``animating`` covers fades in flight
    AND armed OSD/toast auto-hide deadlines — retiring those is tick work
    (and happens AFTER the deadline passes), so suspending with one armed
    would strand a visible overlay.
    """
    if pin_ms:
        return int(pin_ms)
    if animating or (now_ms is not None and now_ms < hold_until_ms):
        want = TICK_FAST_MS
    elif visible:
        want = TICK_SLOW_MS
    else:
        return None
    return clamp_gap(want, rate)


def pump_interval_ms(frames_age_ms, now_ms, hold_until_ms,
                     engagement_armed: bool = False, pin_ms=None) -> int:
    """The ladder for the PUMP source — never ``None`` while the window
    lives: video frames and mpv events must always be drained.

    Fast while mpv is handing us frames (``frames_age_ms`` = real-time age
    of the last frame-flag read), while a state-change hold is running, or
    while the R2-P2 engagement watcher is armed (its sampling rhythm is
    pre-R16 and is not this round's to change).  100 ms otherwise: audio-
    only, paused, no media, or a suppressed video track.
    """
    if pin_ms:
        return int(pin_ms)
    if engagement_armed:
        return TICK_FAST_MS
    if now_ms is not None and now_ms < hold_until_ms:
        return TICK_FAST_MS
    if frames_age_ms is not None and frames_age_ms < PUMP_FRAME_WINDOW_MS:
        return TICK_FAST_MS
    return TICK_SLOW_MS
