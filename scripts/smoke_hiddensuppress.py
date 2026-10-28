#!/usr/bin/python3
"""R16 gate (hiddensuppress): a minimized window stops DECODING video — and
nothing else happens.

The brief's properties, each asserted on the OBSERVED mpv state (R9: `vid` is
observed into the snapshot, never read with a sync getter), plus a call-spy on
the core so "nothing else happened" is a fact and not a hope:

  vid_not_suppressed_while_visible   playing window keeps its decoder
  vid_no_on_unmap                    minimize/unmap -> `vid` becomes "no"
  vid_no_on_wm_unmap                 the WINDOW-MANAGER path (state bits)
  vid_restored_on_remap              remap -> the user's own choice comes back
  audio_never_muted                  no `mute`/`pause` write anywhere in the run
  pause_untouched_while_hidden /
  explicit_pause_untouched           an EXPLICIT pause survives unmap+remap
  position_advances_while_hidden     audio keeps playing with the decoder off
  fullscreen_never_suppresses        fullscreen keeps the surface mapped
  geometry_not_persisted_while_hidden / no_geometry_writes_on_restore /
  aspect_corrections_zero_while_hidden / no_storm_on_restore
                                     the R10 sampler and R8 aspect machinery
                                     are inert on a hidden surface, and a
                                     restore is quiet (the resize storm this
                                     would otherwise cause)
  setting_off_writes_nothing         the switch off = the pre-R16 behaviour,
                                     provably zero `vid` writes
  correction_counter_live /
  correction_inert_while_unmapped    the aspect choke point is real: live
                                     while mapped (positive control), inert
                                     while unmapped (negative control)

NOTE ON THE EXPECTED VALUE: mpv resolves `vid=auto` to the selected track id
("1") once a file is loaded, so "visible" is asserted as `vid != "no"`, not
`vid == "auto"`.  The suppression itself is asserted on the exact "no".

HEADLESS STANDING-IN FOR A MINIMIZE (read this before believing the gate):
there is no window manager under Xvfb, and round 16 MEASURED that the two
paths differ (scratch/probe_state_x.py, scratch/probe_state_flags.py):

  GTK `set_visible(False)`      -> `mapped` False, surface state 0
  external `XUnmapWindow` (WM)  -> `mapped` STILL True, surface state 0x10001

So the gate covers BOTH: `set_visible(False/True)` drives the mapping path,
and a phase that fakes the surface-state read to the observed minimise value
drives the state-bit path through the same production entry point the pump
uses (`tick_pump` -> `_sync_hidden_suppression`).  Fullscreen is asserted as
a negative control so a state-bit mask can never fire on a visible window.
"""
import json
import os
import sys

from smoke_harness import SCRATCH, check, finish

CFG = SCRATCH / "hiddensup-cfg"
CFG.mkdir(parents=True, exist_ok=True)
(CFG / "config.json").write_text(json.dumps({
    "ui": {"chrome_timeout_ms": 600000, "lock_aspect": True},
    "engine": {"watchdog_enabled": False},
    "playback": {"suppress_video_when_hidden": True},
}))
os.environ["REELSEE_CONFIG_DIR"] = str(CFG)
os.environ["REELSEE_STATE_DIR"] = str(SCRATCH / "hiddensup-state")

import gi  # noqa: E402
gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import GLib  # noqa: E402

import reelsee  # noqa: E402
import settings_ui  # noqa: E402
from window import ReelseeWindow  # noqa: E402

CLIP = "testdata/smoke_360p.mp4"
AUDIO_PROPS = {"mute", "volume", "audio-delay", "audio-device",
               "audio-exclusive", "audio-pitch-correction", "pause"}


class Spy:
    """Record every write the app sends to mpv, so silence is assertable."""

    def __init__(self, core):
        self.calls: list = []
        self._set = core.set_property
        self._cmd = core.command
        core.set_property = self._rec_set
        core.command = self._rec_cmd

    def _rec_set(self, name, value):
        self.calls.append(("set", str(name), value))
        return self._set(name, value)

    def _rec_cmd(self, *args):
        self.calls.append(("cmd",) + tuple(str(a) for a in args))
        return self._cmd(*args)

    def mark(self):
        i = len(self.calls)
        return lambda: self.calls[i:]

    @staticmethod
    def props(seq):
        return [c[1] for c in seq if c[0] == "set"]

    @staticmethod
    def audio_writes(seq):
        """Any mute/pause write, `set` property or `set`/`cycle` command."""
        bad = [p for p in Spy.props(seq) if p in AUDIO_PROPS]
        for c in seq:
            if c[0] == "cmd" and len(c) >= 3 and c[1] in ("set", "cycle") \
                    and c[2] in AUDIO_PROPS:
                bad.append("cmd:" + c[2])
        return bad

    @staticmethod
    def vid_writes(seq):
        return [(c[1], c[2]) for c in seq if c[0] == "set" and c[1] == "vid"]


