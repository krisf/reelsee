#!/usr/bin/python3
"""R15-B gate: repeat defaults ON, migrated exactly once, never again.

Parent (this process) runs the CHEAP in-process units first — schema
default, mpv init-option seeding, and the migration function itself
(flip once / idempotence / user-off respected / corrupt-and-absent files
untouched / foreign keys preserved).  Then three REAL boots:

Phase "fresh"    — an empty config dir (fresh install): the config value
                   is "inf", the `win.repeat` action state is "one"
                   (the brief's "inf" — the tri-state names it "one";
                   loop-file="inf" ⇔ mode "one"), the bottom-bar control
                   renders ACTIVE (song glyph, no dim class), the FIRST
                   observed mpv loop-file after the first load is "inf",
                   the mode survives a cross-play replace-load (Round 9
                   contract under the new default), and one click takes
                   it to "off" and PERSISTS "no".
Phase "afteroff" — fresh process, SAME dirs: the user's stored "off"
                   survives restart (no second migration): born dimmed,
                   loop-file "no", disk still "no" after a settle, flag
                   still true.
Phase "legacy"   — a pre-seeded legacy config carrying a STORED "no"
                   (and foreign keys): boot migrates it to "inf" exactly
                   once, writes the flag, rewrites NOTHING else, the app
                   is born repeating (first observed value "inf").

No XTEST here: every interaction is a widget `emit("clicked")`, so the
gate is pure state.  Three fresh scratch dirs per invocation, purged by
the harness wrapper; nothing is shared with other gates.
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))
from smoke_harness import SCRATCH, check, finish  # noqa: E402

BASE = Path(os.environ.get("REELSEE_R15REP_BASE")
            or SCRATCH / f"r15rep-{os.getpid()}")
FRESH_CFG = BASE / "fresh-cfg"
FRESH_STATE = BASE / "fresh-state"
LEG_CFG = BASE / "legacy-cfg"
LEG_STATE = BASE / "legacy-state"

PHASE = os.environ.get("REELSEE_R15REP_PHASE", "")
CFG_FILE = (Path(os.environ.get("REELSEE_CONFIG_DIR") or FRESH_CFG)
            / "config.json")
SONG = "media-playlist-repeat-song-symbolic"
LOOP = "media-playlist-repeat-symbolic"
CLIP1 = "testdata/perf_1080p30.mp4"
CLIP2 = "testdata/smoke_360p.mp4"
state: dict = {}

LEGACY_SEED = {
    "version": 1,
    "playback": {"speed": 1.25, "loop_file": "no", "loop_playlist": "no"},
    "ui": {"chrome_timeout_ms": 4000},
    "zeta_custom": {"keep": "me"},
}


# ------------------------------------------------------------- unit phase --
def units() -> None:
    """Config-level checks — no display, no mpv, deterministic."""
    import config
    import engine

    pb = config.DEFAULTS["playback"]
    check("u_default_loop_file_inf", pb.get("loop_file") == "inf",
          str(pb.get("loop_file")))
    check("u_default_loop_playlist_no", pb.get("loop_playlist") == "no",
          str(pb.get("loop_playlist")))

    tmp = BASE / "unit"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    cfg = config.Config(tmp / "config.json")
    check("u_fresh_cfg_data_inf", cfg.get("playback.loop_file") == "inf",
          str(cfg.get("playback.loop_file")))
    opts = engine.mpv_init_options(cfg, headless=True)
    check("u_init_opts_seed_loop_file", opts.get("loop-file") == "inf",
          str(opts.get("loop-file")))
    check("u_init_opts_playlist_untouched",
          opts.get("loop-playlist") in (None, "no"),
          str(opts.get("loop-playlist")))

    mig = getattr(config, "run_migrations", None)
    check("u_run_migrations_exists", callable(mig), repr(mig))
    if not callable(mig):
        return
    path = tmp / "legacy.json"

    def write(data):
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def read():
        return json.loads(path.read_text(encoding="utf-8"))

    # legacy "no" flips once, foreign keys survive
    write(json.loads(json.dumps(LEGACY_SEED)))
    mig(path)
    got = read()
    check("u_mig_flips_no",
          got.get("playback", {}).get("loop_file") == "inf",
          str(got.get("playback", {}).get("loop_file")))
    check("u_mig_flag_written",
          got.get("migrations", {}).get("repeat_default_on") is True,
          str(got.get("migrations")))
    check("u_mig_foreign_speed",
          got.get("playback", {}).get("speed") == 1.25, "speed clobbered")
    check("u_mig_foreign_section",
          got.get("zeta_custom") == {"keep": "me"}, "zeta clobbered")
    check("u_mig_ui_untouched",
          got.get("ui", {}).get("chrome_timeout_ms") == 4000, "ui moved")
    check("u_mig_no_new_sections",
          set(got) - {"version", "playback", "ui", "zeta_custom",
                      "migrations"} == set(), str(sorted(got)))

    # idempotent: a second run rewrites NOTHING (byte-identical file)
    before = path.read_bytes()
    mig(path)
    check("u_mig_idempotent", path.read_bytes() == before, "file changed")

    # user chose OFF after the migration: flag present, "no" survives
    write({"version": 1,
           "migrations": {"repeat_default_on": True},
           "playback": {"loop_file": "no"}})
    before = path.read_bytes()
    mig(path)
    check("u_mig_respects_user_off", path.read_bytes() == before,
          "user's off was rewritten")

    # already-inf stored: value untouched, flag added
    write({"version": 1, "playback": {"loop_file": "inf"}})
    mig(path)
    got = read()
    check("u_mig_inf_untouched",
          got.get("playback", {}).get("loop_file") == "inf", "value moved")
    check("u_mig_inf_flag",
          got.get("migrations", {}).get("repeat_default_on") is True,
          str(got.get("migrations")))

    # numeric / unknown stored values are NOT touched (only literal "no")
    write({"version": 1, "playback": {"loop_file": "3"}})
    before = path.read_bytes()
    mig(path)
    got = read()
    check("u_mig_numeric_untouched",
          got.get("playback", {}).get("loop_file") == "3", "value moved")

    # fresh install (no file at all): migration must NOT create one
    gone = tmp / "none.json"
    if gone.exists():
        gone.unlink()
    mig(gone)
    check("u_mig_no_file_no_write", not gone.exists(), "file created")

    # corrupt file: left alone, never recreated
    bad = tmp / "bad.json"
    bad.write_text("{ not json", encoding="utf-8")
    before = bad.read_bytes()
    mig(bad)
    check("u_mig_corrupt_untouched", bad.read_bytes() == before,
          "corrupt file rewritten")

    # schema: the flag is not a settings leaf (no phantom settings row)
    leaves = config.schema_leaf_keys()
    check("u_flag_not_a_leaf", not any("migrations" in k for k in leaves),
          str([k for k in leaves if "migrations" in k]))


# ------------------------------------------------------------- gui phases --
def schedule(app):
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

    def again(fn, ms=80):
        GLib.timeout_add(ms, fn)
        return False

    def wait_for(desc, pred, then, left=40):
        try:
            val = pred()
        except Exception:                           # noqa: BLE001
            val = None
        if val:
            check(desc, True, "ok" if val is True else str(val))
            return again(then)
        if left <= 0:
            check(desc, False, "timeout")
            app.quit()
            return False
        return again(lambda: wait_for(desc, pred, then, left - 1), 120)

    def snap():
        return win().core.snapshot

    def loops2():
        s = snap()
        return (str(s.get("loop-file")), str(s.get("loop-playlist")))

    def disk_raw():
        try:
            return json.loads(CFG_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def mode():
        return win()._ctx_repeat.get_state().unpack()

    def active_ui():
        w = win()
        return (w.chrome._repeat_icon.get_icon_name() == SONG
                and not w.chrome.btn_repeat.has_css_class(
                    "reelsee-repeat-off"))

    def dim_ui():
        w = win()
        return (w.chrome._repeat_icon.get_icon_name() == LOOP
                and w.chrome.btn_repeat.has_css_class(
                    "reelsee-repeat-off"))

    # ------------------------------------------------------------ fresh --
    def f_start():
        w = win()
        check("f_cfg_default_inf",
              str(w.cfg.get("playback.loop_file")) == "inf",
              str(w.cfg.get("playback.loop_file")))
        check("f_cfg_playlist_no",
              str(w.cfg.get("playback.loop_playlist")) == "no",
              str(w.cfg.get("playback.loop_playlist")))
        check("f_mapping_one_is_inf",
              __import__("chrome").repeat_mode("inf", "no") == "one",
              "loop_file=inf must derive mode one")
        check("f_action_one", mode() == "one", mode())
        check("f_control_active", active_ui(), "icon/dim wrong at boot")
        check("f_tooltip",
              w.chrome.btn_repeat.get_tooltip_text() == "Repeat: one",
              str(w.chrome.btn_repeat.get_tooltip_text()))
        w._load_path(CLIP1)
        return again(guard(f_loaded))

    def f_loaded():
        return wait_for("f_playing", lambda: snap().get("pause") is False,
                        guard(f_pushed), left=80)

    def f_pushed():
        # FIRST load pushes loop-file=inf — observed, not assumed
        return wait_for("f_first_load_loopfile_inf",
                        lambda: loops2()[0] == "inf",
                        guard(f_crossplay), left=40)

    def f_crossplay():
        # Round 9 contract under the new default: cross-play keeps the
        # mode (the pre-load push re-applies it, nothing resets to off)
        win()._load_path(CLIP2)
        return again(guard(f_crossplay2))

    def f_crossplay2():
        return wait_for("f_crossplay_loopfile_inf",
                        lambda: loops2() == ("inf", "no")
                        and snap().get("pause") is False,
                        guard(f_toggle_off), left=60)

    def f_toggle_off():
        check("f_mode_still_one", mode() == "one", mode())
        win().chrome.btn_repeat.emit("clicked")     # one -> off
        return wait_for("f_off_ui", dim_ui, guard(f_off_disk))

    def f_off_disk():
        def no_on_disk():
            raw = disk_raw() or {}
            return (raw.get("playback", {}) or {}).get(
                "loop_file") == "no"
        return wait_for("f_off_persisted_no", no_on_disk,
                        guard(f_done), left=40)

    def f_done():
        check("f_flag_on_disk",
              (disk_raw() or {}).get("migrations", {}).get(
                  "repeat_default_on") is True,
              str((disk_raw() or {}).get("migrations")))
        check("f_off_loop_props", loops2() == ("no", "no"), str(loops2()))
        app.quit()
        return False

    # ---------------------------------------------------------- afteroff --
    def a_start():
        # the file the fresh phase LEFT BEHIND is the input; if the
        # chain broke we still run the checks against an equivalent
        # hand-seeded user-off file, but the chain failure is recorded.
        raw = disk_raw() or {}
        chained = (raw.get("playback", {}) or {}).get("loop_file") == "no"
        check("a_chain_user_off_on_disk", chained, str(raw)[:200])
        if not chained:
            CFG_FILE.write_text(json.dumps({
                "version": 1,
                "migrations": {"repeat_default_on": True},
                "playback": {"loop_file": "no", "loop_playlist": "no"},
            }), encoding="utf-8")
        check("a_flag_present_pre",
              (disk_raw() or {}).get("migrations", {}).get(
                  "repeat_default_on") is True,
              str((disk_raw() or {}).get("migrations")))
        return again(guard(a_born))

    def a_born():
        w = win()
        check("a_cfg_keeps_no", str(w.cfg.get("playback.loop_file"))
              == "no", str(w.cfg.get("playback.loop_file")))
        w._load_path(CLIP1)
        return again(guard(a_loaded))

    def a_loaded():
        return wait_for("a_playing_no_loop", lambda: (
            snap().get("pause") is False
            and loops2() == ("no", "no")), guard(a_ui), left=80)

    def a_ui():
        check("a_mode_off", mode() == "off", mode())
        check("a_control_dim", dim_ui(), "control not dimmed")
        return again(guard(a_settle))

    def a_settle():
        # give any (wrongly re-firing) migration a chance, then assert
        # the stored "no" is STILL there, untouched
        return wait_for("a_no_second_migration", lambda: (
            (disk_raw() or {}).get("playback", {}) or {}).get(
                "loop_file") == "no", guard(a_done), left=20)

    def a_done():
        raw = disk_raw() or {}
        check("a_flag_still_true", raw.get("migrations", {}).get(
            "repeat_default_on") is True, str(raw.get("migrations")))
        app.quit()
        return False

    # ------------------------------------------------------------- legacy --
    def l_start():
        raw = disk_raw() or {}
        check("l_pre_migrated", raw.get("playback", {}).get(
            "loop_file") == "inf", str(raw.get("playback")))
        check("l_flag_written", raw.get("migrations", {}).get(
            "repeat_default_on") is True, str(raw.get("migrations")))
        check("l_speed_kept", raw.get("playback", {}).get("speed")
              == 1.25, str(raw.get("playback")))
        check("l_ui_kept", raw.get("ui", {}).get("chrome_timeout_ms")
              == 4000, str(raw.get("ui")))
        check("l_foreign_kept", raw.get("zeta_custom")
              == {"keep": "me"}, "foreign section lost")
        check("l_only_expected_keys",
              set(raw) - {"version", "playback", "ui", "zeta_custom",
                          "migrations"} == set(), str(sorted(raw)))
        w = win()
        check("l_cfg_born_inf", str(w.cfg.get("playback.loop_file"))
              == "inf", str(w.cfg.get("playback.loop_file")))
        check("l_action_one", mode() == "one", mode())
        check("l_control_active", active_ui(), "control inactive")
        w._load_path(CLIP1)
        return again(guard(l_loaded))

    def l_loaded():
        return wait_for("l_first_load_loopfile_inf", lambda: (
            snap().get("pause") is False and loops2()[0] == "inf"),
            guard(l_done), left=80)

    def l_done():
        # the migration wrote EXACTLY once: file byte-stable across the
        # whole running session now
        before = CFG_FILE.read_bytes()
        win().chrome.btn_repeat.emit("clicked")     # on -> off: USER write
        return again(lambda: wait_for(
            "l_user_write_changes_file", lambda: CFG_FILE.read_bytes()
            != before, guard(l_final), left=40))

    def l_final():
        raw = disk_raw() or {}
        check("l_off_by_user_only", raw.get("playback", {}).get(
            "loop_file") == "no", str(raw.get("playback")))
        app.quit()
        return False

    FIRST = {"fresh": f_start, "afteroff": a_start, "legacy": l_start}

    def start(left=60):
        try:
            w = win()
        except IndexError:
            w = None
        if w is None or not w.get_mapped():
            if left <= 0:
                check("window_realized", False, "never realized")
                app.quit()
                return False
            return again(lambda: start(left - 1), 200)
        check("window_realized", True)
        first = FIRST[PHASE]
        if PHASE == "afteroff":
            # give a WRONG (boot-time) re-migration time to rewrite the
            # file before ANY check reads it
            return again(guard(a_snapshot_first), 1500)
        return again(guard(first), 400)

    def a_snapshot_first():
        a_start()
        return False

    GLib.timeout_add(400, guard(start))


def run_child(phase: str, cfg_dir: Path, state_dir: Path) -> None:
    out = subprocess.run(
        [sys.executable, os.path.abspath(__file__), phase],
        env=dict(os.environ, REELSEE_R15REP_PHASE=phase,
                 REELSEE_R15REP_BASE=str(BASE),
                 REELSEE_CONFIG_DIR=str(cfg_dir),
                 REELSEE_STATE_DIR=str(state_dir)),
        capture_output=True, text=True, timeout=280)
    sys.stdout.write(out.stdout)
    sys.stderr.write(out.stderr[-4000:])
    check(f"{phase}_rc", out.returncode == 0, str(out.returncode))
    check(f"{phase}_asserts", "SMOKE_ASSERTS_PASSED" in out.stdout,
          "missing SMOKE_ASSERTS_PASSED")


def main() -> int:
    argv = sys.argv[1:]
    if PHASE and argv:                      # child: dirs come via env
        rc = reelsee_run(PHASE)
        finish()
        return rc
    shutil.rmtree(BASE, ignore_errors=True)
    for d in (FRESH_CFG, FRESH_STATE, LEG_CFG, LEG_STATE):
        d.mkdir(parents=True)
    os.environ["REELSEE_CONFIG_DIR"] = str(FRESH_CFG)
    os.environ["REELSEE_STATE_DIR"] = str(FRESH_STATE)
    units()
    run_child("fresh", FRESH_CFG, FRESH_STATE)
    run_child("afteroff", FRESH_CFG, FRESH_STATE)
    LEG_CFG.mkdir(parents=True, exist_ok=True)
    (LEG_CFG / "config.json").write_text(
        json.dumps(LEGACY_SEED, indent=2), encoding="utf-8")
    run_child("legacy", LEG_CFG, LEG_STATE)
    shutil.rmtree(BASE, ignore_errors=True)
    finish()
    return 0


def reelsee_run(phase: str) -> int:
    # child processes boot the REAL app with the dirs inherited via env
    import reelsee
    rc = reelsee.run(["reelsee", CLIP1], on_app=schedule)
    check(f"phase_{phase}_run_rc", rc == 0, str(rc))
    return rc


if __name__ == "__main__":
    sys.exit(main())
