#!/usr/bin/env python3
"""OPERATOR live proof (R7): the decode toggle on REAL hardware.

NOT part of the DSH build or the battery — run it on a real desktop
session where /dev/dri exists (the sandbox has no DRI nodes, so every
hardware claim in CI is structurally un-provable; R6 precedent).  It
drives the SHIPPED window/chrome/watchdog code with the real GL render
path and prints OPERATOR_RESULT lines.

usage (from the repo root, real desktop session):
    LD_LIBRARY_PATH=vendor/usr/lib/x86_64-linux-gnu /usr/bin/python3 \
        scripts/op_r7_decode.py <engine-id> [file1 file2]

    <engine-id>   e.g. 0000:13:00.0  (pin the iGPU: run --list-engines)

Steps proven, in order:
  1. boot with the GPU engine pinned  -> hwdec-current becomes an engaged
     mode (vaapi/vaapi-copy) AND the bottom-right control reads "GPU".
  2. click the control                -> observed flips to "no", label
     "CPU", play POSITION PRESERVED (time keeps running forward — no
     reload to 0, no pause), toast shown.
  3. click again                      -> GPU re-engages, label "GPU".
  4. next file (real playlist "n")    -> GPU engaged again WITHOUT any
     click (the override is memory-only and died with the previous file).
  5. config.json still pins the engine (the toggle wrote NOTHING) and a
     relaunch (the script re-execs itself, same config dir) attempts GPU
     and reaches it again.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

ENGINE = sys.argv[1] if len(sys.argv) > 1 else "0000:13:00.0"
FILES = sys.argv[2:4] or ["testdata/perf_1080p30.mp4",
                          "testdata/tone_5s.mkv"]
PHASE = os.environ.get("REELSEE_R7_OP_PHASE", "1")

BASE = Path(tempfile.gettempdir()) / "reelsee-op-r7"
BASE.mkdir(parents=True, exist_ok=True)
if PHASE == "1":
    subprocess.run(["rm", "-rf", str(BASE)], check=False)
    BASE.mkdir(parents=True, exist_ok=True)
os.environ["REELSEE_CONFIG_DIR"] = str(BASE / "cfg")
os.environ["REELSEE_STATE_DIR"] = str(BASE / "state")

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, GLib  # noqa: E402

import watchdog as WD  # noqa: E402  (shipped classifier — same source)
from smoke_harness import check, failures, finish  # noqa: E402

import reelsee  # noqa: E402

state: dict = {}


def R(name, ok, detail=""):
    print(f"OPERATOR_RESULT: {name}={'PASS' if ok else 'FAIL'} {detail}",
          flush=True)
    check(name, ok, str(detail))


def base_schedule(app):
    cfg_path = BASE / "cfg" / "config.json"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    import json
    seed = {"engine": {"decoder": ENGINE}}
    if PHASE == "2":            # relaunch: config as the operator left it
        try:
            with open(cfg_path) as fh:
                seed = json.load(fh)
        except OSError:
            pass
    else:
        with open(cfg_path, "w") as fh:
            json.dump(seed, fh)

    def win():
        return app.windows[0]

    def again(fn, ms):
        GLib.timeout_add(ms, fn)
        return False

    def wait_for(desc, pred, then, left=200):
        try:
            val = pred()
        except Exception:                           # noqa: BLE001
            val = None
        if val:
            return again(then, 60)
        if left <= 0:
            R(desc, False, "TIMEOUT")
            app.quit()
            return False
        return again(lambda: wait_for(desc, pred, then, left - 1), 150)

    def observed():
        return win().core.snapshot.get("hwdec-current")

    def gpu_observed():
        return (observed() is not None
                and WD.mode_base(observed()) in WD.HW_REAL_ON)

    def label():
        return win().chrome.btn_decode.get_label()

    def pos():
        return win().core.snapshot.get("time-pos")

    def fname():
        return win().core.snapshot.get("filename")

    def toggle():
        # activate() == the real clicked handler (same path a pointer
        # click routes through); used because this proof may run under
        # Wayland where XTEST is unavailable.
        win().chrome.btn_decode.activate()

    # ------------------------------------------------------------ steps
    def step1_gpu():
        R("boot_engine_pinned",
          win().cfg.get("engine.decoder") == ENGINE,
          str(win().cfg.get("engine.decoder")))
        return again(lambda: wait_for(
            "gpu_engages_on_boot", gpu_observed, step1_label), 200)

    def step1_label():
        R("gpu_engages_on_boot", True, str(observed()))
        R("label_gpu", label() == "GPU", label())
        if PHASE == "2":                     # relaunch proof is complete
            R("relaunch_attempts_gpu", True,
              f"observed={observed()} (override never persisted)")
            app.quit()
            return False
        state["cfg_bytes_phase1"] = open(cfg_path, "rb").read() \
            if cfg_path.exists() else None
        return again(step2, 400)

    def step2():
        p0, pause0 = pos(), win().core.snapshot.get("pause")
        state["before"] = (p0, pause0)
        toggle()
        return again(lambda: wait_for(
            "click_forces_cpu", lambda: observed() == "no", step2_checks),
            200)

    def step2_checks():
        R("click_forces_cpu", True, "hwdec-current=no")
        R("label_cpu", label() == "CPU", label())
        return again(lambda: wait_for(
            "cpu_playing_still",
            lambda: (win().core.snapshot.get("pause") is False
                     and (pos() or 0) > 0), step2_position), 100)

    def step2_position():
        p1, pause1 = pos(), win().core.snapshot.get("pause")
        p0, pause0 = state["before"]
        ok = (p1 is not None and p0 is not None and p1 > p0
              and pause1 is False)
        R("position_preserved_on_cpu", ok, f"{p0} -> {p1} pause={pause1}")
        R("watchdog_suspended_under_override",
          win().watchdog.suspended is True, str(win().watchdog.suspended))
        return again(step3, 400)

    def step3():
        toggle()
        return again(lambda: wait_for(
            "click_returns_to_gpu", gpu_observed, step3_checks), 200)

    def step3_checks():
        R("click_returns_to_gpu", True, str(observed()))
        R("label_gpu_again", label() == "GPU", label())
        R("override_never_written_to_config",
          open(cfg_path, "rb").read() == state["cfg_bytes_phase1"],
          "config.json bytes identical")
        return again(step4, 500)

    def step4():
        if len(FILES) < 2:
            R("second_file_retries_gpu", False, "give TWO files")
            app.quit()
            return False
        w = win()
        state["file_before"] = fname()
        w._on_key(None, Gdk.KEY_n, 0, 0)          # real playlist next
        return again(lambda: wait_for(
            "second_file_retries_gpu",
            lambda: (fname() != state["file_before"] and gpu_observed()),
            step4_checks), 200)

    def step4_checks():
        R("second_file_retries_gpu", True,
          f"observed={observed()} label={label()} (no click, override "
          "died with the previous file)")
        R("label_gpu_second_file", label() == "GPU", label())
        R("watchdog_rearmed_on_new_file",
          win().watchdog.suspended is False, str(win().watchdog.suspended))
        return again(step5, 400)

    def step5():
        app.quit()
        return False

    def boot(left=100):
        try:
            w = app.windows[0]
        except IndexError:
            w = None
        if w is None or not w.get_realized():
            return again(lambda: boot(left - 1), 200)
        return again(step1_gpu, 300)

    GLib.timeout_add(400, boot)


def main() -> int:
    rc = reelsee.run(["reelsee"] + FILES, on_app=base_schedule)
    check("app_rc", rc == 0, str(rc))
    if PHASE == "1" and not failures:             # only relaunch on green
        env = dict(os.environ, REELSEE_R7_OP_PHASE="2")
        out = subprocess.run([sys.executable, os.path.abspath(__file__)]
                             + FILES, env=env, timeout=180)
        check("relaunch_phase_rc", out.returncode == 0,
              str(out.returncode))
    finish()
    return 0


if __name__ == "__main__":
    sys.exit(main())
