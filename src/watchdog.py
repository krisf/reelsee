"""Overload watchdog (PLAN Appendix C, R-7) — pure decide() + sampler +
applier.

One 1 s sample per tick (fields from the observed Snapshot only — R10).
Three consecutive overloaded samples (after the startup grace, never while
pause/seeking/idle/eof/container_fps==0/hwdec already "no") demote ONE step
in the fallback chain, then a 3 s cooldown and re-arm.  Applying is the
P0-PROOF-A mechanism (set hwdec + vid no/auto — never a seek, never audio).

`decide()`-style state lives in Watchdog.feed(); every rule is pure given
(Sample, thresholds, mode, clock) so tests drive it with fake clocks.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

log = logging.getLogger("reelsee.watchdog")

GRACE_S = 4.0                 # max(4 s, 2 × 1-s fps sample window)
CONFIRM = 3                   # consecutive overloaded samples
COOLDOWN_S = 3.0              # wait after an action, then re-arm

#: demote chains (Appendix C); key = current hwdec mode base
CHAINS = {
    "vaapi": ["vaapi-copy", "no"],
    "vaapi-copy": ["no"],
    "vulkan": ["vulkan-copy", "vaapi", "vaapi-copy", "no"],
    "vulkan-copy": ["vaapi", "vaapi-copy", "no"],
    "auto-safe": ["vaapi-copy", "no"],
    "auto": ["vaapi-copy", "no"],
    "no": [],                 # terminal — never demote further
    "cpu": [],
}


def mode_base(hwdec_current) -> str:
    """"vaapi(h264)" → "vaapi"; None/"no" → "no" (terminal, never act)."""
    s = str(hwdec_current or "no").split("(", 1)[0].strip()
    return s or "no"


#: observed hwdec-current values that mean hardware decode is REALLY on
#: (R2-P2/I1 — anything else, notably "no", means the configured GPU
#: engine silently failed to engage).
ENGAGED_MODES = frozenset({"vaapi", "vaapi-copy", "vulkan", "vulkan-copy"})

#: R7: the brief's name for THE authoritative hardware-on classifier —
#: mode_base() + HW_REAL_ON.  ONE classifier (no second heuristic): the
#: decode indicator and the engagement watchdog both read it.
HW_REAL_ON = ENGAGED_MODES


def decode_effective(hwdec_current) -> str:
    """Classify an OBSERVED `hwdec-current` snapshot value with the ONE
    authoritative classifier (R7): "on" = hardware decode really running
    (HW_REAL_ON), "off" = observed "no", "unknown" = nothing observed yet
    (before the first frame / FILE_LOADED) or a value in neither set.
    Never looks at the configured/requested mode — that is the point
    (R2-P2/I1: a configured GPU engine that silently failed must read
    CPU, never GPU)."""
    if hwdec_current is None or not str(hwdec_current).strip():
        return "unknown"
    base = mode_base(hwdec_current)
    if base in HW_REAL_ON:
        return "on"
    if base == "no":
        return "off"
    return "unknown"


class FallbackChain:
    """Shared ordered mode ladder for one playback session (R2-P2).

    ONE source of truth for "what have we already tried": the engagement
    watcher (engagement deadline) and the overload watchdog (demote) both
    step through the SAME chain, so a retry after a failed engagement can
    never re-apply a mode that already failed.  Pure state — timing lives
    in the consumers."""

    def __init__(self, configured_mode):
        self.configured = mode_base(configured_mode)
        self.current = self.configured
        self.exhausted = self.configured == "no"
        self.fell_back = False           # any step taken off `configured`
        self.fell_from: str | None = None

    def next_step(self) -> str | None:
        """Advance one step down the chain; None when exhausted (CPU)."""
        if self.exhausted:
            return None
        chain = CHAINS.get(self.current, CHAINS["auto"])
        if not chain:                    # unknown mode: step toward auto
            chain = CHAINS["auto"]
        prev, nxt = self.current, chain[0]
        self.current = nxt
        self.fell_back = True
        self.fell_from = prev
        if nxt == "no":
            self.exhausted = True        # terminal — never demote past CPU
        return nxt

    def sync(self, observed_base: str) -> None:
        """Re-key the ladder to an ENGAGED observed mode (watchdog demotes
        from what is actually running)."""
        if observed_base in ENGAGED_MODES:
            self.current = observed_base
            self.exhausted = False


@dataclass(frozen=True)
class Sample:
    t: float
    playing: bool
    pause: bool
    seeking: bool
    idle: bool
    eof: bool
    vf_fps: float | None
    container_fps: float | None
    drop: int
    dec_drop: int
    hwdec: str | None = None
    # R17-D: buffering state, observed-only (mpvcore's OBSERVED_BASE —
    # never a synchronous getter).  APPENDED at the end so every existing
    # positional Sample(...) construction keeps working.
    paused_for_cache: bool = False
    cache_buffering_state: int | None = None

    def skip(self) -> bool:
        # Fully-ignored samples.  hwdec "no" is NOT skipped here: Appendix C
        # still wants the terminal CPU case ("toast if still dropping"), and
        # CHAINS["no"] is empty so a demotion from "no" can never happen —
        # "never demote while hwdec_current is already no" holds either way.
        return (self.pause or self.seeking or self.idle or self.eof
                or not self.playing or not self.container_fps)

    def buffering(self) -> bool:
        """R17-D: this sample was taken while mpv was still filling its
        cache — see `is_buffering`."""
        return is_buffering(self.paused_for_cache, self.cache_buffering_state)


def is_buffering(paused_for_cache, cache_buffering_state) -> bool:
    """R17-D: "the file is still buffering" — mpv is either paused for
    cache (`paused-for-cache`) or still filling its initial cache
    (`cache-buffering-state` < 100, the percentage mpv reports).

    EITHER signal is enough: mpv re-pauses and resumes around cache
    refills, so `paused-for-cache` can flicker to false mid-buffering while
    the fill percentage is still climbing — and a stream that has already
    finished its fill can still pause for cache later.  A missing
    (`None`) percentage means "not observed yet", which is NOT buffering:
    treating unknown as buffering would freeze the watchdog forever on a
    build/source that never emits the property.
    """
    if bool(paused_for_cache):
        return True
    if cache_buffering_state is None:
        return False
    try:
        return float(cache_buffering_state) < 100.0
    except (TypeError, ValueError):
        return False


def ratios(s: Sample, prev: Sample, dt: float):
    """(fps_ratio|None, drop_ratio) per Appendix C; drop uses frame counts
    only.  fps_ratio is None when estimated-vf-fps is null (not-yet-running
    video output) — then only the drop-ceiling path can fire."""
    fps_ratio = (s.vf_fps / s.container_fps) if s.vf_fps else None
    frame_delta = (s.vf_fps or s.container_fps or 0.0) * dt
    dropped = max(0, s.drop - prev.drop) + max(0, s.dec_drop - prev.dec_drop)
    drop_ratio = dropped / max(frame_delta, 1.0)
    return fps_ratio, drop_ratio


def raw_counters(s: Sample, prev: Sample, dt: float) -> dict:
    """D2: the RAW numbers behind every decision — absolute counter values,
    the deltas, dt — so a positive decision is diagnosable from the log
    alone (the false-positive hunt needed exactly this)."""
    frame_delta = (s.vf_fps or s.container_fps or 0.0) * dt
    dropped = max(0, s.drop - prev.drop) + max(0, s.dec_drop - prev.dec_drop)
    return {"drop": s.drop, "dec_drop": s.dec_drop,
            "d_drop": max(0, s.drop - prev.drop),
            "d_dec_drop": max(0, s.dec_drop - prev.dec_drop),
            "dropped": dropped, "frame_delta": round(frame_delta, 2),
            "dt": round(dt, 3)}


class Watchdog:
    """Feed one Sample per second; feed() returns an ACTION dict when the
    rules fire, else None.  clock() is injectable for unit tests."""

    def __init__(self, enabled: bool = True, fps_floor_pct: float = 90.0,
                 drop_ceiling_pct: float = 3.0, clock=time.monotonic):
        self.enabled = bool(enabled)
        self.fps_floor = float(fps_floor_pct) / 100.0
        self.drop_ceiling = float(drop_ceiling_pct) / 100.0
        self.clock = clock
        self._grace_until = 0.0
        self._streak = 0
        self._cool_until = 0.0
        self._prev: Sample | None = None
        self._demoted = False           # any demotion applied this session
        self._terminal_toasted = False
        self.last_action: dict | None = None
        # R2-P2/I1 engine context (set per file by the window):  what the
        # CONFIGURED engine is decides whether hwdec-"no" samples are
        # ignorable at all.  chain=None + is_cpu=False keeps the legacy
        # (pre-R2) behaviour for callers/tests that do not wire it.
        self._chain: FallbackChain | None = None
        self._cfg_cpu = False
        # R7-B: while the USER pins CPU for the current video (the
        # bottom-right decode toggle), the automatic demote/promote
        # machinery is OFF entirely — nothing may switch the decoder
        # behind the user's back.  Owned/reset by the window.
        self.suspended = False

    def set_engine(self, chain: "FallbackChain | None" = None,
                   is_cpu: bool = False) -> None:
        """Wire the configured-engine state (window calls per FILE_LOADED):
        `chain` = shared FallbackChain for the configured GPU engine,
        `is_cpu` = configured engine is CPU (samples then skipped entirely —
        nothing left to switch to)."""
        self._chain = chain
        self._cfg_cpu = bool(is_cpu) and chain is None

    # ------------------------------------------------------------------ hooks
    def file_loaded(self) -> None:
        """Startup grace + full re-arm per file (Appendix C)."""
        self._grace_until = self.clock() + GRACE_S
        self._streak = 0
        self._prev = None
        self._cool_until = 0.0
        self._terminal_toasted = False

    def retry_pending(self, retry_on_new_file: bool) -> bool:
        """True when this FILE_LOADED should restore the configured engine
        (a demotion happened AND the config switch allows retry); consuming
        the flag so each demotion retries exactly once."""
        if self._demoted and bool(retry_on_new_file):
            self._demoted = False
            return True
        return False

    # ------------------------------------------------------------------- feed
    def feed(self, s: Sample) -> dict | None:
        if not self.enabled:
            return None
        now = self.clock()
        prev, self._prev = self._prev, s
        if self.suspended:
            # R7-B: user CPU override active for this video — the automatic
            # machinery does not demote AND cannot "promote" (the retry path
            # below would otherwise step the chain from "no").  Baseline is
            # kept fresh so release after a toggle-back starts clean.
            self._streak = 0
            return None
        if now < self._grace_until or s.skip():
            return None
        if s.buffering():
            # R17-D: NEVER demote while the file is still buffering.  The
            # sample is not counted at all — the confirm streak neither
            # increments (a buffering stall is not overload) nor resets
            # (a startup buffer-fill must not wash out a real overload
            # streak, and a real overload interrupted by a buffering gap
            # still demotes once it resumes).  `_prev` was already updated
            # above, so the next non-buffering sample measures its delta
            # against the newest sample rather than against a stale one.
            log.debug("watchdog: buffering sample (paused-for-cache=%s "
                      "cache-buffering-state=%s) — streak frozen at %d",
                      s.paused_for_cache, s.cache_buffering_state,
                      self._streak)
            return None
        base = mode_base(s.hwdec)
        if base == "no" and (self._cfg_cpu
                             or (self._chain is not None
                                 and self._chain.exhausted)):
            # R2-P2/I1 corrected skip rule: a "no" sample is ignorable ONLY
            # when the CONFIGURED engine is cpu, or the fallback chain is
            # exhausted.  A GPU engine that failed to engage is NOT silently
            # skipped forever any more — the retry path below can act on it.
            return None
        if prev is None or prev.skip():
            return None
        dt = max(0.001, s.t - prev.t)
        fps_ratio, drop_ratio = ratios(s, prev, dt)
        raw = raw_counters(s, prev, dt)
        # D2 (R7): the drop-ceiling path may ONLY fire when fps_ratio is
        # also below the floor, or unknown (no estimated-vf-fps yet).  A
        # healthy fps with a big burst of drop-counter movement — a seek
        # flush, a counter saturation artifact — must NEVER demote: that
        # was the observed false positive (fps_ratio=1.00, drop_ratio=0.97
        # demoted).  fps below the floor remains the one positive signal.
        fps_low = (fps_ratio is not None and fps_ratio < self.fps_floor)
        overloaded = fps_low or (fps_ratio is None
                                 and drop_ratio > self.drop_ceiling)
        if not overloaded:
            self._streak = 0            # ANY clean sample resets confirm
            return None
        # D2: every positive decision carries/logs its RAW inputs.
        log.debug("watchdog: overload signal fps_ratio=%s drop_ratio=%s "
                  "frame_drop=%s dec_drop=%s d_frame_drop=%s "
                  "d_dec_drop=%s dropped=%s frame_budget=%s dt=%s hwdec=%s",
                  round(fps_ratio, 4) if fps_ratio is not None else None,
                  round(drop_ratio, 4), raw["drop"], raw["dec_drop"],
                  raw["d_drop"], raw["d_dec_drop"], raw["dropped"],
                  raw["frame_delta"], raw["dt"], s.hwdec)
        if now < self._cool_until:
            return None                 # don't count during the 3 s wait
        self._streak += 1
        if self._streak < CONFIRM:
            return None
        frm = mode_base(s.hwdec)
        if (frm == "no" and self._chain is not None
                and not self._chain.exhausted):
            # R2-P2 retry: configured GPU engine, hardware decode NOT
            # engaged, sustained overload → try the next chain step with
            # the proven mechanism (never re-try a mode already used).
            from_mode = self._chain.current
            step = self._chain.next_step()
            if step is None:
                return None
            act = {"kind": "demote", "from": from_mode, "to": step,
                   "retry": True,
                   "fps_ratio": round(fps_ratio, 4) if fps_ratio
                   is not None else None,
                   "drop_ratio": round(drop_ratio, 4), "raw": raw}
            self._streak = 0
            self._cool_until = now + COOLDOWN_S
            self._demoted = True
            self.last_action = act
            log.info("watchdog: RETRY decision %s → %s (healthy-fps gate "
                     "passed: fps_ratio=%s drop_ratio=%s)", from_mode, step,
                     act["fps_ratio"], act["drop_ratio"])
            return act
        chain = CHAINS.get(frm)
        if chain is None:               # unknown hwaccel → treat like auto
            chain = CHAINS["auto"]
        if not chain:                   # terminal (cpu / already "no")
            if not self._terminal_toasted:
                self._terminal_toasted = True
                act = {"kind": "toast",
                       "text": "Playback still dropping — CPU decode is "
                               "the limit for this file", "raw": raw}
                self.last_action = act
                log.info("watchdog: terminal toast (fps_ratio=%s "
                         "drop_ratio=%s raw=%s)", act.get("fps_ratio"),
                         act.get("drop_ratio"), raw)
                return act
            return None
        act = {"kind": "demote", "from": frm, "to": chain[0],
               "fps_ratio": round(fps_ratio, 4) if fps_ratio is not None
               else None,
               "drop_ratio": round(drop_ratio, 4), "raw": raw}
        if self._chain is not None:
            # keep the shared ladder keyed to what is actually applied,
            # so a later engagement failure continues DOWNWARD only
            self._chain.sync(frm)
            self._chain.fell_from = frm
            self._chain.fell_back = True
            self._chain.current = chain[0]
            if chain[0] == "no":
                self._chain.exhausted = True
        self._streak = 0
        self._cool_until = now + COOLDOWN_S
        self._demoted = True
        self.last_action = act
        log.info("watchdog: DEMOTE decision %s → %s (fps_ratio=%s "
                 "drop_ratio=%s frame_drop=%s dec_drop=%s dropped=%s "
                 "dt=%s)", frm, chain[0], act["fps_ratio"],
                 act["drop_ratio"], raw["drop"], raw["dec_drop"],
                 raw["dropped"], raw["dt"])
        return act


class Sampler:
    """Snapshot → Sample (observed values ONLY — R10, never a getter).

    simulate=True (the --simulate-overload test flag) yields synthetic
    overloaded samples on a simulated vaapi engine: steady half-rate fps and
    real growing drop counters, 1 s apart — enough to drive every rule
    end-to-end headless, where a real GPU overload cannot be reproduced."""

    def __init__(self, core, simulate: bool = False, container: float = 30.0,
                 clock=None):
        self.core = core
        self.simulate = simulate
        self.container = container
        self.clock = clock or time.monotonic
        self._t0 = self.clock()
        self._n = 0

    def sample(self) -> Sample:
        now = self.clock()
        if self.simulate:
            self._n += 1
            frame_delta = self.container * 0.5          # vf at half rate
            return Sample(
                t=now, playing=True, pause=False, seeking=False,
                idle=False, eof=False, vf_fps=self.container * 0.5,
                container_fps=self.container,
                drop=int(self._n * frame_delta * 0.5), dec_drop=0,
                hwdec="vaapi")
        snap = self.core.snapshot
        return Sample(
            t=now,
            playing=bool(snap.get("duration")),
            pause=bool(snap.get("pause")),
            seeking=bool(snap.get("seeking")),
            idle=bool(snap.get("idle-active")) or bool(
                snap.get("core-idle")),
            eof=bool(snap.get("eof-reached")),
            vf_fps=snap.get("estimated-vf-fps"),
            container_fps=snap.get("container-fps"),
            drop=int(snap.get("frame-drop-count") or 0),
            dec_drop=int(snap.get("decoder-frame-drop-count") or 0),
            hwdec=snap.get("hwdec-current"),
            # R17-D: observed buffering state (OBSERVED_BASE in mpvcore —
            # this reads the snapshot, never a getter).
            paused_for_cache=bool(snap.get("paused-for-cache")),
            cache_buffering_state=(
                None if snap.get("cache-buffering-state") is None
                else int(snap.get("cache-buffering-state"))))


class EngagementWatcher:
    """R2-P2/I1 — did the configured GPU engine ACTUALLY engage?

    Armed on MPV_EVENT_FILE_LOADED; the window feeds it the OBSERVED
    hwdec-current from the Snapshot on every main-thread poll (R10:
    snapshot only).  If, WATCH_S after arming (or after a fallback step),
    the observed mode is not one of ENGAGED_MODES, ONE fallback step is
    applied via the proven mechanism (the `apply` callback is
    core.apply_hwdec: set hwdec + vid no → vid auto, position preserved),
    then the watcher re-arms its deadline — repeating until something
    engages or the chain is exhausted at CPU.  Pure logic: tests drive a
    fake snapshot sequence + a recording apply callback with a fake clock
    (mpv is NEVER mocked)."""

    WATCH_S = 3.0

    def __init__(self, chain: FallbackChain, apply_hwdec,
                 on_action=None, clock=time.monotonic):
        self.chain = chain
        self.apply = apply_hwdec
        self.on_action = on_action or (lambda _a: None)
        self.clock = clock
        self.armed = False
        self._deadline = 0.0
        # R17-D: the instant of the last feed, so a buffering gap can be
        # SUBTRACTED from the deadline instead of being charged to it.
        self._last_feed: float | None = None

    def arm(self, now: float | None = None) -> None:
        self.armed = True
        t = self.clock() if now is None else now
        self._deadline = t + self.WATCH_S
        self._last_feed = t

    def disarm(self) -> None:
        self.armed = False

    def feed(self, observed, now: float | None = None,
             buffering: bool = False) -> dict | None:
        """observed = snapshot hwdec-current (string|None).  Returns the
        action dict on state transitions, else None.

        R17-D: `buffering=True` (mpv still filling its cache) FREEZES the
        deadline — the elapsed gap is added back to it instead of being
        charged against the engine.  A GPU engine that engages late only
        because the file was still buffering is not an engagement failure;
        the timeout must measure time spent waiting on a DECODER, not time
        spent waiting on the network.
        """
        if not self.armed:
            return None
        now = self.clock() if now is None else now
        if buffering:
            gap = 0.0 if self._last_feed is None else max(0.0,
                                                          now - self._last_feed)
            self._deadline += gap
            self._last_feed = now
            return None
        self._last_feed = now
        base = mode_base(observed)
        if base in ENGAGED_MODES:
            self.armed = False
            act = {"kind": "engaged", "mode": base,
                   "fell_back": self.chain.fell_back}
            if self.chain.fell_back:
                act["from"] = self.chain.fell_from
            self.on_action(act)
            return act
        if now < self._deadline:
            return None
        from_mode = self.chain.current
        step = self.chain.next_step()
        if step is None:
            self.armed = False
            act = {"kind": "gave-up", "mode": from_mode}
            self.on_action(act)
            return act
        self.apply(step)                # proven P0-PROOF-A mechanism
        if step == "no" or self.chain.exhausted:
            self.armed = False
            act = {"kind": "exhausted", "from": from_mode, "to": step}
        else:
            self._deadline = now + self.WATCH_S
            act = {"kind": "fallback", "from": from_mode, "to": step}
        self.on_action(act)
        return act


def apply_action(window, action: dict, cfg) -> None:
    """Demote on the LIVE handle (P0-PROOF-A: set hwdec + vid toggle — no
    seek, no reload, no audio), persist engine.last_fallback, toast."""
    if action.get("kind") == "toast":
        window.chrome.show_toast(action["text"])
        return
    frm, to = action["from"], action["to"]
    window.core.apply_hwdec(to)
    entry = {"from": frm, "to": to, "ts": int(time.time()),
             "fps_ratio": action["fps_ratio"],
             "drop_ratio": action["drop_ratio"]}
    cfg.set("engine.last_fallback", entry)
    try:                                # R2-P2: engines.json too (§dispatch)
        import engine as _E
        _E.record_last_fallback(dict(entry, reason="overload"))
    except Exception:                   # never break the fallback path
        log.exception("engines.json last_fallback record failed")
    window.chrome.show_toast(
        f"Overload: decoder switched {frm} → {to}")
    raw = action.get("raw") or {}
    log.info("watchdog: demoted %s → %s (fps_ratio=%s drop_ratio=%s "
             "frame-drop-count=%s decoder-frame-drop-count=%s "
             "dropped=%s dt=%s)", frm, to, action["fps_ratio"],
             action["drop_ratio"], raw.get("drop"), raw.get("dec_drop"),
             raw.get("dropped"), raw.get("dt"))
