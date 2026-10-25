#!/usr/bin/python3
"""R5-E gate smoke: pressing A/B while a loop is ACTIVE FIRST clears the
whole section — marks, drawn markers, persisted entry, mpv ab-loop-a/b
(they return to "no") — and only then applies the pressed mark.  B right
after a clear yields "Set A first" with NO partial state.  Exactly ONE
toast per press.  Runs under scripts/headless-smoke.sh."""
import os
import sys

from smoke_harness import SCRATCH, check, finish

CFG_DIR = SCRATCH / "abclear-cfg"
STATE_DIR = SCRATCH / "abclear-state"
for _d in (CFG_DIR, STATE_DIR):
    _d.mkdir(parents=True, exist_ok=True)
os.environ["REELSEE_CONFIG_DIR"] = str(CFG_DIR)
os.environ["REELSEE_STATE_DIR"] = str(STATE_DIR)
for _f in (CFG_DIR / "config.json", STATE_DIR / "ab.json",
           STATE_DIR / "resume.json"):
    if _f.exists():
        _f.unlink()

import reelsee  # noqa: E402

state: dict = {}


def main() -> int:
    def schedule(app):
        from gi.repository import GLib
        state["app"] = app

        def w():
            return app.windows[0]

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

        def props():
            s = w().core.snapshot
            return (str(s.get("ab-loop-a")), str(s.get("ab-loop-b")))

        def press(which):
            """A real keypress dispatch (NOT a hook poke) via the keymap:
            '[' → set-a, ']' → set-b (mark = current position)."""
            from gi.repository import Gdk
            kv = (Gdk.KEY_bracketleft if which == "a"
                  else Gdk.KEY_bracketright)
            state["toasts"] = []      # toasts fire synchronously
            return w()._on_key(None, kv, 0, 0)

        def s_setup():
            # capture toasts for the single-toast gate
            ch = w().chrome
            _orig = ch.show_osd

            def spy(msg):
                state.setdefault("toasts", []).append(str(msg))
                return _orig(msg)

            ch.show_osd = spy
            # build an ACTIVE loop the ordinary way
            state["toasts"] = []
            w()._ab_hook("set_a", 2.0)
            w()._ab_hook("set_b", 6.0)
            return again(lambda: wait_for(
                "loop_active_props",
                lambda: props()[0].startswith("2")
                and props()[1].startswith("6"),
                guard(s_active), left=25), 100)

        def s_active():
            st = w().ab_state
            check("loop_is_active", st.active and (st.a, st.b) == (2.0, 6.0),
                  f"a={st.a} b={st.b}")
            check("ab_props_observed_strings",
                  all(isinstance(x, str) for x in props()), str(props()))
            # scenario (i): active -> A => clear everything, then ONE mark
            check("key_a_dispatched", bool(press("a")), "True")
            return again(guard(s_after_a), 500)

        def s_after_a():
            st = w().ab_state
            marks = [x for x in (st.a, st.b) if x is not None]
            check("active_A_one_mark_inactive",
                  len(marks) == 1 and st.a is not None
                  and st.b is None and not st.active,
                  f"marks={marks}")
            check("active_A_b_prop_no",
                  props()[1] == "no", str(props()))
            check("active_A_a_prop_is_new",
                  props()[0] != "no" and abs(
                      float(props()[0]) - st.a) < 0.01,
                  f"prop={props()[0]} a={st.a}")
            check("active_A_markers",
                  w().chrome.seek.ab_a == st.a
                  and w().chrome.seek.ab_b is None,
                  f"{w().chrome.seek.ab_a},{w().chrome.seek.ab_b}")
            toasts = state["toasts"]
            check("active_A_single_toast", len(toasts) == 1
                  and "cleared" in toasts[0], repr(toasts))
            check("active_A_store_cleared_or_replaced",
                  w().app.ab_store.get(str(w().target)) in (None,),
                  "no stale entry")
            # scenario (iii) first half done; now B completes a FRESH loop
            state["toasts"] = []
            w()._ab_hook("set_b", st.a + 3.0)
            return again(lambda: wait_for(
                "fresh_loop_props",
                lambda: abs(float(props()[1]) - (st.a + 3.0)) < 0.01,
                guard(s_fresh), left=25), 100)

        def s_fresh():
            st = w().ab_state
            check("fresh_loop", st.active and abs(st.b - (st.a + 3.0)) < 0.01,
                  f"a={st.a} b={st.b}")
            # scenario (ii): ACTIVE -> B => clear all, "Set A first"
            state["toasts"] = []
            check("key_b_dispatched", bool(press("b")), "True")
            return again(guard(s_after_b), 500)

        def s_after_b():
            st = w().ab_state
            check("active_B_no_marks",
                  st.a is None and st.b is None and not st.active,
                  f"a={st.a} b={st.b}")
            check("active_B_props_all_no", props() == ("no", "no"),
                  str(props()))
            check("active_B_markers_gone",
                  w().chrome.seek.ab_a is None
                  and w().chrome.seek.ab_b is None, "ok")
            toasts = state["toasts"]
            check("active_B_single_toast",
                  len(toasts) == 1 and "Set A first" in toasts[0]
                  and "cleared" in toasts[0], repr(toasts))
            check("active_B_store_gone",
                  w().app.ab_store.get(str(w().target)) in (None,),
                  "no entry")
            # scenario (iv): OFF -> A -> B unchanged (no clear semantics)
            state["toasts"] = []
            w()._ab_hook("set_a", 1.0)
            w()._ab_hook("set_b", 4.0)
            return again(lambda: wait_for(
                "off_pair_props",
                lambda: props()[0].startswith("1")
                and props()[1].startswith("4"),
                guard(s_final), left=25), 100)

        def s_final():
            st = w().ab_state
            toasts = state["toasts"]
            check("off_pair_plain", st.active and (st.a, st.b) == (1.0, 4.0),
                  f"a={st.a} b={st.b}")
            check("off_pair_no_clear_word",
                  all("cleared —" not in t for t in toasts), repr(toasts))
            check("ab_props_observed_strings2",
                  all(isinstance(x, str) for x in props()), str(props()))
            app.quit()
            return False

        def start():
            win = w()
            wait_for("playing",
                     lambda: win.core.snapshot.get("pause") is False,
                     guard(s_setup), left=40)
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
