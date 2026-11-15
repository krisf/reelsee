#!/usr/bin/env python3
"""R24 gate: position resume over REAL windows, end to end.

The unit tests prove the arithmetic; this proves the promise the operator
actually complains about — "it does not resume position".  Two phases, each
with its OWN scratch state dir (the `*-state-<pid>` convention so
headless-smoke.sh's per-invocation purge does not touch them):

  Phase A (in-process launch): seed window.json with rect (300,220,900,700)
  on monitor 0, launch the app for real, wait for the window to map, read
  the rect back through the SAME raw-Xlib seam the app uses (x11pos.get_rect)
  and require it at the remembered place (±4 px, size EXACT).

  Phase B (fresh process, its own state dir): same seed, then the "user"
  moves the window to (420,260) through x11pos — the app's own move seam —
  let the sampler run over the moved window, quit cleanly, and assert the
  state file the app WROTE holds x=420 y=260 with monitor 0 still recorded
  (R24 §C.2: on X11 the position is measured, so the index is recomputed
  and must still be right).

  Phase N (negative control, fresh process, no xid at all): the SAME seed
  with the surface deliberately non-X11 — the state of an unpinned Wayland
  launch.  The app must still open at the remembered SIZE, must NOT claim it
  placed the window, and must say so ONCE at INFO (R24 §C.1).  Without this
  phase, phase A's position assertions could pass by measuring "the app
  started" instead of "the position came back".

Phase A runs in this interpreter; B and N are fresh processes, which is what
"relaunch" means anyway (a second Gtk.Application in one process is not the
real thing).  Every check is a SMOKE_CHECK line; the battery greps for
SMOKE_ASSERTS_PASSED.
"""
import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))
from smoke_harness import SCRATCH, check, finish  # noqa: E402

PHASE = os.environ.get("REELSEE_R24GR_PHASE", "parent")
ROOT_STATE = Path(os.environ.get("REELSEE_R24GR_STATE_DIR")
                  or SCRATCH / f"geomresume-state-{os.getpid()}")
ROOT_CFG = Path(os.environ.get("REELSEE_R24GR_CFG_DIR")
                or SCRATCH / f"geomresume-cfg-{os.getpid()}")


def use_dirs(state_dir, cfg_dir):
    """Point the app (and this gate's own reads) at one phase's dirs.  The
    dirs are created HERE, never exported to the wrapper: headless-smoke.sh
    purges $REELSEE_STATE_DIR/window.json when that variable is set in ITS
    own environment, which would delete the seed before the app ever read
    it."""
    for d in (state_dir, cfg_dir):
        d.mkdir(parents=True, exist_ok=True)
    os.environ["REELSEE_STATE_DIR"] = str(state_dir)
    os.environ["REELSEE_CONFIG_DIR"] = str(cfg_dir)


def geom_file():
    return Path(os.environ["REELSEE_STATE_DIR"]) / "window.json"


SEED = (300, 220, 900, 700)          # what phase A must come back at
MOVED = (420, 260, 900, 700)         # phase B: where the "user" drags it
TOL = 4                              # px, per the brief (settle jitter)
N_LOGS: list = []                    # phase N: reelsee.x11pos records, in order

# Every process — parent or phase child — starts pointed at its own dirs;
# the parent then repoints per phase.  Without this the child would inherit
# the parent's REELSEE_STATE_DIR and write phase B's file into phase A's
# directory, which is exactly the "two phases sharing one state dir" trap.
use_dirs(ROOT_STATE, ROOT_CFG)

import reelsee  # noqa: E402


def seed(phase):
    """Write the state file with the app's OWN store, so the gate tests the
    format the app reads rather than a hand-rolled one."""
    import config
    store = config.GeometryStore(geom_file())
    store.remember(SEED, 0)
    store.flush()
    check(f"{phase}_seeded", store.last() == SEED, str(store.last()))


