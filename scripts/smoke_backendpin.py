#!/usr/bin/env python3
"""R24 §B.4 gate: the backend pin is applied in the REAL boot path.

The unit table (tests/test_backend_pin.py) proves the arithmetic; this proves
the thing that actually shipped broken — that ``reelsee.run()`` pins
``GDK_BACKEND`` itself, before GDK opens a display, on a launch where nothing
else pinned it.  That is the gap the packaged ``.desktop`` entry left: a
user-level shadowing entry (or a bare ``/usr/bin/reelsee`` from a terminal)
produced a native Wayland client, where positioning is impossible by
protocol and Mutter centres the window.

Two phases, each its own process (a display backend is fixed for the life of
a process, so one pin per interpreter is the only honest test):

  pin      — an XWayland session with no ``GDK_BACKEND`` set: ``run()`` must
             set it to ``x11`` and mark the process, BEFORE any window opens,
             and GDK must really end up on an X11 display.
  control  — the same session with the X socket seam reporting MISSING: the
             app must NOT pin (fail-closed), must say why, and must still
             boot.  Without this phase the pin assertion is only interesting
             if you also know the app does not pin unconditionally.

Deliberate deviation from the brief's literal ``DISPLAY=:0``: the gate keeps
the harness's own Xvfb ``DISPLAY`` (a real ``:N`` with a real socket, which
is all the decision needs) instead of pointing GDK at the operator's live
``:0`` — that would put a video window on their desktop.  ``WAYLAND_DISPLAY``
is simulated, and ``XDG_RUNTIME_DIR`` is pointed at an EMPTY scratch dir so a
declined pin cannot reach the live compositor either.
"""
import logging
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))
from smoke_harness import SCRATCH, check, finish  # noqa: E402

PHASE = os.environ.get("REELSEE_R24BP_PHASE", "parent")
STATE_DIR = Path(os.environ.get("REELSEE_R24BP_STATE_DIR")
                 or SCRATCH / f"backendpin-state-{os.getpid()}")
CFG_DIR = Path(os.environ.get("REELSEE_R24BP_CFG_DIR")
               or SCRATCH / f"backendpin-cfg-{os.getpid()}")
EMPTY_XRD = SCRATCH.parent / "empty-xrd"      # no wayland socket lives here

import backendpin  # noqa: E402


class _Collect(logging.Handler):
    def __init__(self, sink):
        super().__init__()
        self.sink = sink

    def emit(self, record):
        self.sink.append(record)


def setup_env():
    """The unpinned XWayland session the bug was measured on."""
    for d in (CFG_DIR, STATE_DIR, EMPTY_XRD):
        d.mkdir(parents=True, exist_ok=True)
    os.environ["REELSEE_CONFIG_DIR"] = str(CFG_DIR)
    os.environ["REELSEE_STATE_DIR"] = str(STATE_DIR)
    os.environ.pop(backendpin.ENV_BACKEND, None)      # the user's launch path
    os.environ.pop(backendpin.ENV_PIN, None)
    os.environ.pop(backendpin.ENV_PINNED, None)
    os.environ["WAYLAND_DISPLAY"] = "wayland-0"
    os.environ["XDG_RUNTIME_DIR"] = str(EMPTY_XRD)
    check("env_display_present", bool(os.environ.get("DISPLAY")),
          str(os.environ.get("DISPLAY")))


def on_app(app, phase, logs):
    """Runs after the app is registered and BEFORE app.run() — so before any
    window exists.  Everything here is about the environment side-effect."""
    from gi.repository import Gdk

    got = os.environ.get(backendpin.ENV_BACKEND)
    marker = os.environ.get(backendpin.ENV_PINNED)
    check("no_window_yet", not getattr(app, "windows", []),
          str(getattr(app, "windows", None)))
    if phase == "pin":
        check("gdk_backend_pinned", got == backendpin.PIN_VALUE, str(got))
        check("marker_set", marker == backendpin.PIN_VALUE, str(marker))
        lines = [r for r in logs if r.levelno >= logging.INFO]
        check("one_log_line", len(lines) == 1, str([r.getMessage()
                                                    for r in lines]))
        check("reason_is_xwayland",
              bool(lines) and "reason=xwayland" in lines[0].getMessage(),
              str([r.getMessage() for r in lines]))
        check("log_is_info", bool(lines)
              and lines[0].levelno == logging.INFO,
              str([r.levelname for r in lines]))
    else:
        check("control_not_pinned", got is None, str(got))
        check("control_marker_absent", marker is None, str(marker))
        lines = [r for r in logs if r.levelno >= logging.INFO]
        check("control_one_log_line", len(lines) == 1,
              str([r.getMessage() for r in lines]))
        check("control_reason_is_no_socket",
              bool(lines)
              and "reason=no-xwayland-socket" in lines[0].getMessage(),
              str([r.getMessage() for r in lines]))

    # The proof that the pin is not just an environment write: the display
    # GDK actually opened.  Both phases end on X11 — the pin because we asked,
    # the control because GDK could not reach a compositor from an empty
    # XDG_RUNTIME_DIR.  What differs is whether the APP decided it.
    disp = Gdk.Display.get_default()
    check(f"{phase}_display_is_x11",
          disp is not None and "X11" in type(disp).__name__,
          type(disp).__name__ if disp else "None")
    # Quit from INSIDE app.run(): Gtk.Application.quit() called before
    # g_application_run trips a GLib-CRITICAL and makes run() return 1,
    # which would be the only thing this gate measured.
    from gi.repository import GLib
    GLib.timeout_add(150, app.quit)
    return False


def run_phase(phase):
    import reelsee
    setup_env()
    logs: list = []
    lg = logging.getLogger("reelsee.backendpin")
    lg.setLevel(logging.DEBUG)
    lg.addHandler(_Collect(logs))
    # The socket seam, stubbed exactly as the brief asks: this gate must not
    # depend on whether the machine happens to have an XWayland socket.
    backendpin._socket_exists = (lambda display: True) if phase == "pin" \
        else (lambda display: False)
    rc = reelsee.run(["reelsee"], on_app=lambda a: on_app(a, phase, logs))
    check(f"phase_{phase}_rc", rc == 0, str(rc))
    # idempotence in the real process: a second call sees what the first set
    if phase == "pin":
        before = os.environ.get(backendpin.ENV_BACKEND)
        again = backendpin.apply_pin(pref="auto")
        check("idempotent_value", again is None, str(again))
        check("idempotent_env",
              os.environ.get(backendpin.ENV_BACKEND) == before,
              str(os.environ.get(backendpin.ENV_BACKEND)))
    return rc


def main() -> int:
    if PHASE in ("pin", "control"):
        rc = run_phase(PHASE)
        finish()
        return 0 if rc == 0 else 1

    ok = True
    for phase in ("pin", "control"):
        env = dict(os.environ, REELSEE_R24BP_PHASE=phase,
                   REELSEE_R24BP_STATE_DIR=str(STATE_DIR) + f"-{phase}",
                   REELSEE_R24BP_CFG_DIR=str(CFG_DIR) + f"-{phase}")
        out = subprocess.run([sys.executable, os.path.abspath(__file__)],
                             env=env, capture_output=True, text=True,
                             timeout=60)
        sys.stdout.write(out.stdout)
        sys.stderr.write(out.stderr[-4000:])
        check(f"child_{phase}_rc", out.returncode == 0, str(out.returncode))
        check(f"child_{phase}_asserts",
              "SMOKE_ASSERTS_PASSED" in out.stdout, "marker")
        ok = ok and out.returncode == 0
    finish()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
