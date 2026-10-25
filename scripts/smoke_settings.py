#!/usr/bin/python3
"""P4 gate smoke: settings window opens, binds every key, live-applies,
closes cleanly, and persists.  Runs under scripts/headless-smoke.sh."""
import json
import os
import sys

from smoke_harness import SCRATCH, check, finish

# config/state must NOT touch the real home dirs from a smoke run
CFG_DIR = os.path.join(SCRATCH, "settings-cfg")
STATE_DIR = os.path.join(SCRATCH, "settings-state")
for _d in (CFG_DIR, STATE_DIR):
    os.makedirs(_d, exist_ok=True)
os.environ["REELSEE_CONFIG_DIR"] = CFG_DIR
os.environ["REELSEE_STATE_DIR"] = STATE_DIR
for _f in (os.path.join(CFG_DIR, "config.json"),
           os.path.join(CFG_DIR, "resume.json")):
    if os.path.exists(_f):
        os.unlink(_f)

from gi.repository import GLib  # noqa: E402

import reelsee  # noqa: E402

state: dict = {}


def disk_cfg():
    with open(os.path.join(CFG_DIR, "config.json")) as fh:
        return json.load(fh)


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

    def wait_for(desc, pred, then, left=20):
        val = None
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

    def s0():
        st = app.show_settings()
        state["st"] = st
        n_pages = len(st.pages)
        check("settings_opens", n_pages == 7, f"pages={n_pages}")
        import config as C
        missing = [k for k in C.schema_leaf_keys() if k not in st.widgets]
        check("every_key_bound", not missing,
              "missing=" + ",".join(missing) if missing else "all 35 keys")
        GLib.timeout_add(200, guard(s1))
        return False

    def s1():
        # toggling a switch must hit config AND the live core immediately
        state["st"].widgets["audio.muted"].set_active(False)
        w = app.windows[0]
        wait_for("live_mute", lambda: (
            disk_cfg()["audio"]["muted"] is False
            and w.core.snapshot.get("mute") in (False, "no")),
            guard(s2))
        return False

    def s2():
        state["st"].widgets["audio.volume"].set_value(123.0)
        w = app.windows[0]
        wait_for("live_volume", lambda: float(
            w.core.snapshot.get("volume") or 0) == 123.0, guard(s3))
        return False

    def s3():
        st = state["st"]
        st.close()
        GLib.timeout_add(250, guard(s4))
        return False

    def s4():
        check("settings_closes", app._settings is None, repr(app._settings))
        reopen = app.show_settings()
        check("settings_reopens", reopen is not None
              and reopen is not state["st"], "ok")
        reopen.close()
        GLib.timeout_add(250, guard(s5))
        return False

    def s5():
        app.quit()
        return False

    GLib.timeout_add(900, guard(s0))
    return False


def main() -> int:
    rc = reelsee.run(["reelsee", "testdata/smoke_360p.mp4"],
                     on_app=probe_schedule)
    check("app_rc", rc == 0, str(rc))
    # on-disk proof (mute/volume are R-4 immediate-persist keys)
    try:
        data = disk_cfg()
        ok = (data["audio"]["muted"] is False
              and float(data["audio"]["volume"]) == 123.0)
        check("persist_after_quit", ok,
              f"muted={data['audio']['muted']} vol={data['audio']['volume']}")
    except Exception as exc:                       # noqa: BLE001
        check("persist_after_quit", False, repr(exc))
    finish()
    return 0


if __name__ == "__main__":
    sys.exit(main())