def base_schedule(app, phase):
    from gi.repository import GLib
    state = {"app": app}

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

    def rect(i=0):
        return x11pos.get_rect(win(i))

    def near(got, want, tol=TOL):
        return (got is not None
                and all(abs(a - b) <= tol for a, b in zip(got, want)))

    # ------------------------------------------------------------ phase A
    def a_mapped():
        return wait_for("a_window_mapped", lambda: win().get_mapped(),
                        guard(a_settle_done), see=rect)

    def a_settle_done():
        # read the rect only once the restore has REPORTED an outcome: the
        # settle loop re-asserts the move until the window is observed at the
        # target, so a read taken mid-loop measures the fight, not the
        # restore (and would pass or fail by luck).
        return wait_for(
            "a_restore_settled",
            lambda: getattr(win(), "_geom_settle", None) is None
            and getattr(win(), "_geom_settle_reason", None) == "settled",
            guard(a_restored), see=lambda: (
                getattr(win(), "_geom_settle", None),
                getattr(win(), "_geom_settle_reason", None), rect()))

    def a_restored():
        r = rect()
        check("a_rect_read", r is not None, str(r))
        check("a_restored_x", r is not None and abs(r[0] - SEED[0]) <= TOL,
              f"{r} want x={SEED[0]}±{TOL}")
        check("a_restored_y", r is not None and abs(r[1] - SEED[1]) <= TOL,
              f"{r} want y={SEED[1]}±{TOL}")
        # the size is not "close enough": a remembered rect must come back
        # with the same w/h or the aspect lock fights it on the next frame
        check("a_size_exact", r is not None and r[2:] == SEED[2:],
              f"{r} want {SEED[2:]}")
        # R21: the restore is a settle loop that re-asserts the move until
        # the window is OBSERVED there — a silent give-up is exactly the bug
        # this gate exists to catch, so the outcome has to be reported.
        check("a_settle_reason",
              getattr(win(), "_geom_settle_reason", None) == "settled",
              str(getattr(win(), "_geom_settle_reason", None)))
        check("a_settle_bounded",
              1 <= getattr(win(), "_geom_settle_moves", 0) <= 8,
              str(getattr(win(), "_geom_settle_moves", None)))
        app.quit()
        return False

    # ------------------------------------------------------------ phase B
    def b_mapped():
        return wait_for("b_window_mapped", lambda: win().get_mapped(),
                        guard(b_settle_done), see=rect)

    def b_settle_done():
        # The "user" can only drag the window AFTER the app has finished
        # placing it: the R21 settle loop re-asserts the remembered rect
        # until the window is OBSERVED there, so a synthetic move issued
        # mid-settle loses to the loop and the gate would measure the loop,
        # not the save.
        return wait_for(
            "b_restore_settled",
            lambda: getattr(win(), "_geom_settle", None) is None
            and getattr(win(), "_geom_settle_reason", None) == "settled",
            guard(b_moved), see=lambda: (getattr(win(), "_geom_settle", None),
                                         getattr(win(),
                                                 "_geom_settle_reason", None),
                                         rect()))

    def b_moved():
        # the "user" drags it: the app's own Xlib seam, size unchanged
        ok = x11pos.set_rect(win(), MOVED)
        check("b_move_sent", bool(ok), str(ok))
        return wait_for("b_at_target", lambda: near(rect(), MOVED),
                        guard(b_settled), see=rect)

    def b_settled():
        # give the 1 s sampler a full period over the MOVED window: the value
        # the flush below writes must have come through the live sampling
        # path, not a stale copy from before the move.
        return again(guard(b_sampled), 1400)

    def b_sampled():
        check("b_tracked", win()._geom_rect is not None
              and near(win()._geom_rect, MOVED), str(win()._geom_rect))
        # §A.1: the debounced write is supposed to land on its own — a file
        # that only gets written by the quit hook would lose a crash.
        disk = read_state().get("rect") or {}
        check("b_debounce_saved",
              int(disk.get("x", -1)) == MOVED[0]
              and int(disk.get("y", -1)) == MOVED[1], json.dumps(disk))
        return again(guard(b_quit), 300)

    def b_quit():
        app.quit()
        return False

    # ------------------------------------------- phase N (negative control)
    def n_mapped():
        # Same seed, same code path — but the placement seam cannot see or
        # move the window, which is EXACTLY the state of an unpinned Wayland
        # launch (no xid).  If the position assertions in phase A could pass
        # vacuously, they would pass here too; they must not.
        return wait_for("n_window_mapped", lambda: win().get_mapped(),
                        guard(n_settled), see=lambda: win()._geom_rect)

    def n_settled():
        return wait_for(
            "n_gave_up",
            lambda: getattr(win(), "_geom_settle", None) is None
            and getattr(win(), "_geom_settle_reason", None) is not None,
            guard(n_checks), see=lambda: (
                getattr(win(), "_geom_settle", None),
                getattr(win(), "_geom_settle_reason", None)))

    def n_checks():
        w = win()
        check("n_position_unobservable", x11pos.get_rect(w) is None,
              str(x11pos.get_rect(w)))
        check("n_not_settled",
              getattr(w, "_geom_settle_reason", None) != "settled",
              str(getattr(w, "_geom_settle_reason", None)))
        # The documented degradation: size-only, never a crash.  Self-
        # consistency against GDK's own reported size is the invariant —
        # under WM-less Xvfb GTK draws CSD shadows, so get_width() sits a
        # few px under the X size (smoke_wingeom p3's lesson: mirror what
        # GDK reports, never assert a literal).
        check("n_size_tracks_gdk",
              w._geom_rect is not None
              and w._geom_rect[2:] == (w.get_width(), w.get_height()),
              f"{w._geom_rect} vs {w.get_width()}x{w.get_height()}")
        # ...and the position half of the tracked rect is CARRIED FORWARD,
        # not measured.  Saying so out loud is the point: the app must not
        # claim it placed the window.
        check("n_position_carried_forward",
              w._geom_rect is not None and w._geom_rect[:2] == SEED[:2],
              str(w._geom_rect))
        # R24 §C.1: the ONE line that explains the degradation must be
        # visible at the default level, and still only once.
        notices = [r for r in N_LOGS if "no xid" in r.getMessage()]
        check("n_one_info_notice", len(notices) == 1, str(notices))
        check("n_notice_is_info",
              bool(notices) and notices[0].levelno == logging.INFO,
              str([r.levelname for r in notices]))
        app.quit()
        return False

    return {"A": a_mapped, "B": b_mapped, "N": n_mapped}[phase]()


