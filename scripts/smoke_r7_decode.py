#!/usr/bin/python3
"""R7-C gate smoke: the decode indicator/toggle under real X11 input.

Runs with a FRESH config dir pre-seeded {"engine": {"decoder": "cpu"}} —
the forced-CPU engine.  Headless mpv starts at hwdec=no, so the OBSERVED
hwdec-current is "no" and the rightmost bottom-bar control must paint
"CPU" from that observation.  Gates (brief §C):

  * bottom bar order is exact and the decode control sits AFTER mute
    (last widget on the bar);
  * label is "CPU" while observed hwdec-current == "no" (and the dim CPU
    style is on);
  * the tooltip NAMES the observed value (literal "hwdec-current: no");
  * a real XTEST click runs through the proven apply mechanism (wrapped
    core.apply_hwdec recorder sees "no" — the configured engine mode)
    and writes NOTHING to config: config.json bytes AND mtime_ns are
    identical after the click (memory-only override contract).
"""
import json
import os
import sys
import time

from smoke_harness import SCRATCH, check, finish

CFG_DIR = SCRATCH / "r7decode-cfg"
STATE_DIR = SCRATCH / "r7decode-state"
for _d in (CFG_DIR, STATE_DIR):
    _d.mkdir(parents=True, exist_ok=True)
CFG_FILE = CFG_DIR / "config.json"
for _f in (CFG_FILE, STATE_DIR / "resume.json"):
    if _f.exists():
        _f.unlink()
# forced-CPU engine, fresh (restart semantics of the file are not part of
# this gate — the toggle must never touch it)
with open(CFG_FILE, "w") as fh:
    json.dump({"engine": {"decoder": "cpu"}}, fh)
os.environ["REELSEE_CONFIG_DIR"] = str(CFG_DIR)
os.environ["REELSEE_STATE_DIR"] = str(STATE_DIR)

import reelsee  # noqa: E402
from chrome import BOTTOM_ORDER  # noqa: E402

FILES = ["testdata/perf_1080p30.mp4"]
state: dict = {}


