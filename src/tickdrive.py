"""TickDriver — R16-A: the TWO GLib sources that replaced the fixed 10 ms
window tick, and nothing else.  Every interval DECISION is a pure function
in src/cadence.py; this module only applies answers to GLib sources,
measures the clocks, and exposes what it is currently running at.

The CHROME source runs `win.tick_reveal()` (ChromeState.tick + the opacity
crossfade):
  * 10 ms while a fade is in flight, an OSD/toast deadline is live, or the
    400 ms hold after pointer activity / a state change is running;
  * 100 ms while the chrome is visible and nothing is moving;
  * NO SOURCE AT ALL (`current_ms is None`) while the chrome is hidden with
    nothing pending — the state N idle windows live in.

The PUMP source runs `win.tick_pump()` (render-flag drain, mpv event
queue, URL-resolver and next-probe drains, engagement feed):
  * 10 ms while mpv is producing frames (or a hold is running, or the R2-P2
    engagement watcher is armed — its rhythm predates this round);
  * 100 ms while it is not (paused, no media, video track suppressed).
It is NEVER suspended: with no source the picture would freeze and
`end_file`/playlist advance and resolver results would never be delivered.
Splitting the two is what made suspending the chrome legal in the first
place.

`arm()` is the only way the cadence changes from outside, and EVERY input
path calls it before any visual work — pointer motion/click/hover/leave,
key, menu open/close, focus, the observed play/pause edge (they reach it
through `Chrome.on_activity`) — so a suspended chrome source cannot eat the
first motion after an idle stretch: the wake-up re-adds the source and
applies the reveal decision in the same main-loop pass.

Gate surface: `current_ms` / `pump_current_ms` / `debug_state()`, and
`pin_ms` (REELSEE_TICK_PIN_MS or `pin()`) which restores the pre-R16 fixed
cadence so a gate can measure the ladder against the old behaviour in the
same process.
"""
from __future__ import annotations

import logging
import time

from gi.repository import GLib

import cadence

log = logging.getLogger("reelsee.tick")