class FakeSurface:
    """A toplevel surface WITHOUT get_xid — exactly what a Wayland surface
    is.  connect()/set_cursor() keep the real callers (aspect wiring, chrome
    cursor) alive; the missing get_xid is the whole point (smoke_wingeom p3's
    stand-in, same shape on purpose)."""

    def connect(self, *_a, **_k):
        return 0

    def set_cursor(self, *_a):
        return None


def run_phase(phase):
    seed(phase)
    if os.environ.get("REELSEE_R24GR_NOXID") == "1":
        # The unpinned-Wayland condition, produced the way it happens in the
        # field: the surface simply has no xid, so the REAL x11pos code runs
        # and degrades.  (Shadowing x11pos itself would skip the very log
        # line this phase is about.)
        import window
        import x11pos
        fake = FakeSurface()
        window.ReelseeWindow.get_surface = lambda self: fake
        x11pos.reset_logging_for_tests()
        lg = logging.getLogger("reelsee.x11pos")
        lg.setLevel(logging.DEBUG)
        lg.addHandler(_Collect())
    rc = reelsee.run(["reelsee"], on_app=lambda a: base_schedule(a, phase))
    check(f"phase{phase}_inner_rc", rc == 0, str(rc))
    return rc


class _Collect(logging.Handler):
    """Every reelsee.x11pos record this process emits (phase N counts them)."""

    def emit(self, record):
        N_LOGS.append(record)


