#!/usr/bin/env python3
"""R10 gate smoke: remembered geometry + cascade placement, real windows.

Phase 1 (process A): open, resize+MOVE to a known rect (real Xlib
configure — the same seam the app uses), quit ⇒ window.json holds that
exact rect (§A.1 flush-on-quit: the 250 ms debounce had a chance here,
the capture does not depend on it).

Phase 2 (process B, fresh process, same state dir): the relaunch
restores size AND position within ±2 px (§A.3); a SECOND window opens
at the source's EXACT size moved 15 px TOWARD THE SCREEN CENTRE on BOTH
axes (R20 §0.2 — the user replaced the old right-then-left flip; ±2 px)
and overlapping; move it to the right edge and the THIRD window steps
back toward the centre (chained, still centerward); sizes stay identical
all the way.

Phase 3 (process C, fresh state dir, no-xid simulation): with the
surface deliberately non-X11 (a stand-in for a Wayland toplevel), the
whole open path still completes at the correct cascade SIZE, applies
position as a no-op with exactly ONE debug log, no exception, no retry
storm (§B.3 — "degrade, never fail").

Phase 4 (process D, fresh state dir, a simulated 60 px bottom PANEL):
the R20 §0.1 corner rule over real windows.  A rect closed with its
bottom corners UNDER THE PANEL (2 corners off the work area, 0 off the
screen) reopens at the EXACT coords+dims — no clamp; a rect with >=2
corners off the SCREEN still repositions via clamp_into_workarea.
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))
from smoke_harness import SCRATCH, check, finish  # noqa: E402

STATE_DIR = Path(os.environ.get("REELSEE_R10WG_STATE_DIR")
                 or SCRATCH / f"wingeom-state-{os.getpid()}")
CFG_DIR = Path(os.environ.get("REELSEE_R10WG_CFG_DIR")
               or SCRATCH / f"wingeom-cfg-{os.getpid()}")
for _d in (CFG_DIR, STATE_DIR):
    _d.mkdir(parents=True, exist_ok=True)
os.environ["REELSEE_CONFIG_DIR"] = str(CFG_DIR)
os.environ["REELSEE_STATE_DIR"] = str(STATE_DIR)

PHASE = os.environ.get("REELSEE_R10WG_PHASE", "1")
GEOM_FILE = STATE_DIR / "window.json"
# R20 §0.2: the cascade step is 15 px TOWARD THE SCREEN CENTRE now (the
# user replaced the old 50 px right-then-left flip).  config default too.
OFFSET = 15
T1 = (140, 90, 760, 430)          # phase-1 target rect (x, y, w, h)
TOL = 2
PANEL_H = 60                     # phase 4: simulated bottom panel height
# closed with BOTH bottom corners under the panel: 2 corners off the WORK
# area, 0 off the SCREEN ⇒ must reopen verbatim (R20 §0.1).  Its bottom
# edge (975) is inside the 1000 px screen but below the 940 px work area.
PANEL_RECT = (140, 545, 760, 430)
# 2 corners off the SCREEN (right edge 2260 > 1600) ⇒ reposition as before
OFFSCREEN_RECT = (1500, 100, 760, 430)

import reelsee  # noqa: E402


def base_schedule(app, phase, shared=None):
    from gi.repository import GLib
    state = shared if shared is not None else {}
    state["app"] = app

    def win(i=0):
        return app.windows[i]

    def guard(fn):
        def run(*a):
            try:
                return fn(*a)
            except Exception as exc:                # noqa: BLE001
                import traceback
                traceback.print_exc()
                check(fn.__name__ + "_crashed", False, repr(exc))
                app.quit()
                return False
        run.__name__ = fn.__name__
        return run

    def again(fn, ms):
        GLib.timeout_add(ms, fn)
        return False

    def wait_for(desc, pred, then, left=40, see=None):
        try:
            val = pred()
        except Exception:                           # noqa: BLE001
            val = None
        if val:
            check(desc, True, "ok" if val is True else str(val))
            return again(then, 80)
        if left <= 0:
            try:
                last = "timeout" if see is None else repr(see())
            except Exception as exc2:               # noqa: BLE001
                last = f"probe:{exc2!r}"
            check(desc, False, last)
            app.quit()
            return False
        return again(lambda: wait_for(desc, pred, then, left - 1, see), 120)

    import x11pos
    import wingeom

    def rect(i=0):
        return x11pos.get_rect(win(i))

    def near(got, want, tol=TOL):
        return (got is not None
                and all(abs(a - b) <= tol for a, b in zip(got, want)))

    def wa():
        if "wa" not in state:
            areas = wingeom.work_areas(
                win().get_display().get_monitors())
            state["wa"] = areas[0] if areas else (0, 0, 1600, 1000)
        return state["wa"]

    def right_edge_rect():
        wx, _wy, ww, _wh = wa()
        return (wx + ww - T1[2], T1[1], T1[2], T1[3])

    # ------------------------------------------------------------ phase 1
    def p1_moved():
        # the "user" resized AND moved: one real Xlib configure to T1
        # (size+position), exactly what the app does at open.
        w = win()
        w.set_default_size(T1[2], T1[3])
        ok = x11pos.set_rect(w, T1)
        check("p1_configure_sent", bool(ok), str(ok))
        return wait_for("p1_at_target", lambda: near(rect(), T1),
                        guard(p1_settled))

    def p1_settled():
        # let the 1 s sampler run over the moved window (its value is the
        # fallback source for a crash — the flush below is the promise)
        return again(guard(p1_quit), 1300)

    def p1_quit():
        check("p1_tracked", win()._geom_rect is not None,
              str(win()._geom_rect))
        app.quit()
        return False

    # ------------------------------------------------------------ phase 2
    def p2_restored():
        # fresh process: the ONLY window opened at the remembered rect —
        # size AND position, within ±2 px (§A.3)
        return wait_for("p2_restores_rect", lambda: near(rect(), T1),
                        guard(p2_spawn2), see=rect)

    def p2_spawn2():
        r0 = rect()                             # Xlib truth — the size the
        check("p2_size_exact", r0 is not None       # whole feature reads
              and all(abs(a - b) <= TOL for a, b in
                      zip(r0[2:], T1[2:])), str(r0))
        # R21: the restore is not "send one move and hope" — the settle loop
        # re-asserts the post-map move until the window is OBSERVED at the
        # target.  Under a WM-less Xvfb the first move lands, so it must
        # report a real outcome, a bounded number of moves, and leave
        # nothing armed before the next window opens.
        w0 = win()
        check("p2_settle_reason",
              getattr(w0, "_geom_settle_reason", None) == "settled",
              str(getattr(w0, "_geom_settle_reason", None)))
        check("p2_settle_bounded",
              1 <= getattr(w0, "_geom_settle_moves", 0) <= 8,
              str(getattr(w0, "_geom_settle_moves", None)))
        check("p2_settle_over",
              getattr(w0, "_geom_settle", None) is None,
              "the loop finished before the next window opened")
        app._spawn_window()
        # R20 §0.2: 15 px toward the work-area centre on BOTH axes (T1 sits
        # left of and above the centre, so both deltas are +).
        want2 = (T1[0] + OFFSET, T1[1] + OFFSET, T1[2], T1[3])
        return wait_for("p2_cascade_pos", lambda: near(rect(1), want2),
                        guard(p2_cascade_checks), see=lambda: rect(1))

    def p2_cascade_checks():
        r0, r1 = rect(), rect(1)
        check("p2_two_same_size", r0 is not None and r1 is not None
              and r0[2:] == r1[2:], f"{r0} {r1}")
        dx, dy = r1[0] - r0[0], r1[1] - r0[1]
        check("p2_offset_exact", abs(abs(dx) - OFFSET) <= TOL, f"dx={dx}")
        check("p2_centerward_y", abs(abs(dy) - OFFSET) <= TOL, f"dy={dy}")
        # the step goes TOWARD the centre, never away from it (§0.2)
        check("p2_centerward_sign", dx > 0 and dy > 0, f"dx={dx} dy={dy}")
        check("p2_overlap", wingeom.overlaps(r0, r1), f"{r0} {r1}")
        # chain + centre: move the NEWEST window to the right edge of the
        # work area, the next one steps BACK toward the centre (§0.2)
        x11pos.set_rect(win(1), right_edge_rect())
        return wait_for("p2_at_right_edge",
                        lambda: near(rect(1), right_edge_rect()),
                        guard(p2_spawn3), see=lambda: rect(1))

    def p2_spawn3():
        app._spawn_window()
        wx, _wy, ww, _wh = wa()
        # source flush against the right edge ⇒ x steps LEFT toward the
        # centre; its top is still above the centre ⇒ y keeps stepping down
        want3 = (wx + ww - T1[2] - OFFSET, T1[1] + OFFSET, T1[2], T1[3])
        return wait_for("p2_flips_left", lambda: near(rect(2), want3),
                        guard(p2_final_checks), see=lambda: rect(2))

    def p2_final_checks():
        r1, r2 = rect(1), rect(2)
        check("p2_flip_same_size", r1 is not None and r2 is not None
              and r1[2:] == r2[2:], f"{r1} {r2}")
        check("p2_flip_overlap", wingeom.overlaps(r1, r2), f"{r1} {r2}")
        dx = r2[0] - r1[0]
        check("p2_flip_offset", abs(abs(dx) - OFFSET) <= TOL, f"dx={dx}")
        check("p2_flip_centerward_sign", dx < 0, f"dx={dx}")
        check("p2_no_aspect_refights",
              all(w._aspect_corrections == 0 for w in app.windows),
              str([w._aspect_corrections for w in app.windows]))
        app.quit()
        return False

    # ------------------------------------------------------------ phase 3
    def p3_noxid():
        # surface is deliberately non-X11 (Wayland stand-in): the sampler
        # must fall back to the size-only truth.  Self-consistency against
        # GDK's own reported size is the invariant — under WM-less Xvfb GTK
        # draws CSD shadows so get_width() sits a few px under the X size;
        # our job is to MIRROR whatever GDK reports, never a literal.
        w = win()
        w.set_default_size(800, 450)

        def settled():
            r = w._geom_rect
            return (r is not None and w.get_width() > 1
                    and r[2:] == (w.get_width(), w.get_height()))
        return wait_for("p3_sampler_size_only", settled, guard(p3_spawn2),
                        see=lambda: (w._geom_rect, w.get_width(),
                                     w.get_height()))

    def p3_spawn2():
        w0, w1 = win(), app._spawn_window()  # cascade plan + degraded place
        check("p3_open_completed", w1 is not None and not w1._closing,
              "ok")
        # the size the plan CARRIED is the source's, byte-exact (§B.3: the
        # size half of the promise survives even with no way to position)
        check("p3_adopt_size_verbatim",
              w1._geom_rect is not None
              and w1._geom_rect[2:] == w0._geom_rect[2:],
              f"src={w0._geom_rect} new={w1._geom_rect}")
        return again(guard(p3_after_guard), 400)

    def p3_after_guard():
        w1 = win(1)
        # GDK really did resize it (exact px is GDK's CSD bookkeeping —
        # p3_adopt_size_verbatim holds the line on the app's promise)
        check("p3_size_applied", w1.get_width() > 1 and w1.get_height() > 1,
              str((w1.get_width(), w1.get_height())))
        check("p3_position_noop", w1._geom_open is None,
              "placement consumed (degraded)")
        logs = state.get("logs") or []
        check("p3_one_debug_log", len(logs) == 1, f"{len(logs)}: {logs}")
        app.quit()
        return False

    # ------------------------------------------------------------ phase 4
    def screen_geos():
        """The FULL monitor geometries — the SCREEN, panel included (the
        reference the corner rule counts against, R20 §0.1)."""
        return wingeom.work_areas(
            [m.get_geometry() for m in win().get_display().get_monitors()])

    def p4_verbatim():
        # the ONLY window, restored from a rect whose bottom corners sit
        # UNDER THE PANEL: the exact coords+dims, no clamp (§0.1)
        return wait_for("p4_verbatim_restore", lambda: near(rect(), PANEL_RECT),
                        guard(p4_controls), see=rect)

    def p4_controls():
        # non-vacuity: the rect really does hang off the WORK area (that is
        # the bug being fixed) while every corner stays ON THE SCREEN
        check("p4_hangs_off_workarea",
              wingeom.corners_offscreen(PANEL_RECT, wa()) >= 2,
              f"{wingeom.corners_offscreen(PANEL_RECT, wa())} of {wa()}")
        check("p4_corners_on_screen",
              wingeom.corners_offscreen(PANEL_RECT, screen_geos()) == 0,
              str(screen_geos()))
        check("p4_old_rule_would_move_it",
              wingeom.clamp_into_workarea(PANEL_RECT, wa()) != PANEL_RECT,
              str(wingeom.clamp_into_workarea(PANEL_RECT, wa())))
        check("p4_plan_is_verbatim",
              win().plan_open_geometry(None) == ("restore", PANEL_RECT),
              str(win().plan_open_geometry(None)))
        app.geometry_store.remember(OFFSCREEN_RECT, 0)
        return guard(p4_reposition)()

    def p4_reposition():
        # >=2 corners off the SCREEN ⇒ the reposition half of the rule still
        # runs, and it clamps into the WORK area at the exact size
        plan = win().plan_open_geometry(None)
        want = wingeom.clamp_into_workarea(OFFSCREEN_RECT, wa())
        check("p4_two_corners_off_screen",
              wingeom.corners_offscreen(OFFSCREEN_RECT, screen_geos()) >= 2,
              str(wingeom.corners_offscreen(OFFSCREEN_RECT, screen_geos())))
        check("p4_repositions", plan == ("restore", want),
              f"{plan} want={want}")
        got = plan[1] if plan else None
        check("p4_reposition_keeps_size",
              got is not None and got[2:] == OFFSCREEN_RECT[2:], str(got))
        wx, wy, ww, wh = wa()
        check("p4_reposition_onscreen",
              got is not None and got[0] >= wx and got[1] >= wy
              and got[0] + got[2] <= wx + ww and got[1] + got[3] <= wy + wh,
              f"{got} in {wa()}")
        app.quit()
        return False

    first = {"1": p1_moved, "2": p2_restored, "3": p3_noxid,
             "4": p4_verbatim}[phase]

    def start(left=40):
        w = None
        try:
            w = app.windows[0]
        except IndexError:
            pass
        if w is None or not w.get_realized():
            if left <= 0:
                check("window_realized", False, "never realized")
                app.quit()
                return False
            return again(lambda: start(left - 1), 200)
        return again(guard(first), 500)

    GLib.timeout_add(400, guard(start))


class FakeSurface:
    """A toplevel surface WITHOUT get_xid — exactly what a Wayland surface
    is.  connect()/set_cursor() keep the real callers (aspect wiring,
    chrome cursor) alive; the missing get_xid is the whole point."""

    def connect(self, *_a, **_k):
        return 0

    def set_cursor(self, *_a):
        return None


def run_noxid_phase() -> int:
    """Process C: shadow ReelseeWindow.get_surface app-wide (the python-side
    calls wingeom placement makes) with a non-X11 stand-in, then run the
    SAME production open path — plan, adopt, present, configure-attempt."""
    import logging

    import window
    import x11pos
    fake = FakeSurface()
    window.ReelseeWindow.get_surface = lambda self: fake
    x11pos.reset_logging_for_tests()
    logs: list = []

    class Cap(logging.Handler):
        def emit(self, record):
            try:
                logs.append(record.getMessage())
            except Exception:                       # noqa: BLE001
                pass

    lg = logging.getLogger("reelsee.x11pos")
    lg.setLevel(logging.DEBUG)
    lg.addHandler(Cap())
    shared = {"logs": logs}
    rc = reelsee.run(["reelsee"],
                     on_app=lambda a: base_schedule(a, "3", shared))
    check("p3_inner_rc", rc == 0, str(rc))
    return rc


class PanelMonitor:
    """A monitor with a bottom panel: its WORK area is PANEL_H shorter than
    its GEOMETRY — the desktop of the operator recipe.  Only the two calls
    the placement code makes are wrapped; the rest delegates.  GDK's own C
    side keeps the real monitor list, so the X11 window still maps normally.
    """

    def __init__(self, real):
        self._real = real

    def get_workarea(self):
        r = self._real.get_workarea()
        return SimpleNamespace(x=r.x, y=r.y, width=r.width,
                               height=max(1, r.height - PANEL_H))

    def get_geometry(self):
        g = self._real.get_geometry()
        return SimpleNamespace(x=g.x, y=g.y, width=g.width, height=g.height)

    def __getattr__(self, name):
        return getattr(self._real, name)


def run_panel_phase() -> int:
    """Process D: remember a panel-hung rect, simulate a bottom panel, then
    open the app for real — the restore must be verbatim (§0.1)."""
    import config
    import gi
    gi.require_version("Gdk", "4.0")
    from gi.repository import Gdk

    store = config.GeometryStore(GEOM_FILE)
    store.remember(PANEL_RECT, 0)
    store.flush()
    check("p4_seeded", store.last() == PANEL_RECT, str(store.last()))

    real_get_monitors = Gdk.Display.get_monitors
    Gdk.Display.get_monitors = (
        lambda self: [PanelMonitor(m) for m in real_get_monitors(self)])
    try:
        rc = reelsee.run(["reelsee"],
                         on_app=lambda a: base_schedule(a, "4"))
    finally:
        Gdk.Display.get_monitors = real_get_monitors
    check("p4_inner_rc", rc == 0, str(rc))
    return rc


def main() -> int:
    if PHASE != "1":
        if PHASE == "3":
            rc = run_noxid_phase()
        elif PHASE == "4":
            rc = run_panel_phase()
        else:
            rc = reelsee.run(["reelsee"],
                             on_app=lambda a: base_schedule(a, PHASE))
            check(f"phase{PHASE}_inner_rc", rc == 0, str(rc))
        finish()
        return 0
    # parent: process A in this interpreter...
    rc = reelsee.run(["reelsee"], on_app=lambda a: base_schedule(a, "1"))
    check("phase1_rc", rc == 0, str(rc))
    try:
        with open(GEOM_FILE, encoding="utf-8") as fh:
            disk = json.load(fh)
    except Exception as exc:                        # noqa: BLE001
        disk = {"error": repr(exc)}
    got = (disk.get("rect") or {})
    ok = all(abs(int(got.get(k, -9999)) - v) <= 1
             for k, v in zip("xywh", T1))
    check("phase1_rect_on_disk", ok, json.dumps(disk))
    check("phase1_monitor_recorded", disk.get("monitor") == 0,
          str(disk.get("monitor")))
    # ...then B (restore + cascade + flip), C (no-xid) and D (panel) as FRESH
    # processes over the same state dir (C and D get their own).
    for ph in ("2", "3", "4"):
        # THE dirs travel with the child (its own pid would otherwise pick
        # a fresh scratch dir and phase B would have nothing to restore)
        env = dict(os.environ, REELSEE_R10WG_PHASE=ph,
                   REELSEE_R10WG_CFG_DIR=str(CFG_DIR),
                   REELSEE_R10WG_STATE_DIR=str(STATE_DIR))
        if ph in ("3", "4"):   # own dirs: no restored rect to confuse
            env["REELSEE_R10WG_STATE_DIR"] = str(STATE_DIR) + f"-p{ph}"
            env["REELSEE_R10WG_CFG_DIR"] = str(CFG_DIR) + f"-p{ph}"
        out = subprocess.run(
            [sys.executable, os.path.abspath(__file__)],
            env=env, capture_output=True, text=True, timeout=280)
        sys.stdout.write(out.stdout)
        sys.stderr.write(out.stderr[-4000:])
        check(f"phase{ph}_rc", out.returncode == 0, str(out.returncode))
        check(f"phase{ph}_asserts", "SMOKE_ASSERTS_PASSED" in out.stdout,
              "marker")
    shutil.rmtree(CFG_DIR, ignore_errors=True)
    shutil.rmtree(STATE_DIR, ignore_errors=True)
    for ph in ("3", "4"):
        shutil.rmtree(str(CFG_DIR) + f"-p{ph}", ignore_errors=True)
        shutil.rmtree(str(STATE_DIR) + f"-p{ph}", ignore_errors=True)
    finish()
    return 0


if __name__ == "__main__":
    sys.exit(main())