def base_schedule(app):
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

    # ------------------------------------------------------ X plumbing
    def xopen():
        from Xlib import display as xdisplay
        state["xd"] = xdisplay.Display(os.environ.get("DISPLAY", ":0"))

    def xfind():
        from Xlib import X
        d = state["xd"]
        root = d.screen().root
        found = [None]
        big = [None, 0]

        def name_of(w):
            try:
                n = w.get_wm_name()
                return n.decode("utf-8", "replace") if isinstance(
                    n, bytes) else (n or None)
            except Exception:                       # noqa: BLE001
                return None

        def mapped(w):
            try:
                return w.get_attributes().map_state == X.IsViewable
            except Exception:                       # noqa: BLE001
                return False

        def walk(w, depth=0):
            if found[0] is not None or depth > 6:
                return
            nm = name_of(w)
            if nm and "REELSEE" in nm and mapped(w) and w != root:
                found[0] = w
                return
            if mapped(w) and w != root and depth <= 2:
                try:
                    g = w.get_geometry()
                    area = g.width * g.height
                except Exception:                   # noqa: BLE001
                    area = 0
                if g.width >= 800 and g.height >= 400 and (
                        big[0] is None or area > big[1]):
                    big[0], big[1] = w, area
            try:
                kids = w.query_tree().children
            except Exception:                       # noqa: BLE001
                kids = []
            for k in kids:
                walk(k, depth + 1)
        walk(root)
        return found[0] or big[0]

    def fake_button(kind, x, y, button=1):
        from Xlib import X
        from Xlib.ext import xtest
        d = state["xd"]
        d.screen().root.warp_pointer(x, y)
        d.sync()
        et = X.ButtonPress if kind == "press" else X.ButtonRelease
        xtest.fake_input(d, et, button)
        d.sync()

    def click(x, y, button=1, then=None, gap=60):
        def release():
            fake_button("release", x, y, button)
            return again(then, 500)
        fake_button("press", x, y, button)
        return again(release, gap)

    def children(box):
        out, c = [], box.get_first_child()
        while c is not None:
            out.append(c)
            c = c.get_next_sibling()
        return out

    def click_widget(name, then):
        w = win()
        b = getattr(w.chrome, name)
        ok, rect = b.compute_bounds(w)
        assert ok, "button has no bounds in window coords"
        x = int(rect.origin.x + rect.size.width / 2)
        y = int(rect.origin.y + rect.size.height / 2)
        return click(x, y, 1, then)

    def observed():
        return win().core.snapshot.get("hwdec-current")

    def label():
        return win().chrome.btn_decode.get_label()

    # ------------------------------------------------------ assertions
    def s_structural():
        w = win()
        names = [c.get_name() for c in children(w.chrome.bottom)]
        check("bottom_order_exact", names == list(BOTTOM_ORDER), str(names))
        check("decode_after_mute",
              names.index("decode") == len(names) - 1
              and names.index("mute") == len(names) - 2
              and names.index("decode") > names.index("mute"), str(names))
        # BEFORE any frame is observed the control must sit idle ("…"),
        # NEVER a confident claim:
        if observed() is None:
            check("idle_marker_before_first_frame",
                  label() in ("…", "..."), label())
        # wrap the PROVEN mechanism on the live core: the click must run
        # through it (recorder only — no behaviour change)
        core = w.core
        state["hwdec_calls"] = []
        real = core.apply_hwdec

        def spy(mode, *a, **kw):
            state["hwdec_calls"].append(str(mode))
            return real(mode, *a, **kw)
        core.apply_hwdec = spy
        # forced-CPU engine: observed "no" must paint "CPU" (never "GPU"
        # from the CONFIG — and never idle once the value is observed)
        return again(guard(lambda: wait_for(
            "cpu_label_under_forced_cpu_engine",
            lambda: observed() == "no" and label() == "CPU",
            guard(s_tooltip))), 100)

    def s_tooltip():
        w = win()
        tip = str(w.chrome.btn_decode.get_tooltip_text() or "")
        check("tooltip_names_observed", "hwdec-current: no" in tip, tip[:90])
        check("cpu_dim_style",
              w.chrome.btn_decode.has_css_class("reelsee-decode-cpu"),
              "class present")
        # snapshot the config EXACTLY (bytes + mtime) before the click:
        with open(CFG_FILE, "rb") as fh:
            state["cfg_bytes"] = fh.read()
        state["cfg_mtime"] = os.stat(CFG_FILE).st_mtime_ns
        return click_widget("btn_decode", guard(s_click_landed))

    def s_click_landed():
        return again(guard(lambda: wait_for(
            "click_ran_apply_mechanism",
            lambda: len(state["hwdec_calls"]) >= 1,
            guard(s_quiet_window))), 40)

    def s_quiet_window():
        # let the debounced (250 ms) config-flush window burn by fully,
        # so ANY stray write the click caused has hit the disk already
        state["settle_at"] = time.time() + 1.2
        return again(guard(lambda: wait_for(
            "config_settle_window",
            lambda: time.time() >= state["settle_at"],
            guard(s_checks_after_click))), 40)

    def s_checks_after_click():
        with open(CFG_FILE, "rb") as fh:
            now_bytes = fh.read()
        now_mt = os.stat(CFG_FILE).st_mtime_ns
        w = win()
        check("click_uses_apply_hwdec_no",
              state["hwdec_calls"][0] == "no",
              ",".join(state["hwdec_calls"]))
        check("click_writes_no_config",
              now_bytes == state["cfg_bytes"]
              and now_mt == state["cfg_mtime"],
              f"bytes={len(now_bytes)} mtime_delta={now_mt - state['cfg_mtime']}")
        check("engine_decoder_untouched",
              w.cfg.get("engine.decoder") == "cpu",
              str(w.cfg.get("engine.decoder")))
        check("label_still_tracks_observation",
              label() == "CPU", label())
        # it is a TOGGLE, not a one-shot: a second real click runs
        # through the same mechanism again (still zero config writes)
        return click_widget("btn_decode", guard(s_second_click))

    def s_second_click():
        return again(guard(lambda: wait_for(
            "second_click_reapplies",
            lambda: len(state["hwdec_calls"]) >= 2,
            guard(s_final_check))), 60)

    def s_final_check():
        with open(CFG_FILE, "rb") as fh:
            now_bytes = fh.read()
        check("still_no_config_write",
              now_bytes == state["cfg_bytes"]
              and os.stat(CFG_FILE).st_mtime_ns == state["cfg_mtime"],
              "identical")
        w = win()
        check("watchdog_unsuspended_after_reattempt",
              w.watchdog.suspended is False, str(w.watchdog.suspended))
        check("window_alive", w.get_realized(), "True")
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
        w.present()
        state["xd"].sync()
        return again(guard(s_structural), 600)

    GLib.timeout_add(300, guard(start))


def main() -> int:
    rc = reelsee.run(["reelsee"] + FILES[:1], on_app=base_schedule)
    check("rc", rc == 0, str(rc))
    finish()
    return 0


if __name__ == "__main__":
    sys.exit(main())