class TickDriver:
    """Owns the sources; `win` is the ReelseeWindow (duck-typed: `view`,
    `core`, `chrome`, `_closing`, `tick_pump()`, `tick_reveal()`)."""

    def __init__(self, win, pin_ms=None):
        self.win = win
        self.pin_ms = pin_ms
        self.current_ms: int | None = None       # chrome source (None = no source)
        self.pump_current_ms: int | None = None  # pump source (never None once started)
        self._chrome_id = None
        self._pump_id = None
        self._idle_id = None          # same-pass reveal after a wake-up
        self._chrome_epoch = 0        # bumped whenever a source is replaced
        self._pump_epoch = 0
        self._hold_until_ms = 0.0     # on the ChromeState clock
        self._rate = 1.0              # observed state-clock rate (>= 1)
        self._state_ms: float | None = None
        self._real_ms: float | None = None
        self._in_chrome_cb = False

    # ------------------------------------------------------------------ clocks
    def _state_now(self) -> float:
        return float(self.win.chrome.state.clock())

    def measure_rate(self) -> None:
        """Estimate how fast the injected ChromeState clock runs vs. real
        time (cadence.clock_rate).  1.0 for the app; smoke_idlechrome.py
        runs it 20x, and the ladder clamps itself so the gap the STATE
        MACHINE sees stays inside its R13 tick-gap contract."""
        s = self._state_now()
        r = time.monotonic() * 1000.0
        if self._state_ms is not None:
            rate = cadence.clock_rate(s - self._state_ms, r - self._real_ms)
            if rate is not None:
                self._rate = rate
        self._state_ms, self._real_ms = s, r

    # -------------------------------------------------------------- intervals
    def _want_chrome(self) -> int | None:
        c = self.win.chrome
        return cadence.chrome_interval_ms(
            self._state_now(), self._hold_until_ms, c.animating,
            bool(c.state.visible or c.bars_visible),
            rate=self._rate, pin_ms=self.pin_ms)

    def _want_pump(self) -> int:
        eng = getattr(self.win, "_engagement", None)
        return cadence.pump_interval_ms(
            self.win.view.frame_age_ms(), self._state_now(),
            self._hold_until_ms, bool(eng is not None and eng.armed),
            pin_ms=self.pin_ms)

    def _apply(self) -> None:
        """Push the ladder onto the GLib sources (idempotent).

        There is NO `g_source_set_interval` on the introspection bridge (it
        is a C macro), so a rung CHANGE is destroy + re-add.  Both only
        happen on a TRANSITION — activity, a hide, a fade landing — so a
        steady-state window never churns its sources.  Destroying the
        currently-dispatching source is documented-safe (g_main_context
        re-reads the destroyed flag), and the epoch guard in the callbacks
        makes an in-flight one remove itself instead of re-arming a source
        that is already gone.
        """
        c = self._want_chrome()
        if self._chrome_id is None:
            if c is not None:
                self._chrome_epoch += 1
                self._chrome_id = GLib.timeout_add(c, self._chrome_cb)
                self.current_ms = c
        elif c is None:
            if not self._in_chrome_cb:
                GLib.source_remove(self._chrome_id)
            # else: inside its own callback — clearing the id makes that
            # callback return False and GLib removes the source itself.
            self._chrome_id = None
            self.current_ms = None
        elif self.current_ms != c:
            self._chrome_epoch += 1
            GLib.source_remove(self._chrome_id)
            self._chrome_id = GLib.timeout_add(c, self._chrome_cb)
            self.current_ms = c
        p = self._want_pump()
        if self._pump_id is None:
            self._pump_id = GLib.timeout_add(p, self._pump_cb)
            self.pump_current_ms = p
        elif self.pump_current_ms != p:
            self._pump_epoch += 1
            GLib.source_remove(self._pump_id)
            self._pump_id = GLib.timeout_add(p, self._pump_cb)
            self.pump_current_ms = p

    # ------------------------------------------------------------------- life
    def start(self) -> None:
        self.measure_rate()
        self._apply()

    def stop(self) -> None:
        """Kill every source.  Called from close-request; the `_closing`
        guard in the callbacks is the belt for the shutdown path that
        never fires that signal."""
        for attr in ("_chrome_id", "_pump_id", "_idle_id"):
            sid = getattr(self, attr, None)
            if sid is not None:
                GLib.source_remove(sid)
                setattr(self, attr, None)
        self.current_ms = None
        self.pump_current_ms = None

    def pin(self, ms) -> None:
        """Gate knob: fix BOTH sources at `ms` (None = run the ladder)."""
        self.pin_ms = int(ms) if ms else None
        if not self.win._closing:
            self.arm("pin")

    def arm(self, why: str = "") -> None:
        """THE re-arm path.  Stamps the hold window, puts both sources on
        the fast interval (RE-ADDING a suspended chrome source) and, if the
        chrome is currently hidden, applies the fresh reveal decision on an
        idle — the same main-loop pass, before any fade work — so the first
        pointer motion after an idle stretch is INSTANTLY responsive."""
        self._hold_until_ms = self._state_now() + cadence.TICK_HOLD_MS
        if self.win._closing:
            return
        wake = self.current_ms is None and not self.win.chrome.bars_visible
        self._apply()
        if wake and self._idle_id is None:
            # PRIORITY_DEFAULT, not the default PRIORITY_DEFAULT_IDLE (200):
            # the whole point of this source is that it runs in the SAME pass
            # as the motion that woke us, and a 200-priority idle is skipped
            # for as long as ANY timeout is ready — which, with a 10 ms pump
            # and a slow draw, is forever (measured: 50 ms+ starved while
            # software-decoding 1080p, i.e. a first-motion latency the old
            # fixed-10 ms tick simply did not have).  At priority 0 it is
            # dispatched with the ready timeouts of the pass it woke in.
            self._idle_id = GLib.idle_add(self._reveal_idle,
                                          priority=GLib.PRIORITY_DEFAULT)

    # --------------------------------------------------------------- callbacks
    def _chrome_cb(self) -> bool:
        me = self._chrome_epoch
        win = self.win
        if win._closing:
            self._chrome_id = None
            return False
        self.measure_rate()
        self._in_chrome_cb = True
        try:
            win.tick_reveal()
            self._apply()
        finally:
            self._in_chrome_cb = False
        # False == suspended (no source at all) OR this dispatch was
        # superseded by a retune — the replacement owns the cadence now.
        return self._chrome_id is not None and me == self._chrome_epoch

    def _pump_cb(self) -> bool:
        me = self._pump_epoch
        win = self.win
        if win._closing:
            self._pump_id = None
            return False
        self.measure_rate()
        win.tick_pump()
        self._apply()
        return me == self._pump_epoch

    def _reveal_idle(self) -> bool:
        self._idle_id = None
        if self.win._closing:
            return False
        self.win.tick_reveal()
        self._apply()
        return False

    # ------------------------------------------------------------- gate view
    def debug_state(self) -> dict:
        """What the tickrate gate reads (never a re-derivation — these are
        the values applied to the sources)."""
        return {"chrome_ms": self.current_ms,
                "pump_ms": self.pump_current_ms,
                "rate": round(self._rate, 3),
                "hold_ms": round(max(0.0, self._hold_until_ms
                                     - self._state_now()), 1),
                "pin_ms": self.pin_ms,
                "animating": bool(self.win.chrome.animating),
                "bars_visible": bool(self.win.chrome.bars_visible)}
