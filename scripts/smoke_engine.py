#!/usr/bin/python3
"""P6 gate smoke: engine enumeration + selector UI + CPU-decode proof.

Runs the REAL app with engine.decoder pre-set to "cpu", proves the live
observed `hwdec-current` is exactly "no", that enumeration yields >= 4
engines on this box (Auto + 3 GPUs + CPU), that the Decode & GPU page now
carries a live GPU selector whose list matches enumeration, and that the
R10 readout renders from the Snapshot.  Under scripts/headless-smoke.sh.
"""
import os
import sys

from smoke_harness import SCRATCH, check, finish

CFG_DIR = os.path.join(SCRATCH, "eng-cfg")
STATE_DIR = os.path.join(SCRATCH, "eng-state")
for _d in (CFG_DIR, STATE_DIR):
    os.makedirs(_d, exist_ok=True)
os.environ["REELSEE_CONFIG_DIR"] = CFG_DIR
os.environ["REELSEE_STATE_DIR"] = STATE_DIR
_CFG = os.path.join(CFG_DIR, "config.json")
if os.path.exists(_CFG):
    os.unlink(_CFG)
with open(_CFG, "w") as _fh:                      # CPU engine from launch
    _fh.write('{"engine": {"decoder": "cpu"}}\n')

from gi.repository import GLib  # noqa: E402

import reelsee  # noqa: E402

from pathlib import Path  # noqa: E402

CLIP = "testdata/perf_1080p30.mp4"      # 10 s, no EOF race


def probe_schedule(app):
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

    def wait_for(desc, pred, then, left=30):
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
        import engine
        engines = engine.enumerate_engines()
        check("engines", len(engines) >= 4, str(len(engines)))
        ids = [e.id for e in engines]
        check("engines_auto_first", ids[0] == "auto", ids[0])
        check("engines_cpu_last", ids[-1] == "cpu", str(ids))
        cpu = engines[-1]
        check("engine_cpu_hwdec_opts",
              engine.engine_hwdec_mode(cpu) == "no",
              engine.engine_hwdec_mode(cpu))
        nodes = [e.render_node for e in engines if e.render_node]
        check("engine_nodes_absolute",
              all(n.startswith("/") for n in nodes), str(nodes))
        disp = engine.display_gpu_slot()
        check("engine_display_gpu", disp is not None, str(disp))
        w = app.windows[0]
        wait_for("file_loaded",
                 lambda: w.core.snapshot.get("duration") is not None,
                 guard(s1), left=60)
        return False

    def s1():
        w = app.windows[0]
        wait_for("hwdec_no_seen",
                 lambda: w.core.snapshot.get("hwdec-current") == "no",
                 guard(s2), left=40)
        return False

    def s2():
        hw = app.windows[0].core.snapshot.get("hwdec-current")
        check("engine_cpu_hwdec", hw == "no", str(hw))
        st = app.show_settings()
        row = st.widgets["engine.decoder"]
        model = row.get_model()
        labels = [model.get_string(i)
                  for i in range(model.get_n_items())]
        check("engine_selector_items", len(labels) >= 4, str(len(labels)))
        check("engine_selector_has_cpu",
              any("CPU" in s for s in labels), "ok")
        check("engine_selector_has_auto",
              any(s.startswith("Auto") for s in labels), "ok")
        sel = row.get_selected()
        cur = "" if sel < 0 else model.get_string(sel)
        check("engine_selector_shows_cpu", cur.startswith("CPU"), cur)
        rrow = st.widgets["engine.renderer_override"]
        check("renderer_selector_items",
              rrow.get_model().get_n_items() >= 2,
              str(rrow.get_model().get_n_items()))
        readout = st._readout_text()
        check("engine_readout", "hwdec-current:" in readout
              and "Renderer:" in readout and "Decoder:" in readout,
              readout)
        check("engine_readout_says_no", "hwdec-current: no" in readout,
              readout)
        st.win.close()
        GLib.timeout_add(200, guard(s3))
        return False

    def s3():
        app.quit()
        return False

    GLib.timeout_add(900, guard(s0))
    return False


def main() -> int:
    rc = reelsee.run(["reelsee", CLIP], on_app=probe_schedule)
    check("app_rc", rc == 0, str(rc))
    finish()
    return 0


if __name__ == "__main__":
    sys.exit(main())
