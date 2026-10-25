#!/usr/bin/python3
"""R2-P4(I4): watchdog REAL-ACTION gate (the sim smoke never touched mpv).

smoke_watchdog.py proves the SIMULATED chain.  Here the demotion runs
through the live core: the sampler is scripted to report a sustained
overload, and the window's untouched reaction path
(watchdog.feed → apply_action → core.apply_hwdec) must actually issue
the runtime hwdec switch on the running mpv.

Assertions that hold REGARDLESS of GPU engagement (this machine may lack
/dev/dri — PLAN R13): the recorded apply call carries the chain's next
step; the real mpv command still executes underneath (apply is wrapped,
not replaced); playback position stays continuous across the switch
(§0.7 PROOF-A: pos delta ≈ wall delta, no restart); the shared
FallbackChain advanced; the toast and config/engines.json records name
the switch.  When /dev/dri exists (operator box) the observed
hwdec-current must additionally be honest (engaged set or 'no').
"""
import json
import os
import sys
import time

from smoke_harness import SCRATCH, check, finish

CFG_DIR = os.path.join(SCRATCH, "wdreal-cfg")
STATE_DIR = os.path.join(SCRATCH, "wdreal-state")
for _d in (CFG_DIR, STATE_DIR):
    os.makedirs(_d, exist_ok=True)
os.environ["REELSEE_CONFIG_DIR"] = CFG_DIR
os.environ["REELSEE_STATE_DIR"] = STATE_DIR
for _d in (CFG_DIR, STATE_DIR):
    for _f in ("config.json", "resume.json", "ab.json"):
        _p = os.path.join(_d, _f)
        if os.path.exists(_p):
            os.unlink(_p)

from gi.repository import GLib  # noqa: E402

import reelsee  # noqa: E402

state: dict = {}


def win():
    return state["app"].windows[0]


def main() -> int:
    # 10 s clip: demote lands ≈ t+8 s, continuity check ≈ t+9.6 s — inside
    # one un-wrapped pass (loop-file would break the position math).
    rc = reelsee.run(["reelsee", "testdata/perf_1080p30.mp4"],
                     on_app=probe_schedule)
    check("app_rc", rc == 0, str(rc))
    finish()
    return 0 if rc == 0 else 1


def probe_schedule(app):
    state["app"] = app

    def guard(fn):
        def run(*a):
            try:
                return fn(*a)
            except Exception as exc:               # noqa: BLE001
                import traceback
                traceback.print_exc()
                check(fn.__name__ + "_crashed", False, repr(exc))
                app.quit()
                return False
        run.__name__ = fn.__name__
        return run

    def wait_for(desc, pred, then, left=30):
        try:
            val = pred()
        except Exception:                         # noqa: BLE001
            val = None
        if val:
            check(desc, True, repr(val) if val is not True else "ok")
            then()
            return False
        if left <= 0:
            check(desc, False, "timeout")
            app.quit()
            return False
        GLib.timeout_add(100, wait_for, desc, pred, then, left - 1)
        return False

    def loaded():
        return bool(win().core.snapshot.get("duration")) or None

    def t0():
        GLib.timeout_add(300, guard(t1))
        return False

    def t1():
        import watchdog as WD
        w = win()
        # live wiring: a GPU engine is CONFIGURED (this bypasses the
        # headless auto-cpu wiring, exactly as a real GPU session would
        # arm it through _arm_engine_watch)
        chain = WD.FallbackChain("vaapi")
        w.watchdog.set_engine(chain=chain, is_cpu=False)
        state["chain"] = chain
        state["applied"] = []
        state["toasts"] = []
        orig_apply = w.core.apply_hwdec
        orig_toast = w.chrome.show_toast

        def rec_apply(mode):
            snap = w.core.snapshot
            state["applied"].append({
                "mode": mode,
                "t": time.monotonic(),
                "pos": float(snap.get("time-pos") or 0.0)})
            return orig_apply(mode)          # REAL mpv command executes

        def rec_toast(text, *a):
            state["toasts"].append(text)
            return orig_toast(text, *a)
        w.core.apply_hwdec = rec_apply
        w.chrome.show_toast = rec_toast

        # script the sampler: healthy baseline first (watchdog grace
        # 4 s), then sustained overload — vf 10/30 with heavy drops
        seq = {"n": 0}

        def scripted():
            seq["n"] += 1
            hot = seq["n"] > 1
            return WD.Sample(
                t=time.monotonic(), playing=True, pause=False, seeking=False,
                idle=False, eof=False,
                vf_fps=10.0 if hot else 30.0, container_fps=30.0,
                drop=30 * seq["n"] if hot else 0, dec_drop=0,
                hwdec="vaapi")
        w._wd_sampler.sample = scripted
        wait_for("real_demote_applied",
                 lambda: state["applied"] or None, guard(t2), left=90)
        return False

    def t2():
        import watchdog as WD
        w = win()
        acts = state["applied"]
        check("apply_called_with_chain_step",
              bool(acts) and acts[0]["mode"] == "vaapi-copy",
              repr([a["mode"] for a in acts]))
        check("chain_advanced",
              state["chain"].current == "vaapi-copy"
              and state["chain"].fell_back
              and state["chain"].fell_from == "vaapi",
              f"current={state['chain'].current}")
        check("toast_names_switch",
              any("decoder switched" in t and "vaapi-copy" in t
                  for t in state["toasts"]),
              "|".join(state["toasts"])[:120] or "NO TOAST")
        cfgfb = w.cfg.get("engine.last_fallback")
        check("config_fallback_record",
              isinstance(cfgfb, dict)
              and cfgfb.get("to") == "vaapi-copy"
              and cfgfb.get("from") == "vaapi",
              repr(cfgfb)[:120])
        # engines.json merge record (R2-P2 path must fire on real action)
        ejson = os.path.join(STATE_DIR, "engines.json")
        fb = None
        try:
            with open(ejson, encoding="utf-8") as fh:
                fb = json.load(fh).get("last_fallback")
        except (OSError, ValueError):
            pass
        check("engines_json_record",
              isinstance(fb, dict) and fb.get("to") == "vaapi-copy",
              repr(fb)[:120])
        # position continuity across the live switch (§0.7 PROOF-A)
        GLib.timeout_add(1600, guard(t3))
        return False

    def t3():
        w = win()
        a = state["applied"][-1]
        snap = w.core.snapshot
        pos = float(snap.get("time-pos") or 0.0)
        dp = pos - a["pos"]
        dw = time.monotonic() - a["t"]
        # playing: pos must have advanced WITH wall time (no restart,
        # no stall beyond the vid no→auto reattach budget)
        check("position_preserved", dp > 0 and abs(dp - dw) < 0.75,
              f"dp={dp:.2f}s dw={dw:.2f}s")
        if os.path.isdir("/dev/dri"):
            import watchdog as WD
            obs = str(snap.get("hwdec-current") or "").split("(")[0]
            check("engaged_mode_honest",
                  obs in WD.ENGAGED_MODES or obs == "no", obs)
        check("window_alive_after_real_demote",
              w in state["app"].windows, "ok")
        state["app"].quit()
        return False

    wait_for("first_file_loaded", loaded, guard(t0), left=60)
    return False


if __name__ == "__main__":
    sys.exit(main())
