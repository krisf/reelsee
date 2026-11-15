#!/usr/bin/python3
"""R28 gate: a PAspect the window VIOLATES is never published.

The defect (ROUND28-BRIEF §2, measured on the operator's Mutter —
scratch/diag/r28-diag4.log is our own confirmation): the first-file seam
publishes WM_NORMAL_HINTS PAspect BEFORE the §R10-B.1 adopted-geometry
skip returns — so an adopted 1200x900 (4:3) window playing a 16:9 clip
got min==max 16:9 written ONTO a size that violates it.  A real WM
answers that instantly: the window was clamped to 1266x792 within ONE
millisecond of the write landing, with no app-side resize logged; on the
operator's rig the same mechanism walked 1200x900 -> 370x230 -> 420x258
-> 54x54 and PERSISTED the tiny rect ("missing window" forever).

Xvfb has no window manager, so the clamp itself cannot be shown here —
what CAN be shown, and is exactly the app-side contract, is:

  * adopted 4:3 seed + 16:9 clip + lock ON: the property NEVER carries
    PAspect while the live size violates it, and the seed size is HELD
    (no self-inflicted resize) — the negative control (HEAD src tree)
    fails both of these checks because HEAD publishes unconditionally;
  * a resize that lands the window on the ratio (a user drag, raw
    MoveResizeWindow here — no WM to intercept) is DECLARED within the
    debounce window: bit 128, words (16,9), pinned;
  * a violating resize with the declaration live: while any sample
    shows an off-ratio size the property shows no PAspect, and once the
    §R8-B correction has landed the declaration is back (republish on
    the honest size).

The property is read back FROM THE SERVER with python-xlib and parsed by
this gate's OWN ICCCM code (never src/x11pos — the auditor may not borrow
the auditee's parser).

Checks: x_window_found, lock_active, seed_adopted_size,
gate_no_publish_on_violation, seed_held_size, publish_after_honest_size,
honest_size_no_fight, never_paspect_while_off_ratio,
republish_after_correction, window_alive, run_rc.
"""
import os
import sys

from smoke_harness import SCRATCH, check, finish

CFG_DIR = SCRATCH / "hintgate-cfg"
STATE_DIR = SCRATCH / "hintgate-state"
for _d in (CFG_DIR, STATE_DIR):
    _d.mkdir(parents=True, exist_ok=True)
for _f in (CFG_DIR / "config.json", STATE_DIR / "resume.json",
           STATE_DIR / "window.json"):
    if _f.exists():
        _f.unlink()

# The operator's exact repro state: adopted 1200x900 (4:3) rect, lock ON,
# crop OFF.  window.json is PHYSICAL (R25); Xvfb runs scale 1, so here the
# physical number IS the window size a real WM would have been given.
(CFG_DIR / "config.json").write_text(
    '{"video": {"crop_to_window": false}, '
    '"ui": {"lock_aspect": true, "remember_geometry": true}}')
(STATE_DIR / "window.json").write_text(
    '{"version": 1, "rect": {"x": 200, "y": 140, "w": 1200, "h": 900},'
    ' "monitor": null}')

os.environ["REELSEE_CONFIG_DIR"] = str(CFG_DIR)
os.environ["REELSEE_STATE_DIR"] = str(STATE_DIR)

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")

import reelsee  # noqa: E402

WIDE = "testdata/perf_1080p30.mp4"       # 1920x1080 -> 16:9
SEED = (1200, 900)                        # 4:3 — violates 16:9 by 25 %

# Gate-owned ICCCM table (see smoke_aspecthints: the auditor keeps its own).
_PASPECT = 1 << 7          # PAspect is 128, NOT PMaxSize (1 << 5) — R28 §1


def ratio_off(w, h, num=16, den=9):
    """Relative distance of (w, h) to the published ratio (same 5 % bar
    the app itself uses for a mismatch)."""
    return abs(w / h - num / den) / (num / den) <= 0.05


state: dict = {}


