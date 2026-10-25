#!/usr/bin/python3
"""R5-C gate smoke: repeat icon toggle in the BOTTOM bar beside the
progress bar.  Click cycles off -> all -> one -> off with the real mpv
properties (loop-file / loop-playlist strings), the icon glyph per mode,
the dim style only while off, tooltip/accessible label "Repeat: <mode>",
and a raw "l" keypress repainting the derived icon.  The old top-bar
"loop" button must be gone.  Runs under scripts/headless-smoke.sh."""
import os
import sys

from smoke_harness import SCRATCH, check, finish

CFG_DIR = SCRATCH / "repeat-cfg"
STATE_DIR = SCRATCH / "repeat-state"
for _d in (CFG_DIR, STATE_DIR):
    _d.mkdir(parents=True, exist_ok=True)
os.environ["REELSEE_CONFIG_DIR"] = str(CFG_DIR)
os.environ["REELSEE_STATE_DIR"] = str(STATE_DIR)
for _f in (CFG_DIR / "config.json", STATE_DIR / "resume.json"):
    if _f.exists():
        _f.unlink()

from gi.repository import Gdk, Gtk  # noqa: E402

import reelsee  # noqa: E402

state: dict = {}


def main() -> int:
    def schedule(app):
        from gi.repository import GLib
        state["app"] = app

        def win():
            return app.windows[0]

        def btn():
            return win().chrome.btn_repeat

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

        def again(fn, ms):
            GLib.timeout_add(ms, fn)
            return False

        def wait_for(desc, pred, then, left=30):
            val = None
            try:
                val = pred()
            except Exception:                          # noqa: BLE001
                val = None
            if val:
                check(desc, True, "ok" if val is True else str(val))
                return again(then, 80)
            if left <= 0:
                check(desc, False, "timeout")
                app.quit()
                return False
            return again(lambda: wait_for(desc, pred, then, left - 1), 120)

        def loops():
            s = win().core.snapshot
            return (str(s.get("loop-file")), str(s.get("loop-playlist")))

        def dimmed():
            return btn().has_css_class("reelsee-repeat-off")

        def expect(desc, mode, props, icon, dim):
            b = btn()
            ok = (win().chrome._repeat_mode == mode
                  and loops() == props
                  and win().chrome._repeat_icon.get_icon_name() == icon
                  and dimmed() == dim
                  and b.get_tooltip_text() == "Repeat: " + mode)
            check(desc, ok,
                  f"mode={win().chrome._repeat_mode} loops={loops()} "
                  f"icon={win().chrome._repeat_icon.get_icon_name()} dim={dimmed()} "
                  f"tip={b.get_tooltip_text()!r}")

        def s0():
            w = win()
            p = btn().get_parent()
            check("repeat_in_bottom_bar", p is w.chrome.bottom, str(p))
            prev_sib = btn().get_prev_sibling()
            nxt_sib = btn().get_next_sibling()
            check("beside_progress_bar",
                  prev_sib is w.chrome.dur_label
                  and nxt_sib is w.chrome.speed_btn,
                  f"{type(prev_sib).__name__}/{type(nxt_sib).__name__}")

            def labels(root):
                out = []
                if isinstance(root, Gtk.Button):
                    out.append(str(root.get_child().get_label()
                                   if isinstance(root.get_child(), Gtk.Label)
                                   else ""))
                c = root.get_first_child()
                while c is not None:
                    out.extend(labels(c))
                    c = c.get_next_sibling()
                return out

            # R8-A: no top bar exists at all any more — the whole-window
            # scan is the stronger form of the old "no loop button in the
            # top bar" check: NOTHING anywhere may be a text-labelled
            # loop/Repeat button (the repeat control is the bottom icon).
            tops = [t for t in labels(w) if "loop" in t.lower()]
            check("no_loop_button_in_top_bar", not tops, repr(tops))
            check("no_btn_loop_attr", not hasattr(w.chrome, "btn_loop"),
                  "attr gone")
            return again(s1, 100)

        def s1():
            expect("initial_off", "off", ("no", "no"),
                   "media-playlist-repeat-symbolic", True)
            # this binding exposes no a11y label GETTER — verify the
            # update_property([LABEL], ["Repeat: <mode>"]) call instead
            state["a11y_calls"] = []
            _orig = Gtk.Button.update_property

            def spy(self, props, values):
                state["a11y_calls"].append((list(props), list(values)))
                return _orig(self, props, values)

            Gtk.Button.update_property = spy
            btn().emit("clicked")                     # off -> all
            return again(lambda: wait_for(
                "mpv_all_props", lambda: loops() == ("no", "inf"),
                guard(s2), left=25), 80)

        def s2():
            want = ([Gtk.AccessibleProperty.LABEL], ["Repeat: all"])
            check("accessible_label_set", want in state["a11y_calls"],
                  repr(state["a11y_calls"]))
            expect("all_state", "all", ("no", "inf"),
                   "media-playlist-repeat-symbolic", False)
            btn().emit("clicked")                     # all -> one
            return again(lambda: wait_for(
                "mpv_one_props", lambda: loops() == ("inf", "no"),
                guard(s3), left=25), 80)

        def s3():
            expect("one_state", "one", ("inf", "no"),
                   "media-playlist-repeat-song-symbolic", False)
            btn().emit("clicked")                     # one -> off
            return again(lambda: wait_for(
                "mpv_off_props", lambda: loops() == ("no", "no"),
                guard(s4), left=25), 80)

        def s4():
            expect("back_to_off", "off", ("no", "no"),
                   "media-playlist-repeat-symbolic", True)
            # raw "l" key (unchanged) must repaint the derived icon
            w = win()
            check("key_l_handled", bool(w._on_key(None, Gdk.KEY_l, 0, 0)),
                  "True")
            return again(lambda: wait_for(
                "key_l_repaints_song",
                lambda: win().chrome._repeat_icon.get_icon_name()
                == "media-playlist-repeat-song-symbolic"
                and not dimmed(),
                guard(s5), left=25), 80)

        def s5():
            # mpv `cycle loop-file` INCREMENTS (no->1->2...), it never
            # walks back to "no" by itself — prove the raw 'l' repainted
            # (checked above), then drive loop-file back to "no" and
            # show the derived repaint dims again from the snapshot.
            w = win()
            check("l_cycles_to_positive",
                  float(w.core.snapshot.get("loop-file") or 0) > 0,
                  repr(w.core.snapshot.get("loop-file")))
            w.core.command("set", "loop-file", "no")
            return again(lambda: wait_for(
                "loop_file_no_redims",
                lambda: dimmed() and win().chrome._repeat_mode == "off",
                guard(s6), left=25), 80)

        def s6():
            # raw 'r' key unchanged: cycles loop-playlist no->1 which the
            # derivation must read as "all" (icon repeat, not dimmed).
            w = win()
            check("key_r_handled", bool(w._on_key(None, Gdk.KEY_r, 0, 0)),
                  "True")
            return again(lambda: wait_for(
                "key_r_derives_all",
                lambda: win().chrome._repeat_mode == "all" and not dimmed(),
                guard(fin), left=25), 80)

        def fin():
            check("loop_props_observed_strings",
                  all(isinstance(x, str) for x in loops()), str(loops()))
            app.quit()
            return False

        def start():
            w = win()
            wait_for("playing",
                     lambda: w.core.snapshot.get("pause") is False,
                     guard(s0), left=40)
            return False

        GLib.timeout_add(600, start)
        return False

    rc = reelsee.run(["reelsee", "testdata/perf_1080p30.mp4"],
                     on_app=schedule)
    check("app_rc", rc == 0, str(rc))
    finish()
    return 0


if __name__ == "__main__":
    sys.exit(main())
