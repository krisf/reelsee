#!/usr/bin/python3
"""REELSEE — GTK4 + libmpv video player (PLAN §6 entry point).

Run with /usr/bin/python3; libmpv is resolved automatically (R4):
REELSEE_LIBMPV, a vendor/ dir next to this file (packaged builds), the dev
repo's vendor/, then the system libmpv2 — the .deb relies on the distro
package (Depends: libmpv2).  Dev-tree runs without the system package:

    LD_LIBRARY_PATH=<repo>/vendor/usr/lib/x86_64-linux-gnu /usr/bin/python3 src/reelsee.py FILE|URL ...

If nothing resolves, the app exits 2 with a dialog + stderr diagnosis —
never an uncaught traceback (R4-P3).

IMPORTANT: apply_renderer_override() runs BEFORE gi is imported — DRI_PRIME
must exist before any GL context exists, so a changed override triggers one
guarded re-exec (REELSEE_PRIME_APPLIED makes a loop impossible).
"""
from __future__ import annotations

import logging
import os
import sys
import threading
from pathlib import Path

SRC = Path(__file__).resolve().parent
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def _split_cli(argv: list[str]) -> tuple[list[str], bool, list[str], bool]:
    """Return (targets, new_window, raw_mpv_args, simulate_overload).
    '--' starts raw mpv options (wired to the core in P10's CLI gate)."""
    targets: list[str] = []
    new_window = False
    raw: list[str] = []
    simulate = False
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("--new-window", "-n"):
            new_window = True
        elif a == "--simulate-overload":
            simulate = True
        elif a == "--":
            raw = argv[i + 1:]
            break
        elif a.startswith("-") and a != "-":
            print(f"reelsee: unknown option {a}", file=sys.stderr)
        else:
            targets.append(a)
        i += 1
    return targets, new_window, raw, simulate


def _mpv_opts_from_raw(raw: list[str]) -> list[str]:
    """Translate `--` raw CLI args into per-file loadfile options (§5.5).

    `--start=30` → `start=30`; bare flags (`--no-video`) pass through as
    key-only.  R12 blocklist + pinned init keys (vo/wid/…) are dropped
    with a warning — the CLI is convenience, not a script-injection door.
    """
    import engine
    out: list[str] = []
    for a in raw:
        if not a.startswith("--") or a == "--":
            print(f"reelsee: ignoring raw arg {a!r} (want --key=value)",
                  file=sys.stderr)
            continue
        opt = a[2:]
        key = opt.split("=", 1)[0]
        bare = key[3:] if key.startswith("no-") else key
        if bare in engine.BLOCKED_INIT_KEYS or \
                bare in engine.PINNED_INIT_KEYS or \
                bare in engine.fixed_init_options() or \
                bare.startswith("script"):
            print(f"reelsee: refusing blocked option --{key}", file=sys.stderr)
            continue
        out.append(opt)
    return out


LIBMPV_FAIL_EXIT = 2                       # R4-P3 contract exit code


def _show_libmpv_error_dialog(body: str) -> None:
    """Blocking GTK error dialog telling the user WHAT is missing and HOW
    to fix it (R4-P3 — the 0.1.0 deb died as an uncaught FileNotFoundError
    that Apport caught, see references/FINDINGS-INSTALL-CRASH.md).

    Runs a private GMainLoop: the application never exists when this fires.
    REELSEE_ERROR_DIALOG_TIMEOUT (seconds) auto-closes the dialog — the
    smoke harness uses it to prove the dialog maps without hanging; with
    no timeout a 2-minute safety quit keeps a headless-wrong environment
    from hanging forever."""
    import gi
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk, GLib

    loop = GLib.MainLoop()
    dlg = Gtk.MessageDialog(
        message_type=Gtk.MessageType.ERROR,
        buttons=Gtk.ButtonsType.OK,
        text="Reelsee cannot start — libmpv not found",
        secondary_text=body)
    dlg.connect("response", lambda *a: loop.quit())
    dlg.connect("close-request", lambda *a: loop.quit())

    def _auto_close() -> bool:
        # GTK4 has NO GtkWidget::destroy signal and destroying the widget
        # alone would NOT quit this private loop — drive the normal button
        # path (response), and quit the loop ourselves either way.
        try:
            dlg.response(Gtk.ResponseType.OK)
        except Exception:
            dlg.destroy()
        if loop.is_running():
            loop.quit()
        return False

    tmo = os.environ.get("REELSEE_ERROR_DIALOG_TIMEOUT")
    if tmo:
        GLib.timeout_add(max(250, int(float(tmo) * 1000)), _auto_close)
    else:
        GLib.timeout_add(120000, _auto_close)   # never hang forever
    dlg.present()
    logging.getLogger("reelsee").info("error dialog shown")
    loop.run()