def read_state():
    try:
        with open(geom_file(), encoding="utf-8") as fh:
            return json.load(fh)
    except Exception as exc:                        # noqa: BLE001
        return {"error": repr(exc)}


def main() -> int:
    if PHASE in ("A", "B", "N"):
        rc = run_phase(PHASE)
        finish()
        return 0 if rc == 0 else 1

    # ---------------- parent: A in this interpreter, B as a fresh process
    a_dir, b_dir = ROOT_STATE / "a", ROOT_STATE / "b"
    a_cfg, b_cfg = ROOT_CFG / "a", ROOT_CFG / "b"

    use_dirs(a_dir, a_cfg)
    rc = run_phase("A")
    check("phaseA_rc", rc == 0, str(rc))
    # phase A quit without moving anything: what it wrote must still be the
    # seed (an unmeasured position must never be "improved" on the way out)
    disk = read_state()
    got = disk.get("rect") or {}
    check("phaseA_disk_matches_seed",
          all(abs(int(got.get(k, -9999)) - v) <= TOL
              for k, v in zip("xywh", SEED)), json.dumps(disk))

    env = dict(os.environ, REELSEE_R24GR_PHASE="B",
               REELSEE_R24GR_STATE_DIR=str(b_dir),
               REELSEE_R24GR_CFG_DIR=str(b_cfg))
    out = subprocess.run([sys.executable, os.path.abspath(__file__)],
                         env=env, capture_output=True, text=True, timeout=90)
    sys.stdout.write(out.stdout)
    sys.stderr.write(out.stderr[-4000:])
    check("phaseB_rc", out.returncode == 0, str(out.returncode))
    check("phaseB_asserts", "SMOKE_ASSERTS_PASSED" in out.stdout, "marker")

    use_dirs(b_dir, b_cfg)
    disk = read_state()
    got = disk.get("rect") or {}
    check("b_saved_x", int(got.get("x", -1)) == MOVED[0], json.dumps(disk))
    check("b_saved_y", int(got.get("y", -1)) == MOVED[1], json.dumps(disk))
    check("b_saved_size",
          int(got.get("w", -1)) == MOVED[2]
          and int(got.get("h", -1)) == MOVED[3], json.dumps(disk))
    check("b_saved_monitor", disk.get("monitor") == 0,
          str(disk.get("monitor")))

    # ---- negative control: the SAME seed with the placement seam blind.
    # This is the state of an unpinned Wayland launch (no xid ⇒ no position,
    # in either direction).  Phase A's assertions must be able to fail here,
    # otherwise they would be measuring "the app started", not "the position
    # came back" (AGENTS.md §4: a negative control wherever a vacuous pass is
    # conceivable).
    n_dir, n_cfg = ROOT_STATE / "n", ROOT_CFG / "n"
    env = dict(os.environ, REELSEE_R24GR_PHASE="N",
               REELSEE_R24GR_NOXID="1",
               REELSEE_R24GR_STATE_DIR=str(n_dir),
               REELSEE_R24GR_CFG_DIR=str(n_cfg))
    out = subprocess.run([sys.executable, os.path.abspath(__file__)],
                         env=env, capture_output=True, text=True, timeout=90)
    sys.stdout.write(out.stdout)
    sys.stderr.write(out.stderr[-4000:])
    check("phaseN_rc", out.returncode == 0, str(out.returncode))
    check("phaseN_asserts", "SMOKE_ASSERTS_PASSED" in out.stdout, "marker")

    shutil.rmtree(ROOT_STATE, ignore_errors=True)
    shutil.rmtree(ROOT_CFG, ignore_errors=True)
    finish()
    return 0


if __name__ == "__main__":
    sys.exit(main())
