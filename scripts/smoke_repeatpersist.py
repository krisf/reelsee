#!/usr/bin/python3
"""R9 gate smoke: the repeat choice PERSISTS (three real processes).

Phase 1 (process A, fresh config): repeat OFF at start; the NATIVE "r"
and "l" keys (real XTEST presses, focus-then-fake-input exactly like the
r6mute gate) walk the mode off -> all -> one — the mpv properties AND
the icon AND the context radio AND the config disk follow every press,
and NOT one call reaches _set_repeat_mode on that path (the key hole is
closed by the snapshot observer, not by routing through the button
writer).  Phase 2 (fresh process, same dirs) asserts with NO
interaction that the restart is already repeating: the FIRST observed
loop-file/loop-playlist values are the persisted pair (mpv is born with
them as init options — the paint log never once saw "off"), the
bottom-bar glyph is the "one" icon undimmed, and the radio says "one".
Then the user-visible contract inside process B: the same-file RELOAD
keeps the mode (this was the bug), the file really LOOPS, "all"
ADVANCES A SINGLE-ITEM playlist, next/prev keep all and off, OFF lands
on disk before quit.  Phase 3 (fresh process #3) proves OFF survives
restart too, icon dimmed from the very first paint.
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))
from smoke_harness import SCRATCH, check, finish  # noqa: E402

# per-run scratch dirs (children are told the exact paths via env): the
# three battery invocations of this gate never inherit each other's
# config, yet all three walk the identical fresh -> persisted -> restart
# journey.
CFG_DIR = Path(os.environ.get("REELSEE_R9REP_CFG_DIR")
               or SCRATCH / f"r9rep-cfg-{os.getpid()}")
STATE_DIR = Path(os.environ.get("REELSEE_R9REP_STATE_DIR")
                 or SCRATCH / f"r9rep-state-{os.getpid()}")
for _d in (CFG_DIR, STATE_DIR):
    _d.mkdir(parents=True, exist_ok=True)

PHASE = os.environ.get("REELSEE_R9REP_PHASE", "1")
CFG_FILE = CFG_DIR / "config.json"
if PHASE == "1" and CFG_FILE.exists():
    CFG_FILE.unlink()
os.environ["REELSEE_CONFIG_DIR"] = str(CFG_DIR)
os.environ["REELSEE_STATE_DIR"] = str(STATE_DIR)

import reelsee  # noqa: E402

CLIP1 = "testdata/perf_1080p30.mp4"      # 10.0 s
CLIP2 = "testdata/smoke_360p.mp4"        #  3.0 s
SONG = "media-playlist-repeat-song-symbolic"
LOOP = "media-playlist-repeat-symbolic"
OFF_VALS = ("no", "nil", "None", "")
state: dict = {}


def base_schedule(app, phase):
    from gi.repository import GLib
    state["app"] = app

    def win():
        return app.windows[0]

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

    def wait_for(desc, pred, then, left=40):
        try:
            val = pred()
        except Exception:                           # noqa: BLE001
            val = None
        if val:
            check(desc, True, "ok" if val is True else str(val))
            return again(then, 80)
        if left <= 0:
            check(desc, False, "timeout")
            app.quit()
            return False
        return again(lambda: wait_for(desc, pred, then, left - 1), 120)

    def snap():
        return win().core.snapshot

    def loops():
        s = snap()
        return (str(s.get("loop-file")), str(s.get("loop-playlist")))

    def pos():
        p = snap().get("time-pos")
        return None if p is None else float(p)

    def fname():
        f = snap().get("filename")
        return os.path.basename(str(f)) if f else ""

    def playing():
        return snap().get("pause") is False

    def icon():
        return win().chrome._repeat_icon.get_icon_name()

    def dimmed():
        return win().chrome.btn_repeat.has_css_class("reelsee-repeat-off")

    def radio():
        return win()._ctx_repeat.get_state().unpack()

    def click():
        win().chrome.btn_repeat.emit("clicked")

    def disk():
        try:
            with open(CFG_FILE, encoding="utf-8") as fh:
                pb = json.load(fh).get("playback", {})
                return (str(pb.get("loop_file")),
                        str(pb.get("loop_playlist")))
        except FileNotFoundError:
            return (None, None)

    def expect_mode(mode, lf, lp, glyph, dim, want_disk=True):
        # the full agreement contract: mpv props, icon+dim, radio, disk
        check(f"props_{mode}", loops() == (lf, lp), repr(loops()))
        check(f"icon_{mode}", icon() == glyph, icon())
        check(f"dim_{mode}", dimmed() is dim, str(dimmed()))
        check(f"radio_{mode}", radio() == mode, radio())
        if want_disk:      # fresh-dir phase-1 has no config file yet
            check(f"disk_{mode}", disk() == (lf, lp), repr(disk()))

    def ensure_xd():
        if "xd" not in state:
            from Xlib import display as xdisplay
            state["xd"] = xdisplay.Display(
                os.environ.get("DISPLAY", ":0"))
        return state["xd"]

    def xkey(ch):
        from Xlib import X, XK
        from Xlib.ext import xtest
        d = ensure_xd()
        kc = d.keysym_to_keycode(getattr(XK, f"XK_{ch}"))
        xtest.fake_input(d, X.KeyPress, kc)
        d.sync()
        xtest.fake_input(d, X.KeyRelease, kc)
        d.sync()

    def focus_window():
        from Xlib import X
        d = ensure_xd()
        win().present()
        d.sync()
        root = d.screen().root
        big = [None, 0]

        def walk(c, dep=0):
            if dep > 6:
                return
            try:
                if c.get_attributes().map_state == X.IsViewable and c != root:
                    g = c.get_geometry()
                    if g.width >= 800 and g.height >= 400 and \
                            g.width * g.height > big[1]:
                        big[0], big[1] = c, g.width * g.height
            except Exception:                       # noqa: BLE001
                pass
            for k in c.query_tree().children:
                walk(k, dep + 1)
        walk(root)
        if big[0] is not None:
            big[0].set_input_focus(X.RevertToParent, X.CurrentTime)
            d.sync()

    def instrument():
        """record every paint-path repeat mode, every _set_repeat_mode
        call (the r/l keys must persist WITHOUT the latter) and every
        file_loaded event (the reload proof)."""
        w = win()
        state["paint_modes"] = []
        state["writer_calls"] = 0
        state["loads"] = 0

        def counted_load(*_a):
            state["loads"] += 1

        _sr = w.chrome.set_repeat

        def paint(mode):
            state["paint_modes"].append(mode)
            return _sr(mode)

        w.chrome.set_repeat = paint
        w.core.on("file_loaded", counted_load)
        _wr = w._set_repeat_mode

        def writer(mode, action=None):
            state["writer_calls"] += 1
            return _wr(mode, action=action)

        w._set_repeat_mode = writer

    # ------------------------------------------------------------ phase 1
    # fresh process, fresh config: the NATIVE keys walk the tri-state
    # and every leg must persist on the spot.
    def p1_ready():
        # the snapshot must carry the two loop properties before the
        # OFF baseline is meaningful
        return wait_for("p1_props_observed",
                        lambda: "loop-file" in snap()
                        and "loop-playlist" in snap(),
                        guard(p1_playing))

    def p1_playing():
        # fresh scratch dir: no config.json on disk yet, so the off
        # state is asserted for mpv + icon + radio only here
        expect_mode("off", "no", "no", LOOP, True, want_disk=False)
        state["calls0"] = state["writer_calls"]
        focus_window()
        xkey("r")
        return again(guard(lambda: wait_for(
            "p1_r_cycles_playlist", lambda: loops()[1] not in OFF_VALS,
            guard(p1_r_persists))), 100)

    def p1_r_persists():
        # THE key hole, closed: the raw `r` never touched the button
        # writer, yet props, radio AND disk all say "all".
        check("p1_r_key_persisted", disk() == ("no", "inf"), repr(disk()))
        check("p1_r_radio_all", radio() == "all", radio())
        check("p1_r_undimmed", dimmed() is False, str(dimmed()))
        check("p1_r_no_writer_call", state["writer_calls"] == state["calls0"],
              f"{state['calls0']} -> {state['writer_calls']}")
        focus_window()
        xkey("l")
        return again(guard(lambda: wait_for(
            "p1_l_cycles_file", lambda: loops()[0] not in OFF_VALS,
            guard(p1_l_persists))), 100)

    def p1_l_persists():
        # the RAW-key contract: cycle keeps mpv's own numeric value live
        # (this mpv turns `cycle loop-playlist` into "1"/"2" — that is
        # mpv's business, smoke_repeat pins it), while the OBSERVER
        # canonicalises: disk gets the exact pair, and the derived mode
        # drives icon + dim + radio.  The exact LIVE pair is the load
        # path's contract (_push_all_settings), asserted in phases 2/3.
        import chrome
        check("p1_l_key_persisted", disk() == ("inf", "no"), repr(disk()))
        check("p1_l_mode_one", chrome.repeat_mode(*loops()) == "one",
              repr(loops()))
        check("p1_l_icon_one", icon() == SONG and not dimmed(),
              f"{icon()} dim={dimmed()}")
        check("p1_l_radio_one", radio() == "one", radio())
        check("p1_l_no_writer_call", state["writer_calls"] == state["calls0"],
              f"{state['calls0']} -> {state['writer_calls']}")
        app.quit()
        return False

    # ------------------------------------------------------------ phase 2
    def p2_seed():
        # NO interaction yet — the restart must already be repeating.
        s = snap()
        check("p2_born_looping", str(s.get("loop-file")) == "inf"
              and str(s.get("loop-playlist")) == "no",
              repr((s.get("loop-file"), s.get("loop-playlist"))))
        modes = state["paint_modes"]
        check("p2_no_off_paint", "off" not in modes and len(modes) >= 2,
              repr(modes[-4:]))
        check("p2_glyph_one", icon() == SONG and not dimmed(),
              f"{icon()} dim={dimmed()}")
        check("p2_radio_one", radio() == "one", radio())
        return again(guard(lambda: wait_for(
            "p2_playing", playing, guard(p2_loaded))), 100)

    def p2_loaded():
        expect_mode("one", "inf", "no", SONG, False)
        # same-file RELOAD — the exact pre-load-push path that erased
        # the mode before R9.  The file_loaded counter pins down that
        # the NEW file really loaded (a loop-wrap of a freshly started
        # 10 s file cannot fake it inside this window).
        state["loads0"] = state["loads"]
        win()._load_path(CLIP1)
        return again(guard(lambda: wait_for(
            "p2_reload_file_loaded",
            lambda: state["loads"] >= state["loads0"] + 1,
            guard(p2_reload_keeps))), 100)

    def p2_reload_keeps():
        expect_mode("one", "inf", "no", SONG, False)
        # the file must ACTUALLY loop: seek near the end, watch it wrap
        state["near"] = False

        def near_wrap():
            p = pos()
            if p is None:
                return False
            if p > 7.5:
                state["near"] = True
            return state["near"] and p < 3.0
        win().core.command("seek", "8.5", "absolute")
        return again(guard(lambda: wait_for(
            "p2_one_really_loops", near_wrap, guard(p2_loop_keeps),
            left=120)), 100)

    def p2_loop_keeps():
        expect_mode("one", "inf", "no", SONG, False)
        click()                              # one -> off
        return again(guard(lambda: wait_for(
            "p2_off_props", lambda: loops() == ("no", "no"),
            guard(p2_off))), 100)

    def p2_off():
        expect_mode("off", "no", "no", LOOP, True)
        click()                              # off -> all
        return again(guard(lambda: wait_for(
            "p2_all_props", lambda: loops() == ("no", "inf"),
            guard(p2_all))), 100)

    def p2_all():
        expect_mode("all", "no", "inf", LOOP, False)
        # "all" must ADVANCE A SINGLE-ITEM list: replace-load the 3 s
        # clip (playlist of one; the pre-load push must re-apply all),
        # seek near its end and watch the list loop it back.
        win()._load_path(CLIP2)
        return again(guard(lambda: wait_for(
            "p2_single_loaded",
            lambda: fname() == "smoke_360p.mp4" and playing()
            and pos() is not None and pos() < 2.5,
            guard(p2_all_push))), 100)

    def p2_all_push():
        check("p2_all_survives_load", loops() == ("no", "inf"),
              repr(loops()))
        state["near2"] = False

        def near_wrap2():
            p = pos()
            if p is None:
                return False
            if p > 2.0:
                state["near2"] = True
            return state["near2"] and p < 1.2
        win().core.command("seek", "2.4", "absolute")
        return again(guard(lambda: wait_for(
            "p2_all_loops_single_item_list", near_wrap2,
            guard(p2_all_after_wrap), left=120)), 100)

    def p2_all_after_wrap():
        check("p2_still_all_after_list_loop", loops() == ("no", "inf"),
              repr(loops()))
        win()._load_path(CLIP1, "append")
        return again(guard(lambda: wait_for(
            "p2_playlist_two",
            lambda: isinstance(snap().get("playlist"), list)
            and len(snap().get("playlist")) == 2, guard(p2_next))), 100)

    def p2_next():
        win().core.command("playlist-next")
        return again(guard(lambda: wait_for(
            "p2_next_clip1", lambda: fname() == "perf_1080p30.mp4",
            guard(p2_next_keeps))), 100)

    def p2_next_keeps():
        check("p2_next_keeps_all", loops() == ("no", "inf"), repr(loops()))
        check("p2_next_radio_all", radio() == "all", radio())
        win().core.command("playlist-prev")
        return again(guard(lambda: wait_for(
            "p2_prev_clip2", lambda: fname() == "smoke_360p.mp4",
            guard(p2_prev_keeps))), 100)

    def p2_prev_keeps():
        check("p2_prev_keeps_all", loops() == ("no", "inf"), repr(loops()))
        click()                              # all -> one
        return again(guard(lambda: wait_for(
            "p2_mid_one", lambda: loops() == ("inf", "no"),
            guard(p2_to_off))), 100)

    def p2_to_off():
        click()                              # one -> off
        return again(guard(lambda: wait_for(
            "p2_off2", lambda: loops() == ("no", "no"),
            guard(p2_final_off))), 100)

    def p2_final_off():
        expect_mode("off", "no", "no", LOOP, True)
        check("p2_off_on_disk_before_quit", disk() == ("no", "no"),
              repr(disk()))
        app.quit()
        return False

    # ------------------------------------------------------------ phase 3
    def p3_seed():
        s = snap()
        check("p3_born_off", str(s.get("loop-file")) == "no"
              and str(s.get("loop-playlist")) == "no",
              repr((s.get("loop-file"), s.get("loop-playlist"))))
        modes = state["paint_modes"]
        check("p3_paints_off", modes and all(m == "off" for m in modes),
              repr(modes[-4:]))
        check("p3_glyph_off", icon() == LOOP and dimmed(),
              f"{icon()} dim={dimmed()}")
        check("p3_radio_off", radio() == "off", radio())
        return again(guard(lambda: wait_for(
            "p3_playing", playing, guard(p3_after_load))), 100)

    def p3_after_load():
        expect_mode("off", "no", "no", LOOP, True)
        app.quit()
        return False

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
        instrument()
        first = {"1": p1_ready, "2": p2_seed, "3": p3_seed}[phase]
        # phase 1 presses real keys: give the map+focus a beat, like the
        # r6mute gate does
        return again(guard(first), 900 if phase == "1" else 400)

    GLib.timeout_add(400, guard(start))


def main() -> int:
    if PHASE != "1":
        rc = reelsee.run(["reelsee", CLIP1],
                         on_app=lambda a: base_schedule(a, PHASE))
        check(f"phase{PHASE}_inner_rc", rc == 0, str(rc))
        finish()
        return 0
    # parent: run process A here, then B and C as FRESH processes
    rc = reelsee.run(["reelsee", CLIP1],
                     on_app=lambda a: base_schedule(a, "1"))
    check("phase1_rc", rc == 0, str(rc))
    for ph in ("2", "3"):
        env = dict(os.environ, REELSEE_R9REP_PHASE=ph,
                   REELSEE_R9REP_CFG_DIR=str(CFG_DIR),
                   REELSEE_R9REP_STATE_DIR=str(STATE_DIR))
        out = subprocess.run(
            [sys.executable, os.path.abspath(__file__), CLIP1],
            env=env, capture_output=True, text=True, timeout=280)
        sys.stdout.write(out.stdout)
        sys.stderr.write(out.stderr[-4000:])
        check(f"phase{ph}_rc", out.returncode == 0, str(out.returncode))
        check(f"phase{ph}_asserts", "SMOKE_ASSERTS_PASSED" in out.stdout,
              "marker")
    shutil.rmtree(CFG_DIR, ignore_errors=True)
    shutil.rmtree(STATE_DIR, ignore_errors=True)
    finish()
    return 0


if __name__ == "__main__":
    sys.exit(main())