def _libmpv_error_exit(err: BaseException) -> int:
    """Friendly failure when no libmpv can be loaded: full diagnosis on
    stderr (every searched path + both remedies), an error dialog when a
    desktop is reachable, exit 2 — never an uncaught traceback.

    Headless runs (REELSEE_HEADLESS=1) and machines with no DISPLAY/
    WAYLAND_DISPLAY are stderr-only: the dialog is desktop UX, and smoke
    gates must exit promptly.  OSError covers a found-but-unloadable
    library (dlopen failure, e.g. vendored lib without LD_LIBRARY_PATH)."""
    if isinstance(err, FileNotFoundError):
        body = str(err)              # "libmpv not found — searched: ..." + remedy
    else:
        body = (f"libmpv could not be loaded: {err}\n"
                "Remedy: install libmpv2 (sudo apt install libmpv2) or export "
                "REELSEE_LIBMPV=/path/to/libmpv.so.2")
    print(f"reelsee: {body}", file=sys.stderr, flush=True)
    headless = os.environ.get("REELSEE_HEADLESS") == "1"
    display = os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
    if not headless and display:
        try:
            _show_libmpv_error_dialog(body)
        except Exception as e2:      # a broken display must not re-crash us
            logging.getLogger("reelsee").warning(
                "could not show error dialog: %s", e2)
    return LIBMPV_FAIL_EXIT


