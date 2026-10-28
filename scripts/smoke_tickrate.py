#!/usr/bin/python3
"""R16 gate (tickrate): the adaptive tick ladder, and what it buys.

Part 1 — THE LADDER.  The driver's CURRENT interval is the surface
(`ReelseeWindow.tick_interval_ms` / `.pump_interval_ms` — the value actually
applied to the GLib source, never a re-derivation).  Asserted, in order:

  motion         -> 10 ms, immediately, with the fade in flight;
  visible+stable -> 100 ms once the 400 ms hold and the 180 ms fade are over;
  hidden         -> NO SOURCE AT ALL (`None`), and provably no ticks: the
                    state machine's own `_last_tick_ms` must not advance;
  motion again   -> the FIRST motion re-arms 10 ms BEFORE any visual work
                    (the interval is sampled from inside `_apply_reveal`,
                    the first thing a reveal does), and the decision lands
                    in the same main-loop pass;
  no frames      -> the pump drops to its 100 ms rung (it is never
                    suspended — a window with no pump cannot advance a
                    playlist or deliver a resolver result).

Part 2 — THE CPU, MEASURED.  utime of this process over scripted 30 s
windows: the ladder vs the pre-R16 fixed cadence (`TickDriver.pin`, which
pins BOTH sources to 10 ms — equivalently `REELSEE_TICK_PIN_MS=10`), both
arms in the SAME process, the chrome hidden in both arms, in the state the
brief names: idle and hidden with nothing to show.  Headless llvmpipe
software-decodes 1080p at ~174 % CPU; that cost is identical in both arms
and swamps per-window Python bookkeeping, so the video track is suppressed
in BOTH arms (`vid=no` — exactly the state a minimized window is in) and
the number measures the tick, not the decoder.  The pair measured WITH the
video running is printed too (ungated, labelled): it is the honest statement
of how little of a decode-bound window's CPU the tick is.

Prints `R16_TD` / `R16_UTIME` lines for the report tables.
"""
import json
import os
import sys
import time

from smoke_harness import SCRATCH, check, finish

CFG = SCRATCH / "tickrate-cfg"
CFG.mkdir(parents=True, exist_ok=True)
(CFG / "config.json").write_text(json.dumps(
    {"ui": {"chrome_timeout_ms": 5000, "osd_ms": 1200}}))
os.environ["REELSEE_CONFIG_DIR"] = str(CFG)
os.environ["REELSEE_STATE_DIR"] = str(SCRATCH / "tickrate-state")

import reelsee  # noqa: E402

GATE_S = 30.0            # the brief's scripted 30 s idle-hidden window
INFO_S = 10.0            # the labelled "with decode running" pair
ARMS: dict = {}


def utime() -> float:
    """Process user CPU seconds (all threads — `times(2)` is per process)."""
    return os.times()[0]


def playing(win) -> bool:
    s = win.core.snapshot
    return (s.get("pause") is False and s.get("idle-active") is False
            and s.get("eof-reached") is not True)


def hidden(win) -> bool:
    return not win.chrome.state.visible and not win.chrome.bars_visible