def playing(win) -> bool:
    s = win.core.snapshot
    return (s.get("pause") is False and s.get("idle-active") is False
            and s.get("eof-reached") is not True)


def vid_of(win):
    return win.core.snapshot.get("vid")


def decoded(win) -> bool:
    """mpv resolves `vid=auto` to the track id ("1") once a file is loaded,
    so "the decoder is on" is `vid` set to something other than "no"."""
    return vid_of(win) not in (None, "no")


def state_of(win) -> int:
    return win._surface_state()


def unmapped_flag(win) -> bool:
    """The state-bit half of `ReelseeWindow._surface_hidden` — the fact a
    real window-manager minimize leaves behind (see the module docstring)."""
    return bool(state_of(win) & ReelseeWindow._SURFACE_STATE_UNMAPPED_BITS)


def probe_schedule(app):
    from types import SimpleNamespace
    st = SimpleNamespace()

    def win():
        return app.windows[0]

    def guarded(fn):
        """A phase that raises must FAIL the gate, not print a traceback and
        let `finish()` see zero recorded failures (this happened in the first
        run of this gate: a typo killed the chain and the run still
        "passed")."""
        def inner(*a):
            try:
                return fn(*a)
            except Exception as exc:                  # noqa: BLE001
                check("gate_phase_raised", False, f"{fn.__name__}: {exc!r}")
                app.quit()
                return False
        inner.__name__ = getattr(fn, "__name__", "phase")
        return inner

    def poll(name, every, deadline_s, cond, then):
        """Generous polling, no fixed sleeps; the timeout value says what the
        property actually was, which is how a wrong expectation gets caught."""
        tries = {"n": 0}
        then = guarded(then)

        def step():
            if cond():
                then()
                return False
            tries["n"] += 1
            if tries["n"] * every >= deadline_s * 1000:
                check(name, False,
                      f"no within {deadline_s}s; vid={vid_of(win())} "
                      f"state={hex(state_of(win()))} "
                      f"mapped={win().get_mapped()}")
                app.quit()
                return False
            GLib.timeout_add(every, step)
            return False

        GLib.timeout_add(every, step)

    def later(ms, fn):
        fn = guarded(fn)
        GLib.timeout_add(ms, lambda: (fn(), False)[1])

    # ------------------------------------------------------------------ 1
    def phase_visible():
        w = win()
        st.spy = Spy(w.core)
        st.remember = {"n": 0}
        real_remember = app.geometry_store.remember

        def counting(*a, **k):
            st.remember["n"] += 1
            return real_remember(*a, **k)

        app.geometry_store.remember = counting
        st.mark_all = st.spy.mark()
        st.size0 = (w.get_width(), w.get_height())
        st.corr0 = w._aspect_corrections
        check("vid_observed", "vid" in w.core.snapshot,
              "vid" if "vid" in w.core.snapshot else "absent")
        # The app's own pre-load push (`_push_all_settings`) writes mute,
        # volume, audio-* … and can land AFTER the first frame.  Wait for it
        # and for a quiet window before taking any mark, otherwise the push
        # would be blamed on the suppression path.
        phase_quiescent()

    def phase_quiescent():
        """Wait for the app to be IDLE before starting the phases: no mpv
        write in flight, no geometry persist still debounced from start-up,
        and quiet for a FULL geometry-sampler cycle (1 s, `tick`/`_geom_sample`).

        Without this a legitimate start-up write — the first-map rect the
        sampler notices, or `_push_all_settings` landing after the first
        frame — falls inside a later phase's window and is blamed on the
        minimise path.  Measured exactly that in the first runs of this gate.
        """
        st.mark_quiet = st.spy.mark()
        st.remember["quiet0"] = st.remember["n"]
        state = {"ms": 0, "tries": 0}

        def step():
            quiet = (not st.mark_quiet()
                     and st.remember["n"] == st.remember["quiet0"]
                     and win()._geom_timer is None)
            if quiet:
                state["ms"] += 100
                if state["ms"] >= 1500:
                    phase_visible_ready()
                    return False
            else:
                state["ms"] = 0
                st.mark_quiet = st.spy.mark()
                st.remember["quiet0"] = st.remember["n"]
            state["tries"] += 1
            if state["tries"] * 100 >= 15000:
                check("app_quiescent_before_phases", False,
                      f"never idle; pending-writes={st.mark_quiet()[:4]} "
                      f"remember={st.remember['n']} "
                      f"geom_timer={win()._geom_timer}")
                app.quit()
                return False
            GLib.timeout_add(100, step)
            return False

        GLib.timeout_add(100, step)

    def phase_visible_ready():
        w = win()
        poll("vid_not_suppressed_while_visible", 50, 8.0,
             lambda: decoded(w), phase_hide)

    # ------------------------------------------------------------------ 2
    def phase_hide():
        w = win()
        check("no_persist_pending_at_hide", w._geom_timer is None,
              f"geom_timer={w._geom_timer}")
        st.mark_hide = st.spy.mark()
        st.remember["base"] = st.remember["n"]
        w.set_visible(False)                     # the minimize stand-in
        poll("vid_no_on_unmap", 50, 8.0,
             lambda: vid_of(w) == "no", phase_hidden)

    def phase_hidden():
        w = win()
        st.seq_hide = seq = st.mark_hide()
        check("unmap_really_unmapped", not w.get_mapped(),
              f"state={hex(state_of(w))} mapped={w.get_mapped()}")
        check("audio_never_muted_while_hidden", not st.spy.audio_writes(seq),
              f"audio-writes={st.spy.audio_writes(seq)}")
        check("pause_untouched_while_hidden",
              w.core.snapshot.get("pause") is False,
              repr(w.core.snapshot.get("pause")))
        check("aspect_corrections_zero_while_hidden",
              w._aspect_corrections == st.corr0,
              f"{st.corr0}->{w._aspect_corrections}")
        check("geometry_not_persisted_while_hidden",
              st.remember["n"] == st.remember["base"],
              f"remember calls {st.remember['base']}->{st.remember['n']}")
        # audio keeps playing: the position must move with the decoder off
        # (a loop wrap is also progress — the clip is 3 s and loops forever)
        t0 = w.core.snapshot.get("time-pos") or 0.0
        later(1600, lambda: advance(t0))

    def advance(t0):
        w = win()
        t1 = w.core.snapshot.get("time-pos") or 0.0
        moved = t1 > t0 + 0.4 or t1 < t0
        check("position_advances_while_hidden", moved,
              f"t0={t0:.2f} t1={t1:.2f}")
        phase_show()

    # ------------------------------------------------------------------ 3
    def phase_show():
        w = win()
        st.mark_show = st.spy.mark()
        w.set_visible(True)
        poll("vid_restored_on_remap", 50, 8.0,
             lambda: decoded(w), phase_shown)

    def phase_shown():
        # give a resize storm a chance to actually happen before denying it
        later(900, phase_quiet_after_restore)

    def phase_quiet_after_restore():
        w = win()
        st.seq_show = st.mark_show()
        check("no_geometry_writes_on_restore",
              st.remember["n"] == st.remember["base"],
              f"remember calls {st.remember['base']}->{st.remember['n']}")
        check("no_storm_on_restore",
              w._aspect_corrections == st.corr0
              and (w.get_width(), w.get_height()) == st.size0,
              f"corr={w._aspect_corrections} size={w.get_width()}x"
              f"{w.get_height()} want={st.size0}")
        check("audio_never_muted_on_restore",
              not st.spy.audio_writes(st.seq_show),
              f"audio-writes={st.spy.audio_writes(st.seq_show)}")
        # ---- explicit pause must survive unmap + remap untouched
        w.core.set_property("pause", True)
        poll("explicit_pause_taken", 50, 6.0,
             lambda: w.core.snapshot.get("pause") is True, phase_paused)

    def phase_paused():
        w = win()
        # mark AFTER the gate's own pause write, so only the app's writes are
        # in the window that follows
        st.mark_pause = st.spy.mark()
        w.set_visible(False)
        poll("vid_no_while_paused", 50, 8.0,
             lambda: vid_of(w) == "no", phase_paused_hidden)

    def phase_paused_hidden():
        w = win()
        w.set_visible(True)
        poll("vid_restored_after_paused_remap", 50, 8.0,
             lambda: decoded(w), phase_paused_back)

    def phase_paused_back():
        w = win()
        st.seq_pause = seq = st.mark_pause()
        check("explicit_pause_untouched",
              w.core.snapshot.get("pause") is True
              and not st.spy.audio_writes(seq),
              f"pause={w.core.snapshot.get('pause')} "
              f"audio-writes={st.spy.audio_writes(seq)}")
        w.core.set_property("pause", False)
        poll("resumed", 50, 6.0, lambda: playing(w), phase_fullscreen)

    # ------------------------------------------------------------------ 4
    def phase_fullscreen():
        w = win()
        st.mark_fs = st.spy.mark()
        w.fullscreen()
        later(700, phase_fullscreen_check)

    def phase_fullscreen_check():
        w = win()
        seq = st.mark_fs()
        check("fullscreen_never_suppresses",
              decoded(w) and not st.spy.vid_writes(seq),
              f"vid={vid_of(w)} vid-writes={st.spy.vid_writes(seq)}")
        w.unfullscreen()
        later(700, phase_wm_state)

    # ------------------------------------------------------------------ 4b
    def phase_wm_state():
        """THE window-manager path, which is what a real minimize is.

        Measured (scratch/probe_state_x.py): an external `XUnmapWindow` leaves
        `Gtk.Window.mapped` True and fires no signal — only the surface state
        changes (0 -> 0x10001).  This environment has no WM, so the gate
        drives the production entry point (`tick_pump` ->
        `_sync_hidden_suppression`) with the state read faked to exactly the
        observed minimise reading."""
        w = win()
        st.mark_wm = st.spy.mark()
        st.real_state = ReelseeWindow._surface_state
        ReelseeWindow._surface_state = (
            lambda self: ReelseeWindow._SURFACE_STATE_UNMAPPED_BITS)
        poll("vid_no_on_wm_unmap", 100, 8.0,
             lambda: vid_of(w) == "no", phase_wm_state_back)

    def phase_wm_state_back():
        w = win()
        st.seq_wm = st.mark_wm()
        check("wm_state_path_suppresses",
              unmapped_flag(w) and w.get_mapped(),
              f"state={hex(state_of(w))} mapped={w.get_mapped()}")
        ReelseeWindow._surface_state = st.real_state
        poll("vid_restored_on_wm_remap", 100, 8.0,
             lambda: decoded(w), phase_setting_off)

    # ------------------------------------------------------------------ 5
    def phase_setting_off():
        w = win()
        w.cfg.set("playback.suppress_video_when_hidden", False)
        settings_ui.apply_live(app, "playback.suppress_video_when_hidden",
                               False)
        poll("setting_off_applied", 50, 6.0,
             lambda: w._vidsup.enabled is False, phase_setting_off_hide)

    def phase_setting_off_hide():
        w = win()
        st.mark_off = st.spy.mark()
        w.set_visible(False)
        later(1500, phase_setting_off_check)   # >> the pump's 100 ms rung

    def phase_setting_off_check():
        w = win()
        st.seq_off = seq = st.mark_off()
        check("setting_off_writes_nothing",
              vid_of(w) != "no" and not st.spy.vid_writes(seq),
              f"vid={vid_of(w)} vid-writes={st.spy.vid_writes(seq)}")
        w.set_visible(True)
        later(700, phase_aspect_controls)

    # ------------------------------------------------------------------ 6
    def phase_aspect_controls():
        """Positive then negative control for the aspect choke point."""
        w = win()
        w._apply_aspect_size((max(200, w.get_width() // 2),
                              max(120, w.get_height() // 2)))
        check("correction_counter_live", w._aspect_corrections == st.corr0 + 1,
              f"{st.corr0}->{w._aspect_corrections}")
        w.set_visible(False)
        poll("hidden_for_aspect_control", 50, 6.0,
             lambda: not w.get_mapped(), phase_aspect_control_hidden)

    def phase_aspect_control_hidden():
        w = win()
        before = w._aspect_corrections
        w._apply_aspect_size((w.get_width(), w.get_height() + 40))
        w._apply_aspect_size((w.get_width() + 40, w.get_height()))
        check("correction_inert_while_unmapped",
              w._aspect_corrections == before,
              f"{before}->{w._aspect_corrections}")
        w.set_visible(True)
        later(700, phase_done)

    # ------------------------------------------------------------------ 7
    def phase_done():
        check("suppression_wrote_no",
              any(v == "no" for _, v in st.spy.vid_writes(st.mark_all())),
              f"vid-writes={st.spy.vid_writes(st.mark_all())[:6]}")
        # every suppression transition in the run, concatenated: the app's
        # audio properties were never touched by the hide/restore path
        # (the app's own pre-load push and the gate's own writes are outside
        # these windows by construction)
        trans = (st.seq_hide + st.seq_show + st.seq_pause + st.seq_wm
                 + st.seq_off)
        check("audio_never_written_by_suppression",
              not st.spy.audio_writes(trans),
              f"audio-writes={st.spy.audio_writes(trans)}")
        check("suppression_writes_are_vid_only",
              all(c[1] == "vid" for c in trans
                  if c[0] == "set" and c[1] in ("vid", "mute", "pause")),
              f"props={st.spy.props(trans)}")
        app.quit()

    def wait_playing(tries=0):
        if playing(win()):
            check("file_started_playing", True, "pause=False idle=False")
            phase_visible()
            return False
        if tries >= 80:
            check("file_started_playing", False, repr(win().core.snapshot))
            app.quit()
            return False
        GLib.timeout_add(100, wait_playing, tries + 1)
        return False

    GLib.timeout_add(700, wait_playing)
    return False


def main() -> int:
    rc = reelsee.run(["reelsee", CLIP], on_app=probe_schedule)
    check("app_rc", rc == 0, str(rc))
    finish()
    return 0


if __name__ == "__main__":
    sys.exit(main())
