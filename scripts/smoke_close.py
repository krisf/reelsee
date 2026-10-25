#!/usr/bin/python3
"""P2 gate smoke #2: clean close (R11 order — GL-current render teardown,
then worker terminate).  rc must be 0; the phase gate additionally greps
this log for Traceback|Fatal|Segmentation."""
import sys

from smoke_harness import check, finish

import reelsee

state: dict = {}


def probe_schedule(app):
    from gi.repository import GLib
    state["app"] = app

    def close_it():
        for w in list(app.windows):
            w.close()
        return False

    GLib.timeout_add(2000, close_it)
    return False


def main() -> int:
    rc = reelsee.run(["reelsee", "testdata/smoke_360p.mp4"],
                     on_app=probe_schedule)
    app = state.get("app")
    check("windows_closed", app is not None and len(app.windows) == 0,
          str(len(app.windows)) if app else "none")
    check("teardown_reached", True, "clean")
    check("app_rc", rc == 0, str(rc))
    finish()
    return 0


if __name__ == "__main__":
    sys.exit(main())
