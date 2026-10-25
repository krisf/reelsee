#!/usr/bin/python3
"""P7 gate smoke: live app under --simulate-overload.

The flag makes the watchdog's Sampler yield synthetic overloaded samples on
a simulated vaapi engine (a headless box can't produce a real GPU overload).
Proves end-to-end: after the 4 s grace + 3-sample confirm the watchdog
demotes vaapi → vaapi-copy ON THE LIVE HANDLE, toasts, and persists
engine.last_fallback exactly as Appendix C specifies.
"""
import json
import os
import sys

from smoke_harness import SCRATCH, check, finish

CFG_DIR = os.path.join(SCRATCH, "wd-cfg")
STATE_DIR = os.path.join(SCRATCH, "wd-state")
for _d in (CFG_DIR, STATE_DIR):
    os.makedirs(_d, exist_ok=True)
os.environ["REELSEE_CONFIG_DIR"] = CFG_DIR
os.environ["REELSEE_STATE_DIR"] = STATE_DIR
for _f in ("config.json", "ab.json"):
    _p = os.path.join(CFG_DIR if _f == "config.json" else STATE_DIR, _f)
    if os.path.exists(_p):
        os.unlink(_p)

from gi.repository import GLib  # noqa: E402

import reelsee  # noqa: E402

CLIP = "testdata/perf_1080p30.mp4"      # 10 s: fire at ~7-8 s, quit before EOF


def probe_schedule(app):
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
        val = None
        try:
            val = pred()
        except Exception:                         # noqa: BLE001
            val = None
        if val:
            check(desc, True, repr(val) if val is not True else "ok")
            then(val)
            return False
        if left <= 0:
            check(desc, False, "timeout")
            app.quit()
            return False
        GLib.timeout_add(100, wait_for, desc, pred, then, left - 1)
        return False

    def s0():
        w = app.windows[0]
        check("wd_sim_mode", getattr(w._wd_sampler, "simulate", False),
              "simulated")
        check("wd_enabled", w.watchdog.enabled, "yes")

        def fired():
            act = w.watchdog.last_action
            return act if act and act.get("kind") == "demote" else None

        # grace 4 s + 3×1 s samples ≈ 7 s; allow generous 25 s
        wait_for("watchdog_action", fired, guard(s1), left=250)
        return False

    def s1(act):
        check("watchdog_action", act["kind"] == "demote", act["kind"])
        check("watchdog_chain_step", act["from"] == "vaapi"
              and act["to"] == "vaapi-copy",
              f"{act['from']}->{act['to']}")
        w = app.windows[0]
        check("watchdog_toast", w.chrome.toast.get_visible()
              and "vaapi-copy" in w.chrome.toast.get_label(),
              w.chrome.toast.get_label())
        wait_for("fallback_persisted",
                 lambda: _fallback_from_cfg() is not None,
                 guard(s2), left=40)
        return False

    def s2(_cfg_fb):
        fb = _fallback_from_cfg()
        ok = (isinstance(fb, dict) and fb.get("from") == "vaapi"
              and fb.get("to") == "vaapi-copy"
              and isinstance(fb.get("ts"), int)
              and "fps_ratio" in fb and "drop_ratio" in fb)
        check("watchdog_config_changed", ok, json.dumps(fb))
        GLib.timeout_add(300, guard(s3))
        return False

    def s3():
        app.quit()
        return False

    GLib.timeout_add(600, guard(s0))
    return False


def _fallback_from_cfg():
    try:
        with open(os.path.join(CFG_DIR, "config.json")) as fh:
            return json.load(fh).get("engine", {}).get("last_fallback")
    except (OSError, ValueError):
        return None


def main() -> int:
    rc = reelsee.run(["reelsee", "--simulate-overload", CLIP],
                     on_app=probe_schedule)
    check("app_rc", rc == 0, str(rc))
    finish()
    return 0


if __name__ == "__main__":
    sys.exit(main())