def sched(app):
    from gi.repository import GLib

    state["app"] = app

    def win():
        return app.windows[0]

    def guard(fn):
        def run(*a):
            try:
                return fn(*a)
            except Exception as exc:                        # noqa: BLE001
                import traceback
                traceback.print_exc()
                check(fn.__name__ + "_crashed", False, repr(exc))
                app.quit()
                return False
        run.__name__ = fn.__name__
        return run

    def again(fn, ms):
        GLib.timeout_add(ms, guard(fn))
        return False

    def wait_for(desc, pred, then, left=60):
        try:
            val = pred()
        except Exception:                                   # noqa: BLE001
            val = None
        if val:
            check(desc, True, "ok" if val is True else str(val))
            return again(then, 80)
        if left <= 0:
            check(desc, False, "timeout")
            app.quit()
            return False
        return again(lambda: wait_for(desc, pred, then, left - 1), 120)

    # ------------------------------------------------------------ X plumbing
    def xopen():
        from Xlib import display as xdisplay
        state["xd"] = xdisplay.Display(os.environ.get("DISPLAY", ":0"))

    def xfind():
        from Xlib import X
        d = state["xd"]
        root = d.screen().root
        found = [None]

        def walk(w, depth=0):
            if found[0] is not None or depth > 6:
                return
            try:
                nm = w.get_wm_name()
                nm = nm.decode("utf-8", "replace") if isinstance(
                    nm, bytes) else (nm or "")
                mapped = w.get_attributes().map_state == X.IsViewable
            except Exception:                               # noqa: BLE001
                return
            if "REELSEE" in nm and mapped and w != root:
                found[0] = w
                return
            try:
                kids = w.query_tree().children
            except Exception:                               # noqa: BLE001
                kids = []
            for k in kids:
                walk(k, depth + 1)
        walk(root)
        return found[0]

    def paspect_now():
        """(min_num, min_den, max_num, max_den) only when bit 128 is set ON
        THE SERVER, else None — own parser, live read, never cached."""
        from Xlib import X
        d = state["xd"]
        d.sync()
        prop = state["winx"].get_full_property(
            d.intern_atom("WM_NORMAL_HINTS"), X.AnyPropertyType)
        if prop is None or prop.format != 32 or prop.value is None:
            return None
        arr = [int(v) & 0xFFFFFFFF for v in prop.value]
        arr += [0] * (18 - len(arr))
        if not (arr[0] & _PASPECT):
            return None
        return (arr[11], arr[12], arr[13], arr[14])

    def wsize():
        g = state["winx"].get_geometry()
        return (g.width, g.height)

    def raw_resize(w, h):
        """A third-party MoveResizeWindow — the honest way to hand the
        window a NEW size under a WM-less Xvfb without going through the
        app (which is exactly what a WM clamp/user drag looks like from
        the app's outside)."""
        state["winx"].configure(width=w, height=h)
        state["xd"].sync()

    def wipe_property():
        """Mimic GDK's per-frame WM_NORMAL_HINTS clobber: the property
        leaves the building, and only the app's own machinery may put a
        PAspect back."""
        from Xlib import X
        state["winx"].delete_property(state["xd"].intern_atom(
            "WM_NORMAL_HINTS"))
        state["xd"].sync()

    def sampler(on_sample, left, done, every=60):
        def step(n):
            try:
                on_sample(wsize(), paspect_now())
            except Exception:                               # noqa: BLE001
                pass
            if n <= 0:
                return again(guard(done), 10)
            return again(lambda: step(n - 1), every)

        def start():
            return step(left)
        return start

    # ---------------------------------------------------------------- phases
    def s_seed_size():
        return wait_for("seed_adopted_size",
                        lambda: wsize() == SEED, s_lock, left=50)

    def s_lock():
        return wait_for("lock_active", lambda: win()._aspect_locked(),
                        s_load, left=40)

    def s_load():
        wipe_property()
        win()._load_path(WIDE)
        # THE gate phase starts WITH the load, not after it: on HEAD the
        # first-file publish lands within ~40 ms of file-loaded, and any
        # polling gap before sampling could hide it.  2.4 s of samples
        # cover the immediate publish, the 200 ms debounced republish and
        # the NOTICE repair path.
        return again(guard(sampler(state["on_watch_sample"], 40,
                                   s_watch_check)), 60)

    def s_watch_run():
        return wait_for("dwidth_wide_observed",
                        lambda: win().core.snapshot.get("dwidth") == 1920,
                        s_go_honest, left=50)

    def s_watch_check():
        smps = state["samples"]
        bad = [s for s in smps if s[1] is not None]
        check("gate_no_publish_on_violation", not bad, f"bad={bad[:3]}")
        moved = [s for s in smps if s[0] != SEED]
        check("seed_held_size", not moved, f"moved={moved[:3]}")
        return again(guard(s_watch_run), 60)

    def s_go_honest():
        wipe_property()
        raw_resize(960, 540)                    # EXACT 16:9 — now honest
        return again(guard(s_honest), 350)

    def s_honest():
        def pred():
            return paspect_now() == (16, 9, 16, 9)
        ok = wait_for("publish_after_honest_size", pred, s_after_publish)
        return ok

    def s_after_publish():
        check("honest_size_no_fight", wsize() == (960, 540), f"size={wsize()}")
        wipe_property()
        state["c_samples"] = []

        def on_sample(size, pasp):
            state["c_samples"].append((size, pasp))
        raw_resize(800, 600)                    # violates: 4:3 again
        # sample THROUGH the whole correction window, then assert
        return again(guard(sampler(on_sample, 30, s_c_watch)), 60)

    def s_c_watch():
        def pred():
            p = paspect_now()
            s = wsize()
            return (p == (16, 9, 16, 9) and ratio_off(*s)) or None
        return wait_for("republish_after_correction", pred, s_c_check,
                        left=50)

    def s_c_check():
        # No sample during the violating moment may show a PAspect:
        # the stale one was wiped, and neither apply nor NOTICE-repair
        # may re-declare while the live size is off-ratio.
        bad = [s for s in state["c_samples"] if s[1] is not None
               and not ratio_off(*s[0])]
        check("never_paspect_while_off_ratio", not bad, f"bad={bad[:3]}")
        check("correction_moved_size", state["c_samples"]
              and state["c_samples"][-1][0] != (800, 600),
              f"last={state['c_samples'][-1][0] if state['c_samples'] else None}")
        check("window_alive", win().get_realized(), "True")
        app.quit()
        return False

    def start(left=40):
        w = None
        try:
            w = app.windows[0]
        except IndexError:
            pass
        if w is None or not w.get_realized():
            return again(lambda: start(left - 1), 200)
        if "xd" not in state:
            xopen()
        x = xfind()
        if x is None:
            if left <= 0:
                check("x_window_found", False, "no REELSEE window")
                app.quit()
                return False
            return again(lambda: start(left - 1), 300)
        check("x_window_found", True, "mapped")
        state["winx"] = x
        state["samples"] = []

        def on_watch_sample(size, pasp):
            state["samples"].append((size, pasp))
        state["on_watch_sample"] = on_watch_sample
        w.present()
        state["xd"].sync()
        return again(guard(lambda: wait_for(
            "window_mapped", lambda: w.get_mapped(), s_seed_size, left=40)),
            300)

    GLib.timeout_add(300, guard(start))


def main() -> int:
    rc = reelsee.run(["reelsee"], on_app=sched)   # seeded state dir first
    check("run_rc", rc == 0, str(rc))
    finish()
    return 0


if __name__ == "__main__":
    sys.exit(main())