def probe_schedule(app):
    from gi.repository import GLib

    def win():
        return app.windows[0]

    def poll(name, every, deadline_s, cond, then):
        """Generous polling, no fixed sleeps: wait for a condition."""
        tries = {"n": 0}

        def step():
            if cond():
                then()
                return False
            tries["n"] += 1
            if tries["n"] * every >= deadline_s * 1000:
                check(name, False, f"no within {deadline_s}s")
                app.quit()
                return False
            GLib.timeout_add(every, step)
            return False

        GLib.timeout_add(every, step)

    # ---------------------------------------------------------------- part 1
    def check_motion():
        w = win()
        w.chrome.motion(400, 300, 960, 540)          # the input path itself
        iv, anim = w.tick_interval_ms, w.chrome.animating
        print(f"R16_TD motion interval={iv} animating={anim} "
              f"pump={w.pump_interval_ms}", flush=True)
        check("motion_interval_10ms", iv == 10, str(iv))
        # (`animating` is the FADE rung; the bars were already up here, so no
        # fade started — it is asserted at the real hidden->shown wake below.)
        check("motion_hold_not_fade", not bool(anim), str(anim))
        check("pump_fast_while_frames", w.pump_interval_ms == 10,
              str(w.pump_interval_ms))
        GLib.timeout_add(700, check_stable)   # hold 400 + fade 180 + slack

    def check_stable():
        w = win()
        iv = w.tick_interval_ms
        vis = bool(w.chrome.state.visible and w.chrome.bars_visible)
        print(f"R16_TD stable-visible interval={iv} visible={vis}", flush=True)
        check("stable_visible_100ms", iv == 100 and vis,
              f"interval={iv} visible={vis}")
        w.chrome.state.timeout_ms = 600       # now let it hide on its own
        poll("hidden_suspends_source", 50, 8.0,
             lambda: w.tick_interval_ms is None, check_suspended)

    def check_suspended():
        w = win()
        base = w.chrome.state._last_tick_ms
        t0 = time.monotonic()

        def later():
            gap = w.chrome.state._last_tick_ms - base
            wall = time.monotonic() - t0
            print(f"R16_TD hidden wall={wall:.2f}s state_clock_advanced="
                  f"{gap:.0f}ms interval={w.tick_interval_ms}", flush=True)
            check("hidden_suspends_source", w.tick_interval_ms is None,
                  str(w.tick_interval_ms))
            check("hidden_chrome_really_hidden", hidden(w),
                  f"bars={w.chrome.bars_visible} "
                  f"state={w.chrome.state.visible}")
            check("hidden_gets_no_ticks", abs(gap) < 1.0
                  and w.tick_interval_ms is None,
                  f"state_clock_advanced={gap:.0f}ms interval="
                  f"{w.tick_interval_ms}")
            wake_control()

        GLib.timeout_add(1500, later)
        return False

    def wake_control():
        """THE ordering gate: the first motion after an idle stretch re-arms
        the fast cadence BEFORE any visual work happens."""
        w = win()
        seen: list = []
        orig = w._apply_reveal

        def spy(visible, *a, **kw):
            seen.append(w.tick_interval_ms)
            return orig(visible, *a, **kw)

        w._apply_reveal = spy
        w.chrome.motion(400, 300, 960, 540)
        armed = w.tick_interval_ms            # still inside the motion call

        def after():
            w._apply_reveal = orig
            first = seen[0] if seen else None
            print(f"R16_TD wake interval_inside_motion={armed} "
                  f"at_first_reveal_apply={first} "
                  f"bars_after={w.chrome.bars_visible}", flush=True)
            check("wake_rearm_before_visual_work",
                  armed == 10 and first == 10,
                  f"inside_motion={armed} at_first_apply={first}")
            check("wake_reveal_same_pass",
                  bool(w.chrome.bars_visible)
                  and bool(w.chrome.state.visible),
                  f"bars={w.chrome.bars_visible}")
            check("wake_fade_in_flight", bool(w.chrome.animating),
                  str(w.chrome.animating))
            w.chrome.state.timeout_ms = 600
            poll("chrome_rehides", 100, 10.0,
                 lambda: w.tick_interval_ms is None, suppress)

        def suppress():
            """Only AFTER the video is suppressed can the pump see 'no
            pictures for a while' and drop to its slow rung."""
            rc = w.core.set_property("vid", "no")
            print(f"R16_TD vid=no rc={rc}", flush=True)
            stranded_probe()

        def stranded_probe():
            """R16-A regression: an OSD/toast whose deadline has JUST passed
            is still pending tick work — ``tick()`` is what hides it, and it
            hides it only AFTER the deadline.  ``TickDriver._pump_cb`` calls
            ``_apply()`` without a ``tick_reveal()``, so a pump dispatch
            landing after the expiry used to remove the chrome source with
            the overlay still on screen, and nothing hid it again until
            pointer activity (measured: scripts/smoke_cropwin.py lost 4 of
            its 8 settle phases to a permanently visible
            "Crop to window: on").  Deterministic: the expired-but-unretired
            instant is created by hand instead of raced."""
            w = win()
            pre = w.tick_interval_ms
            w.chrome.osd_ms = 250.0
            w.chrome.show_osd("stranded probe")
            armed = w.tick_interval_ms
            # the exact instant the old code got wrong: deadline in the past,
            # widget still up, no chrome tick since — and the 400 ms
            # state-change hold cleared, because while it runs the ladder is
            # fast for an unrelated reason and the bug cannot show
            w.chrome._osd_deadline = w.chrome.state.clock() - 1.0
            w._driver._hold_until_ms = 0.0
            w._driver._pump_cb()              # the dispatch that used to strand it
            after = w.tick_interval_ms
            print(f"R16_TD osd probe pre={pre} armed={armed} "
                  f"after_pump_cb={after} osd={w.chrome.osd.get_visible()} "
                  f"bars={w.chrome.bars_visible} "
                  f"statevis={w.chrome.state.visible}", flush=True)
            check("expired_transient_keeps_chrome_armed",
                  armed == 10 and after is not None
                  and bool(w.chrome.osd.get_visible())
                  and not w.chrome.bars_visible
                  and not w.chrome.state.visible,
                  f"armed={armed} after_pump_cb={after} "
                  f"osd={w.chrome.osd.get_visible()} "
                  f"bars={w.chrome.bars_visible}")

            def retired():
                vis = bool(w.chrome.osd.get_visible())
                print(f"R16_TD osd probe retired osd={vis} "
                      f"chrome={w.tick_interval_ms}", flush=True)
                check("expired_transient_is_retired", not vis,
                      f"osd={vis} label={w.chrome.osd.get_label()!r} "
                      f"chrome={w.tick_interval_ms}")
                poll("pump_slow_without_frames", 100, 12.0,
                     lambda: (w.pump_interval_ms == 100
                              and w.tick_interval_ms is None), measure)

            GLib.timeout_add(1200, retired)
            return False

        GLib.timeout_add(150, after)
        return False

    # ---------------------------------------------------------------- part 2
    def measure():
        w = win()
        print(f"R16_TD ready pump={w.pump_interval_ms}ms "
              f"chrome={w.tick_interval_ms}", flush=True)
        poll("idle_hidden_state", 100, 10.0,
             lambda: (w.tick_interval_ms is None and w.pump_interval_ms == 100
                      and hidden(w)),
             lambda: phase("ladder", GATE_S,
                           lambda: phase("pinned", GATE_S, with_video)))

    def phase(kind, seconds, then):
        """One measurement arm: settle (let the chrome hide), then time."""
        if kind.startswith("pinned"):
            win()._driver.pin(10)             # the pre-R16 fixed cadence

        def start():
            w = win()
            arm = {"hidden_at_start": hidden(w),
                   "iv0": (w.tick_interval_ms, w.pump_interval_ms),
                   "u0": utime(), "w0": time.monotonic()}

            def done():
                arm["utime"] = utime() - arm["u0"]
                arm["wall"] = time.monotonic() - arm["w0"]
                arm["hidden_at_end"] = hidden(w)
                ARMS[kind] = arm
                print(f"R16_TD arm={kind} interval={arm['iv0'][0]} "
                      f"pump={arm['iv0'][1]} wall={arm['wall']:.2f}s "
                      f"utime={arm['utime']:.3f}s hidden="
                      f"{arm['hidden_at_start']}/{arm['hidden_at_end']}",
                      flush=True)
                win()._driver.pin(None)
                then()
                return False

            GLib.timeout_add(int(seconds * 1000), done)
            return False

        GLib.timeout_add(1500, start)         # settle window first
        return False

    def with_video():
        w = win()
        w.core.set_property("vid", "auto")
        poll("video_back_on", 100, 10.0,
             lambda: w.pump_interval_ms == 10,
             lambda: phase("ladder-video", INFO_S,
                           lambda: phase("pinned-video", INFO_S, verdicts)))

    def gate(a, b, na, nb, npct, floor):
        ua = a.get("utime", 0.0)
        ub = b.get("utime", 0.0)
        pct = (100.0 * (ub - ua) / ub) if ub > 0 else -100.0
        check(na, ua > 0, f"{ua:.3f}")
        check(nb, ub > 0, f"{ub:.3f}")
        check(npct, True if floor is None else pct >= floor,
              f"{pct:.1f}" if floor is None else f"{pct:.1f} (floor {floor})")
        print(f"R16_UTIME {na}={ua:.3f}s {nb}={ub:.3f}s drop={pct:.1f}%",
              flush=True)

    def verdicts():
        L, P = ARMS.get("ladder", {}), ARMS.get("pinned", {})
        check("idle_hidden_both_arms_hidden",
              all(x.get("hidden_at_start") and x.get("hidden_at_end")
                  for x in (L, P)),
              f"ladder={L.get('hidden_at_start')}/{L.get('hidden_at_end')} "
              f"pinned={P.get('hidden_at_start')}/{P.get('hidden_at_end')}")
        check("idle_hidden_arms_differ",
              L.get("iv0", (None,))[0] is None
              and P.get("iv0", (None, None))[0] == 10,
              f"ladder={L.get('iv0')} pinned={P.get('iv0')}")
        gate(L, P, "utime_ladder_idlehidden", "utime_pinned10_idlehidden",
             "utime_drop_ge_25pct", 25.0)
        gate(ARMS.get("ladder-video", {}), ARMS.get("pinned-video", {}),
             "utime_ladder_playing_hidden", "utime_pinned10_playing_hidden",
             "utime_drop_playing_reported", None)
        app.quit()

    def wait_playing(tries=0):
        if playing(win()):
            check("file_started_playing", True, "pause=False idle=False")
            check_motion()
            return False
        if tries >= 60:
            check("file_started_playing", False, repr(win().core.snapshot))
            app.quit()
            return False
        GLib.timeout_add(100, wait_playing, tries + 1)
        return False

    GLib.timeout_add(800, wait_playing)
    return False


def main() -> int:
    rc = reelsee.run(["reelsee", "testdata/perf_1080p30.mp4"],
                     on_app=probe_schedule)
    check("app_rc", rc == 0, str(rc))
    finish()
    return 0


if __name__ == "__main__":
    sys.exit(main())