def run(argv: list[str] | None = None, on_app=None) -> int:
    """Entry point.  on_app(app) (smoke harnesses) runs just before app.run.
    Returns 2 (no traceback, dialog when possible) when no libmpv exists
    anywhere reachable — R4-P3, the user-facing crash of 0.1.0."""
    logging.basicConfig(
        level=logging.DEBUG if os.environ.get("REELSEE_DEBUG") else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s")
    argv = list(sys.argv if argv is None else argv)

    import config
    try:
        # R15-B: one-time config migrations (the repeat-on flip) run
        # BEFORE Config() first reads the file — and must never block
        # the boot; a failed migration is a logged, visible no-op.
        config.run_migrations()
    except Exception:                          # noqa: BLE001
        logging.getLogger("reelsee").exception(
            "config migration failed — continuing on the stored file")

    from config import Config
    cfg = Config()

    import engine
    engine.apply_renderer_override(cfg)          # may re-exec; BEFORE gi!

    targets, new_window, raw_mpv, simulate = _split_cli(argv[1:])
    loadfile_opts = _mpv_opts_from_raw(raw_mpv)

    import gi
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gio, Gtk

    # R12: the window controls are OUR OWN buttons in the top overlay
    # strip (see window._build_top_chrome), so this pinned layout is a
    # belt-and-braces default rather than the mechanism — kept because
    # desktops with trimmed decoration layouts (GNOME default: close
    # only) should still never influence chrome look or behaviour.
    _settings = Gtk.Settings.get_default()
    if _settings is not None:
        _settings.set_property("gtk-decoration-layout",
                               ":minimize,maximize,close")
        # R5-D: the video area lives inside a Gtk.WindowHandle so the window
        # is draggable from anywhere; the built-in titlebar click behaviours
        # (which would fire on double/right/middle-click over the VIDEO)
        # must be off — the app owns double-click (fullscreen) and wants no
        # window menu.
        for _prop in ("gtk-titlebar-double-click",
                      "gtk-titlebar-middle-click",
                      "gtk-titlebar-right-click"):
            try:
                _settings.set_property(_prop, "none")
            except Exception:                        # noqa: BLE001
                pass                                 # unknown key: ignore

    import mpvlib as M
    from window import ReelseeWindow

    # R4-P3: library resolution is THE real-world startup failure (0.1.0
    # deb: FileNotFoundError straight into Apport).  Load here — outside
    # the constructor — so failure becomes dialog + stderr + exit 2.
    try:
        libmpv = M.load_libmpv()
    except (M.LibmpvNotFound, OSError) as e:
        return _libmpv_error_exit(e)

    class ReelseeApp(Gtk.Application):
        def __init__(self):
            super().__init__(application_id="pro.krisforbes.Reelsee",
                             flags=Gio.ApplicationFlags.NON_UNIQUE |
                             Gio.ApplicationFlags.HANDLES_OPEN)
            self.headless = os.environ.get("REELSEE_HEADLESS") == "1"
            self.lib = libmpv              # preloaded in run() above
            self.windows: list[ReelseeWindow] = []
            self._retiring: list[threading.Thread] = []
            self._opened = False
            self._force_new = new_window
            self.loadfile_opts = loadfile_opts   # CLI `--` → per-file options
            self._settings = None
            from registry import WindowRegistry
            self.registry = WindowRegistry()      # §4.10 multi-window policy
            self._engine_list = None
            self._focused = None
            # R16-D: focus recency counter and the once-per-session flag for
            # the decode-plan toast (§D.2: a demotion is never silent).
            self._focus_seq = 0
            self._plan_toast = False

        # ------------------------------------------------------- focus policy
        def on_window_focus(self, w):
            """R-audio: only the focused window keeps audio (transient mute
            on the others) while audio.focus_mute is on."""
            self._focused = w
            self._focus_seq += 1
            w._focus_seq = self._focus_seq
            if not bool(cfg.get("audio.focus_mute", True)):
                return
            for other in self.registry.focus_plan(w, True):
                other.transient_mute(True)
            w.transient_mute(False)

        # ------------------------------------------------------------ settings
        def show_settings(self):
            """Open (or re-present) the Adw preferences window (P4)."""
            if self._settings is None:
                from settings_ui import SettingsWindow
                self._settings = SettingsWindow(
                    self, cfg, on_close=self._settings_closed)
            self._settings.present()
            return self._settings

        def _settings_closed(self):
            self._settings = None

        def do_shutdown(self):  # noqa: N802 (GTK virtual)
            # app.quit() (Ctrl+Q / quit action / SIGTERM grace path) does
            # NOT fire window close-request, so flush the lazy state stores
            # here — a resume position captured seconds before quit must
            # still survive (§7).  R3-P1: every live window first captures
            # its CURRENT Snapshot position (no sync getter, R9) so the
            # flush holds the exact position at quit, not the last 5 s tick
            # (or nothing, before the first tick).
            for w in list(getattr(self, "windows", ())):
                w.capture_resume_position()
                w.capture_geometry()     # R10-A: window.json survives quit too
            for name in ("_resume_store_obj", "_ab_store_obj"):
                st = getattr(self, name, None)
                if st is not None:
                    st.persist()
            st = getattr(self, "_geometry_store_obj", None)
            if st is not None:
                st.flush()               # even a windowless quit flushes dirty
            return Gtk.Application.do_shutdown(self)

        # ------------------------------------------------------------ ab.json
        @property
        def ab_store(self):
            """Shared per-path A/B store (R-5); lazy so the state dir is only
            touched once a window actually reads/writes A/B."""
            st = getattr(self, "_ab_store_obj", None)
            if st is None:
                from config import AbStore
                st = self._ab_store_obj = AbStore()
            return st

        # -------------------------------------------------------- resume.json
        @property
        def resume_store(self):
            """§7 T1 resume positions (own resume.json, R12-independent of
            mpv --resume-playback); lazy like ab_store."""
            st = getattr(self, "_resume_store_obj", None)
            if st is None:
                from config import ResumeStore
                st = self._resume_store_obj = ResumeStore()
            return st

        # -------------------------------------------------------- window.json
        @property
        def geometry_store(self):
            """R10-A: the last WINDOWED rect lives in <state>/window.json —
            runtime state, NOT user config (nobody hand-edits where a window
            happened to be).  Lazy like ab_store; lazy-loading is also the
            corrupt-file gate (missing/garbage ⇒ None ⇒ default behaviour)."""
            st = getattr(self, "_geometry_store_obj", None)
            if st is None:
                from config import GeometryStore
                st = self._geometry_store_obj = GeometryStore()
            return st

        # ------------------------------------------------------------ opening
        def _engine_choice(self):
            """Engine for the next window.  R16-D: with engine.decoder == auto
            the choice is the DECODE PLAN (integrated iGPU up to
            engine.decode_capacity, CPU beyond; never a dGPU), not a
            round-robin over every GPU in the machine.  A pinned decoder
            routes through the config path unchanged.

            R16-E §E.2: `playback.vaapi_device` pins the CARD the plan uses
            (it may name a dGPU — a human chose it) while the plan still
            decides how many windows decode on it."""
            if str(cfg.get("engine.decoder", "auto") or "auto") != "auto":
                return None
            if self._engine_list is None:
                self._engine_list = engine.enumerate_engines()
            return self.registry.pick_engine(
                "auto", self._engine_list, engine.select_engine,
                capacity=int(cfg.get("engine.decode_capacity",
                                     engine.DECODE_CAPACITY) or 0),
                pin=engine.vaapi_device_pin(cfg))

        def _spawn_window(self, present: bool = True) -> ReelseeWindow:
            w = ReelseeWindow(self, cfg, self.lib,
                              engine_override=self._engine_choice())
            # R10-B.1/B.2: with a window already open the new one takes the
            # source's EXACT size at a cascaded position (chained windows
            # cascade from the MOST RECENT one); as the only window it
            # restores the remembered rect; with neither, round-8 defaults.
            # Size lands BEFORE present, position AFTER — Xlib can only move
            # a surface that exists (GTK 4 has no move API at all).
            plan = w.plan_open_geometry(self._cascade_source())
            if plan is not None:
                w.adopt_geometry(plan[1])
            self.windows.append(w)
            self.registry.add(w)
            self.retune_decode_threads()       # R16-C: N windows, one budget
            if present:
                w.present()
                if plan is not None:
                    w.apply_open_position()
            return w

        def _cascade_source(self) -> ReelseeWindow | None:
            """The window a new one cascades from: the most recent LIVE one,
            or None (fresh launch ⇒ restore path, §B.1 ordering)."""
            for w in reversed(getattr(self, "windows", ())):
                if not w._closing:
                    return w
            return None

        # ------------------------------------------------- R16-C decode budget
        def retune_decode_threads(self) -> None:
            """Recompute the CPU decode budget for every LIVE window.

            `vd-lavc-threads` used to be decided once, at window open, from
            the count at that instant — so opening a second window left the
            first one still asking lavc for the whole machine.  The budget is
            a pure function (engine.threads_for, unit-tested truth table);
            this is the one place that knows how many windows are live.

            Only CPU-decoded windows are written (window.apply_thread_budget
            checks the OBSERVED hwdec-current), so GPU windows are untouched.
            A window that just opened has no hwdec-current yet and is covered
            by its own init options, which already used the post-open count."""
            live = [w for w in getattr(self, "windows", ()) if not w._closing]
            threads = engine.cpu_thread_count(cfg, len(live))
            for w in live:
                try:
                    w.apply_thread_budget(threads)
                except Exception:               # noqa: BLE001
                    logging.getLogger("reelsee").exception(
                        "thread retune failed for one window")

        # ------------------------------------------------- R16-D decode plan
        def enforce_decode_plan(self) -> None:
            """The decode GPU carries at most `engine.decode_capacity` streams.

            When a session exceeds that, the window that gained focus LAST is
            demoted to CPU (§D.2) — the plan's choice, not whichever window
            happens to trip its own overload watchdog.  A window the user
            pinned to a decoder by hand is never a candidate: the per-video
            GPU/CPU toggle always overrides the plan for its window.

            A demotion is never silent.  The first one in a session toasts;
            after that the toast is suppressed (§D.2: "one-time-per-session")
            and the demotion is only logged, so a window that flaps back and
            forth cannot spam the user."""
            from registry import demotion_target
            live = [w for w in getattr(self, "windows", ()) if not w._closing]
            cap = int(cfg.get("engine.decode_capacity",
                              engine.DECODE_CAPACITY) or 0)
            victim = demotion_target(live, cap)
            if victim is None:
                return
            victim.demote_to_cpu(f"over capacity ({cap})")
            if not self._plan_toast:
                self._plan_toast = True
                try:
                    victim.chrome.show_toast(
                        f"Decode capacity ({cap}) reached — this window "
                        "decodes on the CPU")
                except Exception:               # noqa: BLE001
                    logging.getLogger("reelsee").exception(
                        "decode-plan toast failed")

        def on_decode_state_changed(self, win) -> None:
            """A window's OBSERVED decoder changed (the hwdec-current edge in
            window._on_snapshot).  Both multi-window decode policies are
            functions of who is decoding where, so both are recomputed here:
            the CPU thread budget, and the GPU stream cap."""
            self.retune_decode_threads()
            self.enforce_decode_plan()

        def do_activate(self):  # noqa: N802 (GTK virtual)
            if self._opened:
                return
            self._opened = True
            if not targets:
                self._spawn_window()
                return
            # GApplication file routing is unreliable across backends (P2
            # probe: activate, not open, fired under headless X11), so CLI
            # targets are opened here; do_open stays for bus activations.
            first = True
            for t in targets:
                if first and not self._force_new:
                    self._spawn_window().open_target(t)
                    first = False
                else:
                    self._spawn_window().open_target(t)

        def do_open(self, files, *rest):  # noqa: N802
            # PyGObject passes (files, hint) or (files, n_files, hint)
            self._opened = True
            reused = False
            for f in files:
                target = f.get_path() or f.get_uri()
                if not self._force_new and not reused:
                    w = self._empty_window()
                    if w is not None:
                        w.open_target(target)
                        w.present()
                        reused = True
                        continue
                self._spawn_window().open_target(target)

        def _empty_window(self) -> ReelseeWindow | None:
            for w in self.windows:
                if w.target is None and not w._closing:
                    return w
            return None

        # ------------------------------------------------------------ closing
        def forget_window(self, w: ReelseeWindow) -> None:
            if w in self.windows:
                self.windows.remove(w)
            self.registry.remove(w)
            self.retune_decode_threads()       # R16-C: survivors get the share
            # render context was already detached+freed with GL current
            # (window close, §0.6) — terminate mpv on a WORKER so the main
            # loop never stalls (§4.11).
            t = threading.Thread(target=w.core.destroy, daemon=True,
                                 name="mpv-terminate")
            t.start()
            self._retiring.append(t)

    app = ReelseeApp()
    app.simulate_overload = simulate            # R-7 test flag (P7)

    # R2-P4: SIGTERM/SIGINT must reach the GRACEFUL quit path — the
    # resume/A-B flush lives in do_shutdown, so a systemd stop or a
    # terminal kill still persists positions and exits 0 instead of dying
    # on the default disposition (rc 143 / KeyboardInterrupt stack).
    # g_unix_signal_add runs the handler FROM THE MAIN LOOP (self-pipe),
    # which is the only safe way to touch GTK here — a plain
    # signal.signal handler would be deferred while the loop sits inside
    # native poll.  A second, insistent signal hard-exits (deadlock
    # insurance).
    import signal as _signal
    from gi.repository import GLib, GLibUnix
    _sigs: list[int] = []

    def _graceful(_data):
        _sigs.append(1)
        if len(_sigs) > 1:
            os._exit(1)
        logging.getLogger("reelsee").info(
            "signal received: graceful quit (resume flush)")
        app.quit()
        return False

    for _sig in (_signal.SIGTERM, _signal.SIGINT):
        GLibUnix.signal_add(GLib.PRIORITY_HIGH, int(_sig), _graceful, None)

    if on_app is not None:
        on_app(app)
    rc = app.run(None)   # targets route through do_activate (see above)

    # window closes queued terminate workers; join with a cap (§4.11)
    for t in app._retiring:
        t.join(timeout=3.0)
    return rc


if __name__ == "__main__":
    sys.exit(run())
