"""ReelseeWindow — one top-level window: Overlay(GLArea) + per-window core.

The main thread only ever READS core.snapshot (R9) and writes via
core.command / core.set_property (async mpv calls).  Teardown order follows
§0.6/R11: the render context is detached+freed (GL current) while the window
closes; mpv_terminate_destroy happens later on a worker.
"""
from __future__ import annotations

import logging
import os
import threading
import time

import gi

import ab as AB
import aspect
import chrome
import config as C
import ctxmenu
import engine
import keymap
import mpvlib as M
import reelsee_io
import watchdog as WD
import wingeom
import x11pos
from mpvcore import MpvCore
from render import RenderView

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Pango", "1.0")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango  # noqa: E402

log = logging.getLogger("reelsee.window")


def make_aspect_lock_action(on: bool) -> Gio.SimpleAction:
    """`win.toggle-aspect-lock` — the command-palette/settings surface of
    `ui.lock_aspect`, built in ONE place because its shape carries a trap.

    A stateful action whose state is BOOLEAN must be created with
    parameter_type=None: declaring a "b" parameter type makes
    `activate_action(name, None)` — exactly what a command palette does —
    trip g_simple_action_activate's assertion, and the action then silently
    never fires.  Measured on this build, so the construction lives here
    where a unit test can pin it without a display (tests/test_aspect.py).
    """
    return Gio.SimpleAction.new_stateful(
        "toggle-aspect-lock", None, GLib.Variant("b", bool(on)))


def make_crop_action(on: bool) -> Gio.SimpleAction:
    """`win.crop-to-window` — the command-palette surface of the R11
    checkable "Crop to window" menu entry (cover/fill: the video fills the
    window, the overflow is clipped).

    Built in ONE place for the same measured reason as
    make_aspect_lock_action: a stateful action with BOOLEAN state must be
    created with parameter_type=None, or `activate_action(name, None)` —
    exactly what the command palette does — trips g_simple_action_activate
    and the action silently never fires.  The state doubles as the context
    menu's checkmark (Gtk renders bool-state actions checked)."""
    return Gio.SimpleAction.new_stateful(
        "crop-to-window", None, GLib.Variant("b", bool(on)))

WINDOW_EXTRA_OBSERVED = {
    "playlist-count": M.MPV_FORMAT_INT64,
    "chapter": M.MPV_FORMAT_INT64,
    "metadata": M.MPV_FORMAT_NODE,
    "track-list": M.MPV_FORMAT_NODE,
    # §7 T1 (P11): popover content sources + loop state mirror
    "playlist": M.MPV_FORMAT_NODE,
    "chapter-list": M.MPV_FORMAT_NODE,
    # R5-C: repeat button state source (strings: "no"/"inf"/"N"/"force")
    "loop-file": M.MPV_FORMAT_STRING,
    # R5-E: ab endpoints mirrored as strings ("no" when unset) — cheap,
    # keeps the main thread off sync getters, and makes clear-first
    # behaviour observable in smokes through the snapshot only.
    "ab-loop-a": M.MPV_FORMAT_STRING,
    "ab-loop-b": M.MPV_FORMAT_STRING,
    "loop-playlist": M.MPV_FORMAT_STRING,
    # R8-B: THE aspect source.  dwidth/dheight are mpv's DISPLAYED dimensions
    # — the size the picture is drawn at, after container/aspect handling — so
    # a 1080x1920 phone clip comes out vertical and a 1920x1080 clip wide.
    # width/height (coded size) is the documented fallback for the moment
    # before a display size exists.  Measured on this build: 1920/1080 for the
    # 16:9 test clip, and `video-rotate` moves NEITHER of them (rotation is
    # applied at render time), so the observed display size stays the single
    # source of truth — never a re-derived or cached value.  Snapshot-only
    # reads (R9).
    "dwidth": M.MPV_FORMAT_INT64,
    "dheight": M.MPV_FORMAT_INT64,
    "width": M.MPV_FORMAT_INT64,
    "height": M.MPV_FORMAT_INT64,
    # R11: the crop-to-window live truth (0.0/1.0).  Observed, never
    # polled: the smokes (and any future display) read it through the
    # snapshot like every other mutable input (R9 discipline).
    "panscan": M.MPV_FORMAT_DOUBLE,
}


# The window's opening size.  16:9 on purpose: R8-B compares it with the
# loaded video's aspect before deciding to move the window at all (a 16:9
# file in a 16:9 window must not cause a single resize).
START_SIZE = (960, 540)


def _abs_entry(u):
    """m3u entries must survive the playlist being saved anywhere: mpv
    resolves relative paths against the m3u file's directory (§P11)."""
    if not u or reelsee_io.is_url(u):
        return u
    return os.path.abspath(u)


class ReelseeWindow(Gtk.ApplicationWindow):
    # R12: Gtk.ApplicationWindow, NOT Adw.ApplicationWindow — Adwaita
    # ABORTS at gtk_window_set_titlebar() for its own window class, and
    # R12 requires a real (zero-height) titlebar widget so client-side
    # decoration — shadow, resize edges — stays active while the chrome
    # overlays the video.  Nothing else here depended on Adwaita's window
    # class; the settings dialog keeps using Adw.PreferencesWindow (the
    # idempotent Adw.init() below stays for it).
    def __init__(self, app, config, lib, engine_override=None):
        Adw.init()                              # R5-A: idempotent, safe
        super().__init__(application=app)
        self.app = app
        self.cfg = config
        self.lib = lib
        self.engine_choice = engine_override      # P8 round-robin (or None)
        self.target: str | None = None
        self._pending: str | None = None
        self._closing = False
        self._fs = False
        self._transient_muted = False
        # R11: mirror of the persisted crop-to-window truth (the ONE writer
        # _set_crop keeps it, the surfaces and §C's aspect-lock decision
        # read it).  Seeded from config in _build_actions.
        self._crop_on = False
        # §4.8 playlist/folder IO state
        self.playlist: list[str] | None = None
        self.playlist_dir: str | None = None
        self._folder_monitor = None
        self._next_probe = None
        self.next_probe_info: dict | None = None
        # §7 T1 (P11): URL resolve, resume, popover change detection
        self._url_resolver = None
        self._resolving: str | None = None    # in-flight URL (R2-P3 cancel)
        self._resolve_t0 = 0.0                # smoke measurement stamp
        self._resume_id = None
        self._resume_last = -10.0
        self._pl_sig = None
        self._ch_sig = None
        self._chapter_idx = None
        self._track_sig = None
        self._pl_pop = self._ch_pop = self._tr_pop = None
        self._pl_list = self._ch_list = self._tr_list = None
        self.ab_state = AB.ABState(self.cfg.get("ab.count", "inf"))

        chrome.install_black_css()
        self.add_css_class("reelsee-black")

        opts = engine.mpv_init_options(
            config, app.headless,
            n_windows=len(getattr(app, "windows", []) or []) + 1,
            engine_override=engine_override)
        self.core = MpvCore(lib, opts)
        self.core.observe(WINDOW_EXTRA_OBSERVED)
        self.core.on("file_loaded", self._on_file_loaded)
        self.core.add_listener(self._on_snapshot)

        self.view = RenderView(lib, self.core, on_ready=self._render_ready)
        self.overlay = Gtk.Overlay()
        # R5-D: the video lives inside a Gtk.WindowHandle, so the window is
        # draggable from ANYWHERE on the playing video (press + move past the
        # dnd threshold begins a real window move).  A drag is consumed by
        # the handle and its release never reaches the click gesture, so a
        # drag never toggles pause.  During fullscreen the handle is swapped
        # OUT of the tree (below) — a drag there is a structural no-op.
        self._handle = Gtk.WindowHandle()
        self._handle.set_child(self.view.area)
        self.overlay.set_child(self._handle)
        self._click_pt: tuple[float, float] | None = None
        self._click_singleshot = 0

        # R12: the chrome OVERLAYS the video and NEVER reserves layout
        # space for it.  The window content is the Gtk.Overlay itself:
        # BASE child = the render view (inside Gtk.WindowHandle, so drag-
        # anywhere keeps working), and EVERY piece of chrome is an overlay
        # child painted on top of the video — the top strip (media title
        # + our own minimise / maximise-restore / close buttons, aligned
        # START) and the bottom bar + OSD/preview/toast.  The video owns
        # the whole client area at all times, so reveal/hide is opacity
        # ONLY and the video never resizes with the chrome.  (The old
        # Adw.ToolbarView gave the top bar real layout space and
        # set_reveal_top_bars() re-flowed the content — the shrink/grow
        # bug this round kills; ToolbarView is GONE for every config.)
        # The window stays client-side decorated: set_titlebar() keeps a
        # widget — a ZERO-HEIGHT stub — so the CSD machinery (shadow,
        # resize edges via _GTK_FRAME_EXTENTS/mutter hints) stays intact
        # while the stub contributes nothing to the layout.
        self._title_label = Gtk.Label(label="REELSEE", xalign=0.5)
        self._title_label.set_ellipsize(Pango.EllipsizeMode.END)
        self._title_label.set_max_width_chars(48)   # never width-chars
        self.top_bar = self._build_top_chrome()
        self._titlebar_stub = Gtk.Box()
        self._titlebar_stub.add_css_class("reelsee-titlebar-stub")
        self.set_titlebar(self._titlebar_stub)
        # Gtk.Window content slot: the overlay IS the window content —
        # every piece of chrome floats over the video, none reserves px.
        self.set_child(self.overlay)

        self.chrome = chrome.Chrome(
            self, config,
            on_seek=self._chrome_seek, on_volume=self._chrome_volume,
            on_speed=self._chrome_speed,
            on_ab=lambda which: self._ab_hook("set_a" if which == "a"
                                              else "set_b"),
            on_repeat=self._repeat_cycle,
            on_decode=self._toggle_decode)   # R7-B: the decode toggle
        # R12: the top strip is an overlay child too — added FIRST so the
        # z-order matches the old contract (OSD/toast paint over it, the
        # bottom bar stays topmost).
        self.overlay.add_overlay(self.top_bar)
        for c in self.chrome.overlay_children():
            # R8-A: every child is added UNCONDITIONALLY — the deleted top
            # menu bar is not returned at all (it exists for NO config).
            self.overlay.add_overlay(c)
        # R12: the top strip fades as ONE group with the bottom bar
        # (opacity + can_target only — see Chrome.set_reveal).
        self.chrome.add_reveal_widget(self.top_bar)
        mc = Gtk.EventControllerMotion()
        mc.connect("motion", lambda _c, x, y: self.chrome.motion(
            x, y, self.overlay.get_width(), self.overlay.get_height()))
        mc.connect("leave", lambda *_a: self.chrome.leave())
        self.overlay.add_controller(mc)
        # R5-D: the click gesture lives on the GLArea itself (inside the
        # WindowHandle) — video clicks only, bars unaffected; and it sees
        # events BEFORE the handle's own gestures during bubbling, so a
        # double-click still toggles fullscreen even though the handle
        # claims the second press of a double-click sequence.
        click = Gtk.GestureClick()
        click.set_button(1)             # R6-A: LEFT only (explicit, gated)
        click.connect("pressed", self._on_press)
        click.connect("released", self._on_release)
        click.connect("cancel", self._on_click_cancel)
        self.view.area.add_controller(click)
        # R6-A: RIGHT-click opens the context menu at the pointer.  A
        # separate button-3 controller — the left gesture can never see
        # it (so a right click never toggles playback), WindowHandle only
        # drags with the primary button (so it never moves the window),
        # and gtk-titlebar-right-click is pinned to "none" app-wide (so
        # no competing menu).  Esc / click-away dismiss natively via the
        # popover's own grab; the window key controller never sees them.
        self._rclick_pt = None
        rclick = Gtk.GestureClick()
        rclick.set_button(3)
        rclick.connect("pressed", self._on_rclick_press)
        rclick.connect("released", self._on_rclick_release)
        rclick.connect("cancel", lambda *_a: setattr(
            self, "_rclick_pt", None))
        self.view.area.add_controller(rclick)
        self._click_gestures = (click, rclick)   # structural introspection

        # ---- R8-B aspect-locked resize ------------------------------------
        # GTK 4 has no geometry hints left, so the window CORRECTS its own
        # size when a drag breaks the video's shape (src/aspect.py has every
        # decision; the fields below are plumbing).
        self._aspect_surf = None          # surface we listen to (re-wired)
        self._aspect_size = None          # last (w, h) the surface reported
        self._aspect_pending = None       # (w, h) waiting for the debounce
        self._aspect_debounce_id = None   # ONE timer at a time == at most
        self._aspect_guard_id = None      # one correction per burst
        self._aspect_file_id = None       # new-file relayout source
        self._aspect_applying = False     # re-entrancy: never chase our own
        self._aspect_applied = None       # last size WE asked for
        self._aspect_free_until = 0.0     # fullscreen-settle deadline
        self._aspect_debounce_ms = aspect.DEBOUNCE_MS   # gate-tunable knob
        self._aspect_corrections = 0      # honest counter (smokes read it)
        self._aspect_lock_action = None   # bound in _build_actions

        # ---- R10 remembered geometry / cascade placement ------------------
        # The WHERE-decisions are pure functions in src/wingeom.py (unit
        # tested); the Xlib placement seam is src/x11pos.py (never raises);
        # the last WINDOWED rect lives in <state>/window.json via the app's
        # GeometryStore — runtime state, deliberately NOT user config.
        self._geom_rect = None            # last KNOWN windowed (x,y,w,h)
        self._geom_open = None            # opening rect awaiting its configure
        self._geom_adopted = False        # size came from source/memory (R10-B.1)
        self._geom_id = None              # 1 s move/size sampler
        self._geom_timer = None           # debounced persist (~250 ms)
        self._geom_guard_id = None        # placement-echo guard release

        self.set_default_size(*START_SIZE)
        self._update_title()

        self.keymap = dict(keymap.DEFAULT_BINDINGS)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key)
        self.add_controller(keys)

        self._build_actions()
        self._build_popovers()          # §7 T1: playlist / chapters / tracks
        self._build_context_menu()      # R6-A: right-click menu on video
        self._tick_id = GLib.timeout_add(10, self._tick)
        # §7 T1 resume: cheap dict write every 5 s, flushed on window close
        self._resume_id = GLib.timeout_add(5000, self._resume_tick)
        # R10: user drags move the window with NO GTK signal a WM guarantees
        # (GDK 4 has no move event exposed here) — a 1 s Xlib sample is the
        # honest "did it move?" detector; persistence is debounced on top.
        self._geom_id = GLib.timeout_add(1000, self._geom_sample)

        # R-7 overload watchdog (Appendix C): 1 s samples from the Snapshot
        self.watchdog = WD.Watchdog(
            enabled=bool(self.cfg.get("engine.watchdog_enabled", True)),
            fps_floor_pct=float(self.cfg.get("engine.watchdog_fps_floor_pct",
                                             90)),
            drop_ceiling_pct=float(
                self.cfg.get("engine.watchdog_drop_ceiling_pct", 3)))
        self._wd_sampler = WD.Sampler(
            self.core, simulate=getattr(app, "simulate_overload", False))
        self.watchdog.file_loaded()          # startup grace from t=0
        self._wd_id = GLib.timeout_add(1000, self._watchdog_tick)
        # R2-P2/I1: engagement watcher armed per file in _arm_engine_watch().
        # Headless contract (R13): real hardware is never touched, so the
        # CONFIGURED engine IS cpu — samples skip, nothing can demote.
        self._engagement: WD.EngagementWatcher | None = None
        # R7-B: decode lifecycle — MEMORY ONLY, never config (a restart
        # always tries the configured GPU engine again):
        #   _decode_user  user pinned CPU for the CURRENT video (toggle)
        #   _decode_auto  CPU reached AUTOMATICALLY (watchdog demote or
        #                 engagement fallback) — never a user choice
        self._decode_user = False
        self._decode_auto = False
        if getattr(app, "headless", False):
            self.watchdog.set_engine(chain=None, is_cpu=True)

        self.connect("close-request", self._on_close_request)

        # R8-B: the resize truth lives on the SURFACE, not the window, and
        # the surface object can be replaced — so (re-)wire it on every map.
        self.connect("map", self._wire_aspect_surface)
        # A surface is not permanent (fullscreen re-creates the GL surface),
        # and a layout connection on a dead surface would switch the lock off
        # in total silence — so re-wire on the replacement itself, not only
        # on the next map.
        self.connect("notify::surface", self._wire_aspect_surface)

        # R-audio focus policy (§4.10): only the focused window keeps audio
        self.connect("notify::is-active", self._on_active)

    # ------------------------------------------------------------- clicks --
    def _on_press(self, _gesture=None, n_press=1, x=0.0, y=0.0, *_a) -> None:
        # R2-P3: a click while a URL resolve is in flight cancels it
        # (restore previous state).  The wake is immediate — only the
        # pause toggle is deferred (a double-click must not pause twice).
        self._cancel_resolve()
        self.chrome.click()
        self._click_pt = (float(x), float(y))
        if int(n_press) >= 2:
            # R5-A: double-click toggles fullscreen (the ONLY fullscreen
            # gesture; the reveal mechanism handles the rest).  Fires on
            # PRESS, not release: in windowed mode the video sits inside a
            # Gtk.WindowHandle (R5-D drag-anywhere), and that widget's own
            # click gesture claims the sequence at the second press, which
            # resets our controller before its "released" signal can reach
            # us.  Our controller lives on the GLArea, so it runs first
            # during bubbling and still sees the n=2 press.
            self._cancel_single()
            self._click_pt = None
            self._act_fullscreen()

    def _on_release(self, _gesture=None, n_press=1, x=0.0, y=0.0,
                    *_a) -> None:
        if int(n_press) >= 2:
            # Already handled in _on_press (see above) — never toggle
            # twice for one double-click.
            self._cancel_single()
            self._click_pt = None
            return
        pt, self._click_pt = self._click_pt, None
        if pt is not None and (abs(float(x) - pt[0]) > 12
                               or abs(float(y) - pt[1]) > 12):
            return                     # dragged, not clicked (belt & braces)
        self._cancel_single()
        self._click_singleshot = GLib.timeout_add(
            250, self._fire_single_click)

    def _fire_single_click(self) -> bool:
        self._click_singleshot = 0
        # R5-D: single click on the video toggles playback.
        self._act_play_pause()
        return False

    def _cancel_single(self) -> None:
        if self._click_singleshot:
            GLib.source_remove(self._click_singleshot)
            self._click_singleshot = 0

    def _on_click_cancel(self, *_a) -> None:
        # The WindowHandle claimed the sequence (a real drag) or the
        # gesture was reset — any armed pause toggle is void.
        self._click_pt = None
        self._cancel_single()

    # Kept as the legacy entry point (older smokes call it directly).
    def _on_click(self, _gesture=None, n_press=1, *_a) -> None:
        if self._cancel_resolve():
            return
        if int(n_press) >= 2:
            self._act_fullscreen()
            return
        self.chrome.click()

    # ------------------------------------------------- R6-A context menu --
    def _on_rclick_press(self, _g, n_press, x, y, *_a) -> None:
        self._rclick_pt = (float(x), float(y)) if int(n_press) == 1 else None

    def _on_rclick_release(self, _g, n_press, x, y, *_a) -> None:
        pt, self._rclick_pt = self._rclick_pt, None
        if int(n_press) != 1 or pt is None:
            return
        if abs(float(x) - pt[0]) > 12 or abs(float(y) - pt[1]) > 12:
            return          # a right-button DRAG: no menu, no drag, no-op
        self._open_context_menu(float(x), float(y))

    def _open_context_menu(self, x: float, y: float) -> None:
        """Fresh snapshot-derived model, popped up AT the pointer.
        R8-A: this is THE menu — every feature the deleted top menu bar
        had lives here (header + Audio, Subtitles, Repeat, Playlist,
        Chapters, Tracks; R11 adds Crop to window after Repeat), wired to
        the same handlers."""
        pop = getattr(self, "_ctx_pop", None)
        if pop is None or self._closing:
            return
        pop.set_menu_model(ctxmenu.build(self.core.snapshot))
        rect = Gdk.Rectangle()
        rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
        pop.set_pointing_to(rect)
        pop.popup()

    def _build_context_menu(self) -> None:
        """The ctx.* action half of the right-click menu.  EVERY entry
        routes to the SAME command the playlist/chapters/tracks popovers
        or the keymap already issues — this wires, it never reimplements."""
        ag = Gio.SimpleActionGroup()
        self.insert_action_group("ctx", ag)
        noop = Gio.SimpleAction(name="noop")
        noop.set_enabled(False)             # disabled: header / no-data
        ag.add_action(noop)
        _i = GLib.VariantType("i")

        def add(name, fn, param=None):
            a = Gio.SimpleAction(name=name, parameter_type=param)
            a.connect("activate", lambda _a, p, f=fn: f(p))
            ag.add_action(a)

        def pick(prop):
            return lambda p: self.core.command(
                "set", prop, str(p.get_int32()))

        add("pick-audio", pick("aid"), _i)
        add("pick-video", pick("vid"), _i)
        add("pick-sub", pick("sid"), _i)
        add("video-off", lambda p=None: self.core.command("set", "vid",
                                                          "no"))
        add("audio-off", lambda p=None: self.core.command("set", "aid",
                                                          "no"))
        add("sub-off", lambda p=None: self.core.command("set", "sid",
                                                        "no"))
        add("goto-playlist", lambda p: self.core.command(
            "set", "playlist-index", str(p.get_int32())), _i)
        add("goto-chapter", lambda p: self.core.command(
            "set", "chapter", str(p.get_int32())), _i)
        # R9-A: seed the radio state (and _rep_mode) from the PERSISTED
        # mode so a restart opens already-checked — the seeded mpv init
        # options (engine.mpv_init_options) make the OBSERVED properties
        # agree from the first snapshot, so this matches the derivation
        # instead of racing it.  Unrecognised stored value → "off".
        _rep_seed = chrome.repeat_mode(self.cfg.get("playback.loop_file"),
                                       self.cfg.get("playback.loop_playlist"))
        self._ctx_repeat = Gio.SimpleAction.new_stateful(
            "repeat", GLib.VariantType("s"), GLib.Variant("s", _rep_seed))
        self._ctx_repeat.connect(
            "change-state",
            # NOTE: Variant.get_string() here returns the str directly,
            # NOT the (str, consumed) tuple — get_string()[0] would
            # slice "one" to "o" (probe-proven).  unpack() is unambiguous.
            lambda a, v: self._set_repeat_mode(v.unpack(), action=a))
        ag.add_action(self._ctx_repeat)
        self._rep_mode = _rep_seed
        # R11: "Crop to window" — a STATEFUL boolean action, so Gtk renders
        # the checkmark straight from its state (no model-side check text).
        # Seeded from the persisted key; the ONE writer (_set_crop) mirrors
        # every change here and on the palette action.
        self._ctx_crop = Gio.SimpleAction.new_stateful(
            "crop-to-window", None,
            GLib.Variant("b", bool(self.cfg.get("video.crop_to_window",
                                                False))))
        self._ctx_crop.connect(
            "change-state",
            lambda a, v: self._set_crop(
                a.get_state().get_boolean() if v is None
                else v.get_boolean()))
        ag.add_action(self._ctx_crop)
        self._ctx_pop = Gtk.PopoverMenu(menu_model=ctxmenu.build(
            self.core.snapshot))
        self._ctx_pop.set_parent(self.view.area)
        # an open menu HOLDS the chrome revealed (same rule as the top
        # popovers, same handler) and re-arms the idle timer on close.
        self._ctx_pop.connect("notify::visible", self._on_popover_visible)

    def _set_repeat_mode(self, mode: str,
                         action: Gio.SimpleAction | None = None) -> None:
        """ONE writer for the repeat tri-state — the bottom-bar cycle
        button AND the context-menu radio (change-state) both land here;
        the truth stays the two mpv loop properties (R5-C contract).

        R9-A (the persistence bug): this writer used to touch ONLY the
        live properties.  Every replace-load runs `_push_all_settings()`,
        which re-applies `playback.loop_file`/`playback.loop_playlist`
        from config — the DEFAULTS ("no"/"no") unless the choice is
        written back HERE, at the one place that knows the mode.  So
        both keys are persisted on every change (PERSIST_NOW, like
        mute): the pre-load push now RE-APPLIES the user's mode instead
        of erasing it, same-file reload / next-prev / restart all agree.
        The mpv properties remain the ONLY display truth; config is
        never read to paint anything except at init seeding."""
        loop_file, loop_playlist = chrome.repeat_props(mode)
        self.core.command("set", "loop-file", loop_file)
        self.core.command("set", "loop-playlist", loop_playlist)
        self.cfg.set("playback.loop_file", loop_file)
        self.cfg.set("playback.loop_playlist", loop_playlist)
        # the radio follows EVERY write, button path included: _rep_mode
        # is pre-set below precisely so the snapshot echo does not
        # double-persist, which also means _sync_repeat would never
        # reach its set_state for this transition.
        act = action if action is not None else getattr(
            self, "_ctx_repeat", None)
        if act is not None:
            act.set_state(GLib.Variant("s", mode))
        self._rep_mode = mode
        self.chrome.set_repeat(mode)     # immediate feedback (pre-snapshot)
        self.chrome.show_osd("Repeat: " + mode)

    # ------------------------------------------------- R11 crop to window
    def _set_crop(self, on: bool) -> None:
        """THE one writer of crop-to-window (R11).  The context-menu entry
        and the palette action (and the settings row's live mirror) all
        land here; nothing else writes the state.

        The R9 repeat lesson, applied: the writer PERSISTS
        `video.crop_to_window` (PERSIST_NOW, so a toggle followed by a
        quick quit survives) AND applies the live `panscan` property.  The
        key is in PROPERTY_MAP, so every replace-load runs
        `_push_all_settings()` and re-applies it — the choice survives
        file changes — and engine.mpv_init_options seeds it at mpv start,
        so it survives restart without a first-frame flicker.

        Live mechanism: panscan=1.0 with keepaspect=yes (mpv's own cover
        zoom: fill the window, clip the overflow) — MEASURED on this
        build to render through the embedded vo=libmpv render API, unlike
        a hand-computed zoom that would need re-deriving on every resize.
        The state mirrors onto BOTH surfaces (palette check + menu
        checkmark) before any feedback, and the OSD reports on/off."""
        on = bool(on)
        self._crop_on = on
        self.cfg.set("video.crop_to_window", on)
        self.core.set_property("panscan", 1.0 if on else 0.0)
        for act in (getattr(self, "_crop_action", None),
                    getattr(self, "_ctx_crop", None)):
            if act is not None and act.get_state().get_boolean() != on:
                act.set_state(GLib.Variant("b", on))
        suffix = self._crop_aspect_effect(on)
        self.chrome.show_osd("Crop to window: "
                             + ("on" if on else "off") + suffix)

    def _act_crop(self, action, value) -> None:
        """win.crop-to-window — palette activation toggles (bool state +
        parameterless activation is GAction's own toggle convenience; the
        None arm keeps a hand-activated call working)."""
        new = (value.get_boolean() if value is not None
               else not action.get_state().get_boolean())
        self._set_crop(new)

    def _crop_aspect_effect(self, on: bool) -> str:
        """§C (round 11): crop-ON suspends the aspect lock (a crop needs a
        freely resizable box), crop-OFF re-arms it and snaps the window
        back to the video's aspect — in both cases only when the lock
        setting actually wanted to be on.  Returns the OSD suffix it wants
        spelled out (the ONE hint per toggle — never per resize)."""
        try:
            wanted = bool(self.cfg.get("ui.lock_aspect", True))
        except Exception:
            wanted = False
        if not wanted or self._closing:
            return ""
        if on:
            self._aspect_forget()      # free NOW: crop wants a free box
            return "  ·  aspect lock paused"
        self._aspect_realign()
        return ""

    def _sync_crop_action(self, on: bool) -> None:
        """Mirror an out-of-band change (the settings row pushes the live
        property through apply_live without ever meeting _set_crop) into
        the ONE writer, so actions, checkmark and OSD never disagree."""
        self._set_crop(bool(on))

    def _set_handle_active(self, on: bool) -> None:
        """R5-D: the WindowHandle is IN the tree only while windowed.
        Fullscreen detaches it and re-parents the bare GLArea, so a drag
        there is structurally a no-op — no gesture, no move attempt, no
        exception (Gtk.WindowHandle itself cannot be disabled)."""
        h = self._handle
        if on and h.get_parent() is None:
            self.overlay.set_child(None)
            h.set_child(self.view.area)
            self.overlay.set_child(h)
        elif not on and h.get_parent() is not None:
            self.overlay.set_child(None)
            h.set_child(None)
            self.overlay.set_child(self.view.area)

    def _build_top_chrome(self) -> Gtk.Box:
        """R12: the top overlay strip — media title plus OUR OWN window
        controls (minimise / maximise-restore / close).  It is an overlay
        child, not a titlebar: retracting it only fades opacity, and no
        button starts a window drag (there is no WindowHandle above it)
        nor toggles playback (a press is claimed by the button and can
        never reach the video's click gesture underneath)."""
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        bar.set_valign(Gtk.Align.START)
        bar.set_halign(Gtk.Align.FILL)
        bar.add_css_class("reelsee-chrome")
        bar.set_margin_top(4)
        bar.set_margin_start(6)
        bar.set_margin_end(6)
        self._title_label.set_hexpand(True)
        bar.append(self._title_label)
        self.btn_min = Gtk.Button(icon_name="window-minimize-symbolic")
        self.btn_max = Gtk.Button(icon_name="window-maximize-symbolic")
        self.btn_close = Gtk.Button(icon_name="window-close-symbolic")
        for b, tip in ((self.btn_min, "Minimise"),
                       (self.btn_max, "Maximise"),
                       (self.btn_close, "Close")):
            b.add_css_class("reelsee-chrome")
            b.set_tooltip_text(tip)
            bar.append(b)
        # The controls act on the window and NOTHING else — each handler
        # is one window call (the gate spy-invokes all three).  Dynamic
        # dispatch (self.minimize()…), so tests can substitute the window
        # methods and watch the exact call.
        self.btn_min.connect("clicked", lambda *_a: self.minimize())
        self.btn_max.connect("clicked", self._on_maxrestore_clicked)
        self.btn_close.connect("clicked", lambda *_a: self.close())
        # Maximise/restore: the icon and tooltip track maximized-state.
        self.connect("notify::maximized", self._on_maximized_notify)
        return bar

    def _on_maxrestore_clicked(self, *_a) -> None:
        if self.is_maximized():
            self.unmaximize()
        else:
            self.maximize()

    def _on_maximized_notify(self, *_a) -> None:
        maxed = self.is_maximized()
        img = self.btn_max.get_child()
        if isinstance(img, Gtk.Image):
            img.set_from_icon_name(
                "window-restore-symbolic" if maxed
                else "window-maximize-symbolic")
        self.btn_max.set_tooltip_text("Restore" if maxed else "Maximise")

    def _apply_reveal(self, visible: bool) -> None:
        """R12: the single reveal path — Chrome crossfades EVERY reveal
        group (bottom bar AND the top strip with the window controls)
        by OPACITY only and flips can_target when hidden; no allocation
        ever changes, so the video's size is invariant across the whole
        animation (ChromeState remains the one decision-maker)."""
        self.chrome.set_reveal(bool(visible))

    # R8-A: the former _apply_top_menu_setting() (ui.show_top_menu) is GONE
    # entirely — the setting has no default, no settings row and no
    # handler, and the bar it once toggled has no widget at all.

    def _on_active(self, *_a) -> None:
        if self.is_active():
            self.app.on_window_focus(self)

    def transient_mute(self, on: bool) -> None:
        """Focus mute (audio.focus_mute): mpv-side only — never persisted,
        never touches the settings binding."""
        self._transient_muted = bool(on)
        self.core.set_property(
            "mute", bool(on) or bool(self.cfg.get("audio.muted", False)))

    # ------------------------------------------------------------- lifecycle
    def open_target(self, target: str) -> None:
        self.target = target
        if self.view.rctx is not None:
            self._do_load()
        else:
            self._pending = target       # GLArea not realized yet — §0 order

    def _render_ready(self, _view) -> None:
        log.info("render context ready")
        if self._pending:
            self._do_load()

    def _do_load(self) -> None:
        path = self._pending or self.target
        self._pending = None
        if not path:
            return
        # §4.8: a directory becomes an async-scanned playlist; the type
        # check uses Gio (no sync fs call, R-P9).  Network targets skip it.
        if reelsee_io.is_url(path):
            self._load_path(path)
            return
        f = Gio.File.new_for_path(path)
        if f.query_file_type(Gio.FileQueryInfoFlags.NONE, None) \
                == Gio.FileType.DIRECTORY:
            self._scan_folder(path)
        else:
            self._load_path(path)

    def _scan_folder(self, path: str) -> None:
        self.playlist = []
        self.playlist_dir = path
        reelsee_io.FolderScan(path, self._on_folder_scanned).start()
        self._folder_monitor = reelsee_io.FolderMonitor(
            path, self._on_folder_changed)
        self._folder_monitor.start()
        self.chrome.show_toast("Scanning folder…")

    def _on_folder_scanned(self, files) -> None:
        if self._closing or not files:
            if files == []:
                self.chrome.show_toast("No videos in folder")
            return
        self.playlist = files
        self._load_path(files[0])
        for p in files[1:]:
            self._load_path(p, "append")
        # §4.8 async probe of the NEXT playlist entry (supersedes safely)
        if len(files) > 1:
            if self._next_probe is None:
                self._next_probe = reelsee_io.NextProbe(
                    self._on_next_probed)
            self._next_probe.probe(files[1])

    def _on_next_probed(self, path, info) -> None:
        self.next_probe_info = {"path": path, **info}

    def _on_folder_changed(self, _path) -> None:
        # Gio.FileMonitor refresh (§4.8): rescan, re-append new videos
        if self.playlist is None:
            return
        known = set(self.playlist)
        reelsee_io.FolderScan(
            self.playlist_dir,
            lambda files: self._append_new_files(files, known)
        ).start()

    def _append_new_files(self, files, known) -> None:
        if self._closing:
            return
        for p in files:
            if p not in known:
                self.playlist.append(p)
                self._load_path(p, "append")

    def _load_path(self, path: str, mode: str = "replace") -> None:
        # §7 T1 URL playback: yt-dlp resolution first (worker thread,
        # result delivered via the main-thread poll — R2-P3/I2).  R2-P3:
        # direct media/stream targets skip yt-dlp entirely (mpv opens
        # those natively; the resolver is pure latency for them).
        if (mode == "replace" and reelsee_io.is_url(path)
                and bool(self.cfg.get("playback.ytdlp", True))
                and not reelsee_io.is_direct_media(path)):
            if self._url_resolver is None:
                self._url_resolver = reelsee_io.UrlResolver()
            self._url_resolver.timeout_s = float(
                self.cfg.get("playback.ytdlp_timeout",
                             reelsee_io.UrlResolver.DEFAULT_TIMEOUT_S))
            self._resolving = path      # in-flight marker (Esc/click cancel)
            self._resolve_t0 = time.monotonic()
            self.chrome.show_osd("Resolving URL…")
            self._url_resolver.resolve(path)
            return
        self._load_resolved(path, mode)

    def _cancel_resolve(self, toast: bool = True) -> bool:
        """R2-P3: Esc/click during a resolve cancels the in-flight worker
        and restores the previous state — nothing was loaded yet (the
        old file/render state is untouched), only the OSD must go."""
        if self._url_resolver is None or self._resolving is None:
            return False
        self._url_resolver.cancel()
        self._resolving = None
        self.chrome.hide_osd()
        if toast:
            self.chrome.show_toast("URL resolve cancelled")
        return True

    def _on_url_resolved(self, orig: str, direct: str,
                         err: str | None) -> None:
        if self._closing:
            return
        self._resolving = None
        self.chrome.hide_osd()
        if err:
            # R2-P3: honest failure UI — the resolver's own message, not
            # a silent return to idle.  mpv still gets its best-effort
            # shot at the raw URL afterwards.
            self.chrome.show_toast(f"URL resolve failed: {err}")
        elif direct != orig:
            self.chrome.show_toast("yt-dlp: playing direct stream")
        self._load_resolved(direct, "replace")

    def _load_resolved(self, path: str, mode: str = "replace") -> None:
        # CLI `--` raw options ride along as per-file loadfile options (§5.5)
        if mode == "replace":
            self._push_all_settings()    # runtime props set pre-load
        opts = getattr(self.app, "loadfile_opts", ()) or ()
        if mode == "replace":
            self.core.loadfile(path, *opts)
        else:
            self.core.command("loadfile", path, mode, *opts)

    def _push_all_settings(self) -> None:
        """Push every PROPERTY_MAP leaf into the live core (§7 T1)."""
        for key, (prop, kind) in C.PROPERTY_MAP.items():
            if prop == "title":       # window title is GTK-side, not mpv's
                continue
            if key == "audio.muted":  # focus mute (P8) outranks the setting
                self.core.set_property(
                    "mute", self._transient_muted
                    or bool(self.cfg.get(key, False)))
                continue
            raw = self.cfg.get(key)
            if key == "screenshot.directory" and raw:
                raw = os.path.expanduser(str(raw))
            val = C.coerce(kind, raw)
            if val is not None:
                self.core.set_property(prop, val)
        for prop, val in C.SCALE_PRESETS.get(
                str(self.cfg.get("video.scale_preset", "default")),
                {}).items():
            self.core.set_property(prop, val)
        self.core.set_property(
            "vid", "no" if self.cfg.get("playback.video_off", False)
            else "auto")

    def _on_file_loaded(self, _core, _payload) -> None:
        c = self.cfg
        # R7-B: the per-video decode override is MEMORY ONLY and dies with
        # the file — next/prev, another file, a new window: every load
        # starts by trying the configured (GPU) engine again.
        self._clear_decode_override()
        if self.watchdog.retry_pending(bool(c.get("engine.retry_on_new_file",
                                                  True))):
            sel = engine.select_engine(c.get("engine.decoder", "auto"),
                                       engine.enumerate_engines())
            engine.apply_engine(self.core, sel, c)
            self.chrome.show_toast("Engine retried on new file")
        self._push_all_settings()   # after engine retry (§7 T1 live surface)
        self._ab_restore()
        self._resume_restore()
        self.watchdog.file_loaded()             # restart grace + re-arm
        self._arm_engine_watch()                # R2-P2/I1 engagement ladder
        self._pl_sig = self._ch_sig = None      # force popover refresh
        self._chapter_idx = self._track_sig = None
        # R8-B: "vertical video opens vertical", but only once mpv actually
        # knows the display geometry — hence deferred.  Placed LAST: it must
        # not disturb the resume / AB / watchdog / engine work above, and it
        # touches none of it.
        if self._aspect_file_id is not None:
            GLib.source_remove(self._aspect_file_id)
        self._aspect_file_id = GLib.timeout_add(150, self._aspect_on_file, 20)

    # ------------------------------------------------- R2-P2/I1 engagement
    def _arm_engine_watch(self, apply_now: bool = False):
        """Wire watchdog + engagement watcher to the engine CONFIGURED for
        this file.  Headless / --simulate-overload runs stay on the legacy
        cpu-skip contract (R13: CI never touches real hardware).
        apply_now=True (R7-B re-attempt click) ALSO re-applies the
        configured mode on the live handle through the proven mechanism;
        a headless/simulate run may only ever apply software decode ("no").
        Returns the mode that was armed/applied (None: nothing applied)."""
        c = self.cfg
        headless = (getattr(self.app, "headless", False)
                    or self._wd_sampler.simulate)
        if headless:
            if apply_now:                       # configured engine IS cpu
                self.core.apply_hwdec("no")
            self.watchdog.set_engine(chain=None, is_cpu=True)
            self._engagement = None
            return "no"
        lst = getattr(self.app, "_engine_list_cached", None)
        if lst is None:                         # SYNC-FS-OK: rare, per app
            lst = self.app._engine_list_cached = engine.enumerate_engines()
        sel = engine.select_engine(c.get("engine.decoder", "auto"), lst)
        mode = engine.engine_hwdec_mode(sel, c)
        if apply_now:
            self.core.apply_hwdec(mode)         # PROVEN vid-switch, P0
        chain = WD.FallbackChain(mode)
        self.watchdog.set_engine(chain=chain, is_cpu=(mode == "no"))
        if mode == "no":
            self._engagement = None             # cpu configured: nothing to
            return mode                         # watch (watchdog skips too)
        self._engagement = WD.EngagementWatcher(
            chain, self.core.apply_hwdec, self._on_engagement)
        self._engagement.arm()
        return mode

    # -------------------------------------------------- R7-B decode toggle
    def _set_decode_flags(self, user: bool, auto: bool) -> None:
        """Memory-only lifecycle flags feeding the indicator mapping —
        NEVER written to config (engine.decoder keeps pointing at the
        configured GPU engine, so a restart always tries GPU again)."""
        self._decode_user = bool(user)
        self._decode_auto = bool(auto)
        self.chrome.set_decode_flags(self._decode_user, self._decode_auto)

    def _clear_decode_override(self) -> None:
        """R7-B: every new file load starts clean — no user override, no
        automatic marker, watchdog machinery re-enabled."""
        self.watchdog.suspended = False
        self._set_decode_flags(False, False)

    def _toggle_decode(self) -> None:
        """R7-B: the bottom-right control IS the toggle (one control, two
        roles — the indicator paints itself from the observed snapshot).

        effectively GPU (observed hwdec-current in HW_REAL_ON) → force CPU
        for the CURRENT video ONLY, via the proven apply_hwdec vid-switch
        (position preserved — no seek, no reload, no audio stop); the
        watchdog's automatic demote/promote is suspended while the
        override lives, and the GPU engagement watch is disarmed.
        CPU (own or automatic) / nothing observed yet → re-attempt the
        CONFIGURED engine mode on the live handle, re-run the engagement
        verification with a FRESH fallback chain and re-arm the watchdog
        (fresh grace).  A click on "CPU (auto)" is 'try the engine again',
        never a CPU pin."""
        observed = self.core.snapshot.get("hwdec-current")
        if (observed is not None
                and WD.mode_base(observed) in WD.HW_REAL_ON):
            self.watchdog.suspended = True       # no auto demote/promote
            if self._engagement is not None:
                self._engagement.disarm()        # GPU verification moot
            self.core.apply_hwdec("no")          # PROVEN mechanism, P0
            self._set_decode_flags(True, False)
            self.chrome.show_toast(
                "CPU decode for THIS video — next file tries GPU again")
            log.info("decode toggle: user CPU override (video only; "
                     "memory-only, watchdog suspended); observed "
                     "hwdec-current=%s", observed)
            return
        mode = self._reattempt_engine()
        if mode and mode != "no":
            self.chrome.show_toast(
                f"Attempting {mode} — verifying hardware decode")
        else:
            self.chrome.show_toast("Attempting the configured engine (CPU)")

    def _reattempt_engine(self):
        """Click while CPU (user choice or automatic demotion): clear BOTH
        markers, un-suspend the watchdog, re-apply the configured engine,
        re-arm engagement verification + watchdog (fresh grace).  Never
        touches config."""
        self.watchdog.suspended = False
        self._set_decode_flags(False, False)
        mode = self._arm_engine_watch(apply_now=True)
        self.watchdog.file_loaded()              # grace + full re-arm
        return mode

    def _on_engagement(self, act: dict) -> None:
        """Outcome of the engagement ladder: FALLBACK steps use the proven
        apply_hwdec mechanism (already applied by the watcher); record each
        step in config AND engines.json, then toast the mode that actually
        ENGAGED — never claim a mode that isn't running."""
        kind = act.get("kind")
        if kind in ("fallback", "exhausted"):
            entry = {"from": act["from"], "to": act["to"],
                     "ts": int(time.time()), "reason": "engagement"}
            self.cfg.set("engine.last_fallback", entry)
            engine.record_last_fallback(entry)  # engines.json (dispatch)
            # R7-B: this is AUTOMATIC CPU/fallback entry — mark it so the
            # indicator reads "CPU (auto)"; NEVER a user override (the
            # watchdog stays armed, the next file retries GPU anyway).
            self._set_decode_flags(self._decode_user, True)
        if kind == "engaged":
            if act.get("fell_back"):
                self.chrome.show_toast(
                    f"Hardware decode fell back — engaged {act['mode']}")
        elif kind == "exhausted":
            self.chrome.show_toast(
                "Hardware decode did not engage — decoding with CPU (no)")
        elif kind == "gave-up":
            # R7-B: exhausted without engaging either — automatic CPU
            self._set_decode_flags(self._decode_user, True)
            self.chrome.show_toast(
                f"Hardware decode did not engage ({act['mode']})")

    def _ab_restore(self) -> None:
        """R-5: reopen a file with its last A/B selection (ab.json)."""
        entry = self.app.ab_store.get(str(self.target)) if self.target else None
        self.ab_state = AB.ABState.from_dict(entry or {})
        self.ab_state.set_count(self.cfg.get("ab.count", "inf"))
        if self.ab_state.active:
            self.ab_state.apply(self.core)
            self.chrome.show_toast(
                f"A/B restored: {chrome.fmt_time(self.ab_state.a)}"
                f"–{chrome.fmt_time(self.ab_state.b)}")
        self.chrome.set_ab(self.ab_state.a, self.ab_state.b)

    def _on_snapshot(self, core) -> None:
        self._update_title()
        self.chrome.update_snapshot(core.snapshot)
        self._sync_menus()

    # ---------------------------------------------------------------- resume
    def _resume_record(self, *, force: bool) -> None:
        """Write the CURRENT Snapshot position into the resume store dict
        (persist=False; the flush happens on close/shutdown — R9: the
        Snapshot is a plain dict read, never a sync mpv getter).

        force=True is the graceful-shutdown path (R3-P1): the position at
        quit must land in resume.json exactly, even when the 5 s tick has
        never fired or the delta gate would skip it.
        """
        snap = self.core.snapshot
        pos, dur = snap.get("time-pos"), snap.get("duration")
        if not self.target or pos is None or not dur:
            return
        if pos <= 1.0 or pos >= float(dur) - 1.0:
            self.app.resume_store.remove(str(self.target), persist=False)
            return
        if force or abs(pos - self._resume_last) >= 4.0:
            self._resume_last = pos
            self.app.resume_store.set(str(self.target),
                                      {"pos": round(float(pos), 2)},
                                      persist=False)

    def _resume_tick(self) -> bool:
        """Every 5 s: remember where we are (resume.json, §7 T1)."""
        if self._closing:
            return False
        try:
            self._resume_record(force=False)
        except Exception:                       # noqa: BLE001
            log.exception("resume tick failed")
        return True

    def capture_resume_position(self) -> None:
        """R3-P1: the graceful shutdown path calls this BEFORE flushing the
        store, so quit saves the exact position, not the last 5 s tick."""
        try:
            self._resume_record(force=True)
        except Exception:                       # noqa: BLE001
            log.exception("resume capture failed (shutdown)")

    def _resume_restore(self) -> None:
        """§7 T1: jump back to resume.json position after a file loads."""
        if not self.target or not bool(self.cfg.get("playback.resume", True)):
            return
        entry = self.app.resume_store.get(str(self.target)) or {}
        try:
            pos = float(entry.get("pos") or 0.0)
        except (TypeError, ValueError):
            return
        if pos < float(self.cfg.get("playback.resume_min_s", 300)):
            return
        self._resume_last = pos
        self.core.command("seek", str(pos), "absolute")
        self.chrome.show_toast(f"Resumed at {chrome.fmt_time(pos)}")

    def _update_title(self) -> None:
        snap = self.core.snapshot
        name = snap.get("media-title") or snap.get("filename")
        fmt = str(self.cfg.get("ui.title_format", "") or "")
        if not name:
            base = "REELSEE"
        elif fmt:
            base = (fmt.replace("${media-title}", str(name))
                        .replace("${filename}",
                                 str(snap.get("filename") or "")))
        else:
            base = f"{name} — REELSEE"
        if self._fs or self.is_fullscreen():
            base = f"[{base}]"
        self.set_title(base)
        # R12: the top strip's centred title label shows the FILENAME —
        # ellipsized, max-width-chars bounded (set at creation), never the
        # decorated WM title.
        self._title_label.set_label(str(name) if name else "REELSEE")

    def _tick(self) -> bool:
        if self._closing:
            return False
        self.view.poll()
        self.core.poll()
        self._apply_reveal(self.chrome.tick())
        if self._url_resolver is not None:
            # R2-P3/I2: resolver results are delivered by THIS main-thread
            # poll draining the worker's queue — never GLib from the worker.
            res = self._url_resolver.poll()
            if res is not None:
                self._on_url_resolved(*res)
        if self._next_probe is not None:
            # R3-P3(a): same main-thread drain for the ffprobe worker
            try:
                self._next_probe.poll()
            except Exception:                   # noqa: BLE001
                log.exception("next-probe delivery failed")
        if self._engagement is not None and self._engagement.armed:
            # R2-P2: cheap snapshot read every poll (R9/R10: observed only)
            self._engagement.feed(
                self.core.snapshot.get("hwdec-current"))
        return True

    def _watchdog_tick(self) -> bool:
        if self._closing:
            return False
        try:
            action = self.watchdog.feed(self._wd_sampler.sample())
            if action is not None:
                WD.apply_action(self, action, self.cfg)
                if (action.get("kind") == "demote"
                        and str(action.get("to")) == "no"):
                    # R7-B: reached CPU AUTOMATICALLY (overload demote) —
                    # indicator shows "CPU (auto)", NOT a user override.
                    self._set_decode_flags(self._decode_user, True)
        except Exception:                       # noqa: BLE001
            log.exception("watchdog tick failed")
        return True

    def _on_close_request(self, *_a) -> bool:
        # §0.6: probes/state first (nothing yet in P2), then GL-current
        # render-context teardown; terminate happens on the app's worker.
        self._cancel_single()            # R5-D: no pause toggle post-mortem
        GLib.source_remove(self._tick_id)
        GLib.source_remove(self._wd_id)
        if self._resume_id is not None:
            GLib.source_remove(self._resume_id)
            self._resume_id = None
        # R8-B: a pending correction or a file-poll outliving the window
        # would resize a dead surface (and spam a traceback per tick).
        for _sid in ("_aspect_debounce_id", "_aspect_guard_id",
                     "_aspect_file_id"):
            _src = getattr(self, _sid, None)
            if _src is not None:
                GLib.source_remove(_src)
                setattr(self, _sid, None)
        self._aspect_pending = None
        self._aspect_applying = False
        self._closing = True
        # R10: the geometry sampler/timers die with the window, and the
        # CURRENT windowed rect must reach disk even if the 250 ms debounce
        # never got its turn (quit without a close fires the same capture
        # from ReelseeApp.do_shutdown — app.quit() skips close-request).
        if self._geom_id is not None:
            GLib.source_remove(self._geom_id)
            self._geom_id = None
        for _sid in ("_geom_timer", "_geom_guard_id"):
            _src = getattr(self, _sid, None)
            if _src is not None:
                GLib.source_remove(_src)
                setattr(self, _sid, None)
        self.capture_geometry()
        if self._url_resolver is not None:
            self._url_resolver.cancel()
        if self._folder_monitor is not None:      # §4.8 IO teardown
            self._folder_monitor.close()
            self._folder_monitor = None
        if self._next_probe is not None:
            self._next_probe.cancel()
            self._next_probe = None
        self.capture_resume_position()          # R3-P1 exact position first
        self.app.resume_store.persist()           # §7 T1 flush resume.json
        self.view.teardown()
        self.app.forget_window(self)
        return False   # let GTK finish destroying the window

    def dispose(self) -> None:  # noqa: D102 (GTK virtual)
        self.view.teardown()
        super().dispose()

    # ------------------------- R10 remembered geometry / cascade placement --
    # Every WHERE-decision is a pure function in src/wingeom.py (unit-tested
    # in tests/test_wingeom.py); this side is plumbing plus the Xlib seam
    # (src/x11pos.py) that GTK 4 refuses to offer.  Rules that live here:
    #   * a NEW window takes the source window's EXACT size + shifted
    #     position (cascade_place), the aspect lock does NOT refit it;
    #   * only a WINDOWED rect is ever remembered (§A.2) — closing while
    #     fullscreen or maximized leaves the remembered rect untouched;
    #   * debounced saves (~250 ms) with an unconditional flush on close
    #     and on quit (§A.1);
    #   * no X11 (Wayland) ⇒ size-only degradation, one debug log, no raise.

    def _geom_enabled(self) -> bool:
        """ui.remember_geometry is read LIVE (a settings flip affects the
        NEXT window without restart)."""
        return bool(self.cfg.get("ui.remember_geometry", True))

    def _geom_offset(self) -> int:
        try:
            return max(0, min(200, int(
                self.cfg.get("ui.cascade_offset_px", 50))))
        except (TypeError, ValueError):
            return 50

    def _geom_monitor_areas(self) -> list:
        try:
            mons = self.get_display().get_monitors()
        except Exception:                       # noqa: BLE001
            mons = []
        return wingeom.work_areas(mons)

    def _geom_monitor_index(self, rect):
        """Which monitor the rect sits on (the index stored with it, §A)."""
        try:
            geos = [m.get_geometry()
                    for m in self.get_display().get_monitors()]
        except Exception:                       # noqa: BLE001
            return None
        return wingeom.pick_monitor(rect, wingeom.work_areas(geos))

    def _geom_windowed(self) -> bool:
        try:
            return not (self._fs or self.is_fullscreen()
                        or self.is_maximized())
        except Exception:                       # noqa: BLE001
            return False

    def _geom_live_rect(self):
        """Root-relative (x, y, w, h): the Xlib truth on X11; anywhere else
        the size-only fallback (position stays whatever was last known)."""
        live = x11pos.get_rect(self)
        if live is not None:
            return live
        try:
            w, h = self.get_width(), self.get_height()
        except Exception:                       # noqa: BLE001
            return None
        if w <= 1 or h <= 1:
            return None
        px, py = self._geom_rect[:2] if self._geom_rect else (0, 0)
        return (px, py, w, h)

    def rect_for_cascade(self):
        """The rect a NEW window cascades from: the LIVE one while windowed,
        else the last windowed rect this window actually had — never a
        fullscreen or maximized geometry (§B.2, same rule as §A.2)."""
        if self._geom_windowed():
            live = x11pos.get_rect(self)
            if live is not None:
                self._geom_rect = live
        return self._geom_rect

    def plan_open_geometry(self, source):
        """(mode, rect) for THIS window, or None ⇒ plain default behaviour.

        Ordering (the §B.1 state table): a live source window ⇒ "cascade"
        (exact source size, cascaded position on the SOURCE monitor);
        no source but a remembered rect ⇒ "restore" (clamped into that
        monitor's work area, size never grown); neither ⇒ None, and the
        round-8 defaults (START_SIZE + initial aspect fit) stand.
        """
        if not self._geom_enabled():
            return None
        if source is not None:
            srect = source.rect_for_cascade()
            if srect is not None:
                rect = wingeom.cascade_place(srect, self._geom_monitor_areas(),
                                             self._geom_offset())
                if rect is not None:
                    return ("cascade", rect)
        stored = self.app.geometry_store.last()
        if stored is not None:
            areas = self._geom_monitor_areas()
            idx = self.app.geometry_store.last_monitor()
            if idx is None or idx >= len(areas):
                idx = wingeom.pick_monitor(stored, areas)
            wa = areas[idx] if idx is not None and idx < len(areas) else None
            rect = wingeom.clamp_into_workarea(stored, wa) if wa else stored
            if rect is not None:
                return ("restore", rect)
        return None

    def adopt_geometry(self, rect) -> None:
        """Accept an externally chosen opening geometry (§B.1): the EXACT
        pixel size BEFORE present (GTK resizes a mapped window too, but the
        user must never see a second size), and the position applied AFTER
        the surface exists by apply_open_position().  _geom_adopted is the
        one-shot "don't aspect-refit my first file" flag."""
        self._geom_rect = tuple(rect)
        self._geom_open = tuple(rect)
        self._geom_adopted = True
        self.set_default_size(rect[2], rect[3])

    def apply_open_position(self, retry: bool = True) -> bool:
        """present() first, THEN configure: Xlib can only move a surface
        that exists.  One retry at first-map for an xid that was not ready
        yet; after that, size-only for good — on Wayland positioning simply
        does not exist (x11pos logged it once and never raises)."""
        rect, self._geom_open = self._geom_open, None
        if rect is None or self._closing:
            return False
        self._geom_arm_guard(rect)      # our placement is not a user resize
        if x11pos.set_rect(self, rect):
            return True
        if retry and self.get_surface() is None:
            GLib.timeout_add(150, lambda: (self.apply_open_position(False),
                                           False)[1])
        return False

    def _geom_arm_guard(self, rect) -> None:
        """The surface-layout echo of our OWN configure must not read as a
        user resize (§B.1: the aspect lock resumes on the NEXT *user*
        resize).  Pre-seed the dedupe size and reuse round 8's applying
        latch when nobody else holds it."""
        self._aspect_size = (rect[2], rect[3])
        if self._aspect_applying:
            return
        self._aspect_applying = True

        def _off():
            self._geom_guard_id = None
            self._aspect_applying = False
            return False
        if self._geom_guard_id is not None:
            GLib.source_remove(self._geom_guard_id)
        self._geom_guard_id = GLib.timeout_add(
            aspect.APPLY_GUARD_MS + 60, _off)

    def _geom_consume(self) -> bool:
        """One-shot: True the first time this window's geometry adoption
        is honoured (its FIRST file resolution in _aspect_on_file)."""
        adopted, self._geom_adopted = self._geom_adopted, False
        return adopted

    def _geom_sample(self):
        """1 s: the cheap truth about drags and WM moves.  A CHANGED windowed
        rect schedules the debounced write; a fullscreen rect is never even
        recorded (§A.2), so nothing downstream can remember it by accident."""
        if self._closing:
            return False
        try:
            if self._geom_windowed():
                live = self._geom_live_rect()
                if live is not None and live != self._geom_rect:
                    self._geom_rect = live
                    self._geom_schedule_persist()
        except Exception:                       # noqa: BLE001
            log.exception("geometry sample failed")
        return True

    def _geom_schedule_persist(self):
        """§A.1: debounced ~250 ms — a resize storm writes once, not 60×."""
        if self._geom_timer is not None:
            GLib.source_remove(self._geom_timer)
        self._geom_timer = GLib.timeout_add(250, self._geom_persist_now)

    def _geom_persist_now(self):
        self._geom_timer = None
        self._geom_persist()
        return False

    def _geom_persist(self):
        if not self._geom_enabled() or self._geom_rect is None:
            return
        self.app.geometry_store.remember(
            self._geom_rect, self._geom_monitor_index(self._geom_rect))

    def capture_geometry(self) -> None:
        """§A.1/A.2: the flush-on-close / flush-on-quit promise: sample the
        CURRENT rect and push it to disk NOW, debounce or not.  Fullscreen
        or maximized at that moment leaves the previously remembered rect
        byte-for-byte intact (§A.2's exact scenario)."""
        try:
            windowed = self._geom_windowed()
            if windowed:
                live = self._geom_live_rect()
                if live is not None:
                    self._geom_rect = live
            store = self.app.geometry_store
            if self._geom_enabled():
                store.remember(
                    self._geom_rect,
                    self._geom_monitor_index(self._geom_rect)
                    if self._geom_rect else None,
                    windowed=windowed)
            store.flush()
        except Exception:                       # noqa: BLE001
            log.exception("geometry capture failed")

    # ------------------------------------------ R8-B aspect-locked resizing
    # Every DECISION is a pure function in src/aspect.py (unit-tested in
    # tests/test_aspect.py); this section is signal plumbing plus the three
    # promises that only the window can keep: never fight the user, never
    # fight the compositor, never fight ourselves.
    def _aspect_locked(self) -> bool:
        """Read LIVE, on every signal: flipping ui.lock_aspect off takes
        effect on the very next layout event — no restart, no cached flag.

        R11-C: this is the ONE eligibility question the round-8 machinery
        asks, and it now delegates to the single pure truth,
        aspect.aspect_lock_active — the setting, the crop suspension
        (crop-to-window means "any shape is fine", so a lock fight would
        undo the crop) and the not-ours-to-correct geometries are decided
        in exactly one place, never twice."""
        try:
            setting = bool(self.cfg.get("ui.lock_aspect", True))
        except Exception:                          # noqa: BLE001
            setting = False
        try:
            fs = bool(self._fs) or self.is_fullscreen()
            mx = bool(self.is_maximized())
        except Exception:                          # noqa: BLE001
            fs = mx = False
        return aspect.aspect_lock_active(setting, self._crop_on, fs, mx)

    def _aspect_frozen(self) -> bool:
        """Geometries that are not ours to correct: fullscreen, maximized,
        and the moment right after a fullscreen toggle (leaving fullscreen
        must not snap the window to a size nobody asked for).  self._fs is
        the app's own flag because X11 without a window manager never sends
        the state notify back (P2 finding); the real state is secondary.
        R11-C: fs/maximized also live inside aspect.aspect_lock_active (the
        single eligibility truth); this stays as the TIMING guard."""
        if self._fs or self.is_fullscreen() or self.is_maximized():
            return True
        return time.monotonic() < self._aspect_free_until

    def _work_area(self) -> tuple[int, int]:
        """The monitor work area: the ceiling a correction may never exceed.
        Single monitor is the honest assumption (multi-head placement is a
        window-positioning feature, not an aspect one); the fallback keeps
        the ceiling real instead of unbounded if GDK has none yet."""
        try:
            mons = self.get_display().get_monitors()
            if mons:
                r = mons[0].get_workarea()
                if r.width > 0 and r.height > 0:
                    return (int(r.width), int(r.height))
        except Exception:                          # noqa: BLE001
            log.exception("work area lookup failed; using fallback ceiling")
        return (1600, 1000)

    def _wire_aspect_surface(self, *_a) -> None:
        """Gdk.Surface::layout(surface, width, height) is the only resize
        notification GTK 4 leaves us.  Re-wired on every map: a surface can
        be replaced (GL context loss, reparent) and a dead connection would
        disable the lock in total silence."""
        surf = self.get_surface()
        if surf is None or surf is self._aspect_surf:
            return
        self._aspect_surf = surf
        surf.connect("layout", self._on_surface_layout)

    def _on_surface_layout(self, _surface, width, height, *_args) -> None:
        """A size the user or the compositor already gave us.

        Two measured properties of this signal decide the whole design
        (measured under Xvfb and asserted by scripts/smoke_aspectlock.py,
        check `storm_quiet_when_paused`): it also fires while merely
        repainting — about 9 events per 600 ms playing, ~1 paused — always
        with the CURRENT size, and our own correction answers back through
        it.  Therefore:
          * a size equal to the last one we recorded is a repaint or a
            move, not a resize — that single dedupe defuses the per-frame
            storm (and a Gtk.WindowHandle move never even comes here: its
            geometry is untouched, which is exactly the requirement);
          * the correction is scheduled, never applied inline — ONE timer
            per burst, armed by the first mismatch and deliberately NOT
            re-armed by the ones after it, so a fast drag gets exactly one
            correction and a burst can never postpone itself forever;
          * while our own correction is in flight nothing is scheduled."""
        try:
            size = (int(width), int(height))
        except (TypeError, ValueError):
            return
        same = (size == self._aspect_size)
        self._aspect_size = size
        if self._aspect_applying:
            return          # our own resize echoing back is not a new event
        if not self._aspect_locked():
            self._aspect_forget()               # free NOW: no stale state
            return
        if same or self._aspect_frozen():
            return
        if not aspect.needs_correction(size[0], size[1],
                                       aspect.aspect_of(self.core.snapshot)):
            return
        self._aspect_pending = size
        if self._aspect_debounce_id is None:     # at most ONE per burst
            self._aspect_debounce_id = GLib.timeout_add(
                self._aspect_debounce_ms, self._aspect_correct)

    def _aspect_correct(self) -> bool:
        """The burst settled: apply AT MOST ONE correction, computed from
        the NEWEST size (the user may have kept dragging during the
        debounce, and correcting a stale box is how a window rubber-bands).
        """
        self._aspect_debounce_id = None
        size, self._aspect_pending = self._aspect_pending, None
        if size is None or self._closing:
            return False
        if not self._aspect_locked() or self._aspect_frozen():
            return False
        a = aspect.aspect_of(self.core.snapshot)
        if not aspect.needs_correction(size[0], size[1], a):
            return False
        wa_w, wa_h = self._work_area()
        fit = aspect.fit_aspect(size[0], size[1], a, max_w=wa_w, max_h=wa_h)
        if fit == self._aspect_applied:
            # We already asked for exactly this and the window system answered
            # with something bigger (its own content minimum — the bottom bar
            # is ~360 px wide).  That answer will not change, so asking again
            # every burst would be a resize fight, and fights are how players
            # die.
            return False
        self._apply_aspect_size(fit)
        return False

    def _apply_aspect_size(self, size: tuple[int, int]) -> None:
        """set_default_size() is the ONE proven way to resize a mapped
        application window on this build (Gtk.Window.resize() does not
        exist).  The guard is armed BEFORE the call because the answer
        arrives through the very signal we are reacting to."""
        self._aspect_applying = True
        self._aspect_applied = size
        self._aspect_corrections += 1
        self.set_default_size(size[0], size[1])
        if self._aspect_guard_id is not None:
            GLib.source_remove(self._aspect_guard_id)
        self._aspect_guard_id = GLib.timeout_add(
            aspect.APPLY_GUARD_MS, self._end_aspect_guard)

    def _end_aspect_guard(self) -> bool:
        """Our correction is on screen; whatever the compositor answered with
        is the settled geometry now (recorded by the layout handler), so an
        identical size can never look like a fresh drag."""
        self._aspect_guard_id = None
        self._aspect_applying = False
        return False

    def _aspect_forget(self) -> None:
        """Drop everything in flight — no pending correction, no stale guard,
        no remembered request.  'Off' has to mean FREE resizing immediately,
        and a source we left armed could still move the window later."""
        if self._aspect_debounce_id is not None:
            GLib.source_remove(self._aspect_debounce_id)
            self._aspect_debounce_id = None
        if self._aspect_guard_id is not None:
            GLib.source_remove(self._aspect_guard_id)
            self._aspect_guard_id = None
        self._aspect_pending = None
        self._aspect_applying = False
        self._aspect_applied = None

    def _aspect_realign(self) -> None:
        """Flipping the lock ON mid-playback adopts the shape the user has
        the window in RIGHT NOW, corrected to the video's aspect.  The flip
        itself is the request: no debounce, no burst rules here."""
        if self._closing or not self._aspect_locked() or self._aspect_frozen():
            return
        size = self._aspect_size or START_SIZE
        a = aspect.aspect_of(self.core.snapshot)
        if not aspect.needs_correction(size[0], size[1], a):
            return
        self._aspect_applied = None               # fresh request, retry OK
        wa_w, wa_h = self._work_area()
        self._apply_aspect_size(aspect.fit_aspect(size[0], size[1], a,
                                                  max_w=wa_w, max_h=wa_h))

    def _aspect_on_file(self, left: int = 20) -> bool:
        """§R8-B.4: a newly loaded video sizes the window to ITS aspect,
        inside 80 % of the work area — 1080x1920 opens tall, 3840x2160 opens
        wide.

        Deferred and briefly polling because dwidth/dheight arrive with (or
        after) file-loaded and R9 keeps synchronous mpv getters off the main
        thread; the resume / AB / watchdog work in _on_file_loaded has long
        finished by the first tick and is not touched by anything here.

        A window whose shape is ALREADY the video's shape is not moved: a 16:9
        window playing a 16:9 file stays exactly where the user put it, and an
        unknown aspect (audio-only, idle) leaves the size alone entirely.
        Anything that IS off-shape — a vertical clip opened in the 960x540
        startup box — is fitted into the 80 % work-area box, which is the
        brief's rule and the only honest answer for a window nobody has sized
        yet."""
        if self._closing:
            self._aspect_file_id = None
            return False
        a = aspect.aspect_of(self.core.snapshot)
        if a is None:
            if left > 1:
                self._aspect_file_id = GLib.timeout_add(
                    100, self._aspect_on_file, left - 1)
                return False
            self._aspect_file_id = None
            self._geom_consume()         # nothing to refit anyway (R10-B.1)
            return False                 # audio-only / idle: leave it alone
        self._aspect_file_id = None
        self._aspect_applied = None      # new video: new aspect, retries OK
        if self._geom_consume():
            # R10-B.1: THIS window opened at a size CHOSEN FOR IT — the
            # source window's exact pixel size or the remembered rect.  The
            # round-8 initial fit is a NO-SHOW on the first file; the lock
            # resumes for the next file and on any user resize meanwhile.
            log.debug("aspect refit skipped: window opened with adopted "
                      "geometry (R10-B.1)")
            return False
        if not self._aspect_locked() or self._aspect_frozen():
            return False
        w, h = self._aspect_size or START_SIZE
        if not aspect.mismatch(w, h, a):
            return False                 # already the right shape
        wa_w, wa_h = self._work_area()
        fit = aspect.fit_in_workarea(wa_w, wa_h, a)
        if fit and fit != (w, h):
            self._apply_aspect_size(fit)
        return False

    def _sync_aspect_action(self, on) -> None:
        """Mirror the setting into the palette action (a settings row never
        goes through the action, and the palette must show the truth).
        lookup_action() returns None on this build (measured), hence the
        stored reference from _build_actions."""
        act = self._aspect_lock_action
        if act is None:
            return
        on = bool(on)
        if act.get_state().get_boolean() != on:
            act.set_state(GLib.Variant("b", on))

    def _act_aspect_lock(self, action, value) -> None:
        """win.toggle-aspect-lock — the palette/accel surface for
        ui.lock_aspect, STATEFUL so it also reports the current state.  The
        settings row is the other writer of the same key; it lands in
        on_setting_changed, which mirrors the state back here, so the two can
        never disagree.

        Off is total: _aspect_forget() clears the guard AND the pending
        correction, so the very next drag is free."""
        new = (value.get_boolean() if value is not None
               else not action.get_state().get_boolean())
        action.set_state(GLib.Variant("b", new))   # set_state: no re-emit
        self.cfg.set("ui.lock_aspect", new)
        if new:
            self._aspect_realign()
        else:
            self._aspect_forget()
        self.chrome.show_osd("aspect lock on" if new else "aspect lock off")

    # ---------------------------------------------------------------- actions
    def _build_actions(self) -> None:
        group = Gio.SimpleActionGroup()
        self.insert_action_group("win", group)
        simple = {
            "play-pause": self._act_play_pause,
            "seek-small": lambda p: self._act_seek(p, "small"),
            "seek-long": lambda p: self._act_seek(p, "long"),
            "volume-up": lambda p: self._act_volume(+1),
            "volume-down": lambda p: self._act_volume(-1),
            "toggle-mute": self._act_mute,
            "fullscreen": self._act_fullscreen,
            "fullscreen-exit": self._act_fullscreen_exit,
            "screenshot": self._act_screenshot,
            "cycle-audio": lambda p: self.core.command("cycle", "audio"),
            "cycle-sub": lambda p: self.core.command("cycle", "sub"),
            "cycle-sub-visible":
                lambda p: self.core.command("cycle", "sub-visibility"),
            "frame-step": lambda p: self.core.command("frame-step"),
            "frame-back-step": lambda p: self.core.command("frame-back-step"),
            "loop-file": self._act_loop_file,
            # §7 T1 (P11): stop / quit / playlist / playlist-loop / chapters
            "stop": self._act_stop,
            "quit": lambda p: self.app.quit(),
            "playlist-next": lambda p: self.core.command("playlist-next"),
            "playlist-prev": lambda p: self.core.command("playlist-prev"),
            "loop-playlist": self._act_loop_playlist,
            "chapter-next": lambda p: self.core.command("add", "chapter", "1"),
            "chapter-prev": lambda p: self.core.command("add", "chapter",
                                                        "-1"),
            "settings": lambda p: self.app.show_settings(),
            "speed-up": lambda p: self._act_speed(+0.25),
            "speed-down": lambda p: self._act_speed(-0.25),
            "set-a": lambda p: self._ab_hook("set_a"),
            "set-b": lambda p: self._ab_hook("set_b"),
            "clear-ab": lambda p: self._ab_hook("clear"),
            "exit": lambda p: self.close(),
        }
        self._actions = {}
        for name, fn in simple.items():
            act = Gio.SimpleAction.new(name, None)
            act.connect("activate", lambda _a, _p, f=fn: f(None))
            group.add_action(act)
            self._actions[name] = fn
        # R8-B: the aspect lock is STATEFUL — the palette surface (the "win"
        # group this window enumerates) must be able to show what the setting
        # says, in both directions, so a plain activate-callback is not
        # enough.  Seeded from the setting, mirrored back on every change
        # (see _sync_aspect_action); it is deliberately NOT in the `simple`
        # dict, which builds parameterless stateless actions.
        # parameter_type is deliberately None: with a boolean STATE and no
        # parameter, GAction's own toggle convenience applies, so a bare
        # `win.toggle-aspect-lock` activation flips it (measured: declaring a
        # "b" parameter type makes GTK refuse the parameterless activation
        # with g_simple_action_activate's assertion — a palette entry that
        # cannot be activated is not a palette entry).
        lock = make_aspect_lock_action(
            bool(self.cfg.get("ui.lock_aspect", True)))
        lock.connect("change-state", self._act_aspect_lock)
        group.add_action(lock)
        self._aspect_lock_action = lock
        # R11: crop-to-window joins the palette as a STATEFUL boolean —
        # same measured construction (make_crop_action) and the same ONE
        # writer discipline; it toggles straight from a palette entry.
        self._crop_on = bool(self.cfg.get("video.crop_to_window", False))
        crop = make_crop_action(self._crop_on)
        crop.connect("change-state", self._act_crop)
        group.add_action(crop)
        self._crop_action = crop

    # --------------------------------------------------- §7 T1 menu popovers
    @staticmethod
    def _clear(lb) -> None:
        child = lb.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            lb.remove(child)
            child = nxt

    def _scrolled(self, lb) -> Gtk.ScrolledWindow:
        sw = Gtk.ScrolledWindow()
        sw.set_child(lb)
        sw.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        sw.set_max_content_height(420)
        sw.set_propagate_natural_height(True)
        return sw

    def _row(self, text: str, on_activate=None, insensitive=False):
        lbl = Gtk.Label(label=text[:90])
        lbl.set_xalign(0.0)
        row = Gtk.ListBoxRow()
        row.set_child(lbl)
        row._act = on_activate                      # noqa: SLF001
        if insensitive:
            row.set_sensitive(False)
        return row

    def _popover_of(self, child) -> Gtk.Popover:
        pop = Gtk.Popover()
        pop.set_child(child)
        # R8-A: with the top menu bar deleted there is no launcher button
        # — these popovers hang off the video area (same parent pattern as
        # _ctx_pop) so the playlist/chapters/tracks row lists stay real,
        # mapped widgets the snapshot keeps populated (and a popover open
        # still holds the chrome revealed — same rule as the context menu).
        pop.set_parent(self.view.area)
        # R5-A: an open popover must hold the chrome (R12: the top strip
        # too — it fades as one group) revealed — retracting its anchor
        # would strand the menu — and the idle timer re-arms when it
        # closes.
        pop.connect("notify::visible", self._on_popover_visible)
        return pop

    def _on_popover_visible(self, pop, _pspec) -> None:
        if pop.get_visible():
            self.chrome.menu_opened()
        else:
            self.chrome.menu_closed()

    def _activated(self, _lb, row) -> None:
        act = getattr(row, "_act", None)
        if act is not None:
            act()

    def _build_popovers(self) -> None:
        # ---- playlist: toolbar + rows (§7 playlist row) ----
        self._pl_list = Gtk.ListBox()
        self._pl_list.connect("row-activated", self._activated)
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        for label, tip, fn in (
                ("+file", "Add file(s)", self._pl_add_files),
                ("+dir", "Add folder", self._pl_add_folder),
                ("m3u…", "Load .m3u", self._pl_open_m3u),
                ("save", "Save .m3u", self._pl_save_m3u),
                ("⇄", "Shuffle",
                 lambda: self.core.command("playlist-shuffle")),
                ("⌫", "Remove current", self._pl_remove_current),
                ("↑", "Move up", lambda: self._pl_move(-1)),
                ("↓", "Move down", lambda: self._pl_move(+1)),
        ):
            b = Gtk.Button(label=label)
            b.set_tooltip_text(tip)
            b.connect("clicked", lambda _b, f=fn: f())
            bar.append(b)
        pbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        pbox.append(bar)
        pbox.append(self._scrolled(self._pl_list))
        self._pl_pop = self._popover_of(pbox)
        # ---- chapters (§7 chapter list + next/prev) ----
        self._ch_list = Gtk.ListBox()
        self._ch_list.connect("row-activated", self._activated)
        self._ch_pop = self._popover_of(self._scrolled(self._ch_list))
        # ---- tracks (+ external files, §7) ----
        self._tr_list = Gtk.ListBox()
        self._tr_list.connect("row-activated", self._activated)
        tbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        for label, tip, kind in (
                ("sub+", "Add external subtitle file", "sub"),
                ("aud+", "Add external audio file", "audio"),
        ):
            b = Gtk.Button(label=label)
            b.set_tooltip_text(tip)
            b.connect("clicked", lambda _b, k=kind: self._pick_track_file(k))
            tbar.append(b)
        tbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        tbox.append(tbar)
        tbox.append(self._scrolled(self._tr_list))
        self._tr_pop = self._popover_of(tbox)

    # -- popover content refresh (snapshot-driven; change-detected) --------
    def _pl_rows(self):
        pl = self.core.snapshot.get("playlist")
        return pl if isinstance(pl, list) else []

    def _pl_current_idx(self):
        for i, r in enumerate(self._pl_rows()):
            if isinstance(r, dict) and r.get("current"):
                return i
        return None

    def _fill_playlist(self) -> None:
        self._clear(self._pl_list)
        rows = self._pl_rows()
        if not rows:
            self._pl_list.append(self._row("(empty)", insensitive=True))
            return
        for i, r in enumerate(rows):
            name = (r.get("title") or r.get("filename") or "?")
            self._pl_list.append(self._row(
                f"{'▶' if r.get('current') else ' '} {i:3d}  {name}",
                lambda i=i: self.core.command("set", "playlist-index",
                                             str(i))))

    def _fill_chapters(self) -> None:
        self._clear(self._ch_list)
        chs = self.core.snapshot.get("chapter-list")
        if not isinstance(chs, list) or not chs:
            self._ch_list.append(self._row("(no chapters)", insensitive=True))
            return
        for i, ch in enumerate(chs):
            if not isinstance(ch, dict):
                continue
            title = ch.get("title") or f"Chapter {i + 1}"
            self._ch_list.append(self._row(
                f"{chrome.fmt_time(ch.get('time') or 0.0)}  {title}",
                lambda i=i: self.core.command("set", "chapter", str(i))))

    def _fill_tracks(self) -> None:
        self._clear(self._tr_list)
        tl = self.core.snapshot.get("track-list")
        if not isinstance(tl, list) or not tl:
            self._tr_list.append(self._row("(no tracks yet)",
                                           insensitive=True))
            return
        prop = {"video": "vid", "audio": "aid", "sub": "sid"}
        for t in tl:
            if not isinstance(t, dict):
                continue
            kind = t.get("type") or "?"
            # R6-A: formatting shared with the context menu (ONE source)
            self._tr_list.append(self._row(
                ctxmenu.track_label(t),
                lambda kind=kind, t=t: self.core.command(
                    "set", prop.get(kind, kind),
                    "no" if t.get("selected") else str(t.get("id")))))
        for kind, label in (("video", "no video (audio only)"),
                            ("audio", "no audio"), ("sub", "no subtitles")):
            self._tr_list.append(self._row(
                f"{'●' if self.core.snapshot.get(prop[kind]) in (False, 'no') else '○'}  {label}",
                lambda kind=kind: self.core.command("set", prop[kind],
                                                    "no")))

    def _track_sel(self, tl):
        out = {}
        for t in tl:
            if isinstance(t, dict) and t.get("selected"):
                out[t.get("type")] = (t.get("id"), t.get("title"),
                                      t.get("lang"))
        return out

    def _sync_menus(self) -> None:
        """Snapshot diff → rebuild popovers + OSD on chapter/track change."""
        if self._pl_list is None:
            return
        pl = self.core.snapshot.get("playlist")
        if isinstance(pl, list):
            sig = tuple((r.get("filename"), bool(r.get("current")))
                        for r in pl if isinstance(r, dict))
            if sig != self._pl_sig:
                self._pl_sig = sig
                self._fill_playlist()
        chs = self.core.snapshot.get("chapter-list")
        if isinstance(chs, list):
            sig = tuple((c.get("title"), c.get("time"))
                        for c in chs if isinstance(c, dict))
            if sig != self._ch_sig:
                self._ch_sig = sig
                self._fill_chapters()
        idx = self.core.snapshot.get("chapter")
        if isinstance(idx, int):
            if self._chapter_idx is not None and idx != self._chapter_idx:
                title = ""
                if isinstance(chs, list) and 0 <= idx < len(chs):
                    title = chs[idx].get("title") or ""
                self.chrome.show_osd(
                    f"Chapter {idx + 1}: {title}" if title
                    else f"Chapter {idx + 1}")
            self._chapter_idx = idx
        tl = self.core.snapshot.get("track-list")
        if isinstance(tl, list):
            sel = self._track_sel(tl)
            sig = tuple(sorted((str(k), str(v)) for k, v in sel.items()))
            if sig != self._track_sig:
                if self._track_sig is not None:
                    prev = getattr(self, "_track_prev", {}) or {}
                    for kind in sorted(prev.keys() | sel.keys()):
                        old, new = prev.get(kind), sel.get(kind)
                        if old == new:
                            continue
                        if new is None:
                            self.chrome.show_osd(f"{kind} track: off")
                        else:
                            label = new[1] or (f"[{new[2]}]" if new[2]
                                               else f"#{new[0]}")
                            self.chrome.show_osd(f"{kind} track: {label}")
                self._track_prev = sel
                self._track_sig = sig
                self._fill_tracks()
        # R6-A: the context-menu Repeat radio follows the SAME mpv loop
        # truth — raw "l"/"r" keys move the action state, and the popover
        # repaints its check mark from the action observer.
        self._sync_repeat()

    def _sync_repeat(self) -> None:
        """R9-A: the single place an OBSERVED loop change becomes UI +
        config state (called from the snapshot listener `_sync_menus`).

        The native `l`/`r` keys (`_act_loop_file`/`_act_loop_playlist`)
        keep mpv's own `cycle` semantics and never pass through
        `_set_repeat_mode` — the snapshot is where EVERY loop-property
        change lands (the main thread may not read a sync getter, R9),
        so persisting HERE closes that hole for every current or future
        path.  Config stores the canonical tri-state value via
        `repeat_props`: an mpv count ("1", "2", …) IS "one"/"all" and
        persists as "inf".  `_set_repeat_mode` pre-sets `_rep_mode`, so
        its own changes never double-write here; an unrecognised stored
        value maps to "off" (repeat_mode → _mpv_loop_on) and, being the
        resting mode, rewrites NOTHING."""
        mode = chrome.repeat_mode(self.core.snapshot.get("loop-file"),
                                  self.core.snapshot.get("loop-playlist"))
        if mode == self._rep_mode:
            return
        self._rep_mode = mode
        self._ctx_repeat.set_state(GLib.Variant("s", mode))
        loop_file, loop_playlist = chrome.repeat_props(mode)
        self.cfg.set("playback.loop_file", loop_file)
        self.cfg.set("playback.loop_playlist", loop_playlist)

    # ------------------------------------------------- playlist operations --
    def _pl_add_files(self) -> None:
        Gtk.FileDialog().open_multiple(self, None, self._on_files_chosen)

    def _on_files_chosen(self, dlg, res, _u=None) -> None:
        try:
            model = dlg.open_multiple_finish(res)
        except GLib.Error:
            return
        paths = []
        for i in range(model.get_n_items()):
            f = model.get_item(i)
            p = f.get_path() or f.get_uri()
            if p:
                paths.append(p)
        for p in paths:
            self._load_path(p, "append")
            if self.playlist is not None:
                self.playlist.append(p)
        if paths:
            self.chrome.show_toast(f"Added {len(paths)} file(s)")

    def _pl_add_folder(self) -> None:
        Gtk.FileDialog().select_folder(self, None, self._on_folder_picked)

    def _on_folder_picked(self, dlg, res, _u=None) -> None:
        try:
            f = dlg.select_folder_finish(res)
        except GLib.Error:
            return
        path = f.get_path() if f is not None else None
        if not path:
            return
        known = set(self.playlist or [])
        reelsee_io.FolderScan(
            path,
            lambda files: self._append_new_files(files, known)).start()

    def _pl_open_m3u(self) -> None:
        Gtk.FileDialog().open(self, None, self._on_m3u_open)

    def _on_m3u_open(self, dlg, res, _u=None) -> None:
        try:
            f = dlg.open_finish(res)
        except GLib.Error:
            return
        if f is None:
            return
        path = f.get_path() or f.get_uri()
        self.core.command("loadlist", path, "append")
        self.chrome.show_toast(f"Loaded {os.path.basename(path)}")

    def _pl_save_m3u(self) -> None:
        urls = [r.get("filename") for r in self._pl_rows()
                if isinstance(r, dict) and r.get("filename")]
        if not urls:
            self.chrome.show_toast("Playlist is empty")
            return
        fd = Gtk.FileDialog()
        fd.set_initial_name("playlist.m3u")
        fd.save(self, None, self._on_m3u_save)

    def _on_m3u_save(self, dlg, res, _u=None) -> None:
        try:
            f = dlg.save_finish(res)
        except GLib.Error:
            return
        if f is None:
            return
        path = f.get_path() or f.get_uri()
        # mpv resolves relative m3u entries against the m3u's own
        # directory, not the CWD — always write absolute paths/URLs.
        urls = [_abs_entry(r.get("filename"))
                for r in self._pl_rows()
                if isinstance(r, dict) and r.get("filename")]
        reelsee_io.save_text_async(
            path, reelsee_io.build_m3u(urls),
            lambda ok: self.chrome.show_toast(
                "Playlist saved" if ok else "Could not save playlist"))

    def _pl_remove_current(self) -> None:
        idx = self._pl_current_idx()
        if idx is None:
            # loadlist can leave the playlist with no current entry; the
            # popover button then removes the first row (UI fallback).
            idx = 0 if self._pl_rows() else None
        if idx is not None:
            self.core.command("playlist-remove", str(idx))

    def _pl_move(self, delta: int) -> None:
        idx = self._pl_current_idx()
        n = len(self._pl_rows())
        if idx is None:
            return
        new = idx + delta
        if 0 <= new < n:
            self.core.command("playlist-move", str(idx), str(new))

    def _pick_track_file(self, kind: str) -> None:
        Gtk.FileDialog().open(self, None,
                              lambda d, r, _u=None, k=kind:
                              self._on_track_added(d, r, k))

    def _on_track_added(self, dlg, res, kind: str) -> None:
        try:
            f = dlg.open_finish(res)
        except GLib.Error:
            return
        if f is None:
            return
        path = f.get_path() or f.get_uri()
        self.core.command("sub-add" if kind == "sub" else "audio-add", path)
        self.chrome.show_toast("Added "
                               + ("subtitle" if kind == "sub" else "audio")
                               + " track")

    def _ab_hook(self, what: str, t=None) -> None:
        """`[`/`]`/Backspace + the A/B chrome buttons (§5.4, R-5).

        `t=None` → use the current observed position; smokes/tests pass an
        explicit time so the path is deterministic.
        """
        st = self.ab_state
        if what == "clear":
            st.clear()
            st.apply(self.core)
            self._ab_persist()
            self.chrome.set_ab(None, None)
            self.chrome.show_osd("A/B cleared")
            return
        if t is None:
            t = self.core.snapshot.get("time-pos")
        if t is None:
            self.chrome.show_osd("no position yet")
            return
        # R5-E: pressing A or B while a loop is ACTIVE clears the WHOLE
        # section first (mpv endpoints, persisted entry, drawn markers —
        # everything) and only then applies the pressed mark.  A B press
        # right after the clear hits the plain "Set A first" rule — no
        # partial state ever results.  One toast names what happened.
        ok, cleared, reason = AB.press_ab(
            st, "a" if what == "set_a" else "b", t)
        if cleared:
            st.apply(self.core)          # endpoints back to "no"/"no"
            self._ab_persist()           # stored entry removed
            self.chrome.set_ab(None, None)
        if not ok:
            msg = reason or "A/B: rejected"
            self.chrome.show_osd(("A/B cleared — " + msg) if cleared
                                 else msg)
            return
        st.apply(self.core)
        self._ab_persist()
        self.chrome.set_ab(st.a, st.b)
        label = "A" if what == "set_a" else "B"
        if cleared:
            self.chrome.show_osd(f"A/B cleared — new {label} set")
            return
        extra = " (B cleared)" if what == "set_a" and st.b is None else ""
        self.chrome.show_osd(f"{label} = {chrome.fmt_time(t)}{extra}")

    def _ab_persist(self) -> None:
        if not self.target:
            return
        key = str(self.target)
        if self.ab_state.active:
            self.app.ab_store.set(key, {"a": self.ab_state.a,
                                        "b": self.ab_state.b})
        else:
            self.app.ab_store.remove(key)

    def on_setting_changed(self, key, value) -> None:
        """Hook called by settings apply_live for keys no mpv property maps."""
        if key == "ab.count":
            self.ab_state.set_count(value)
            if self.ab_state.active:
                self.ab_state.apply(self.core)
        elif key == "ui.chrome_timeout_ms":
            try:
                self.chrome.state.timeout_ms = float(value)
            except (TypeError, ValueError):
                pass
        elif key == "engine.watchdog_enabled":
            self.watchdog.enabled = bool(value)
        elif key == "engine.watchdog_fps_floor_pct":
            try:
                self.watchdog.fps_floor = float(value) / 100.0
            except (TypeError, ValueError):
                pass
        elif key == "engine.watchdog_drop_ceiling_pct":
            try:
                self.watchdog.drop_ceiling = float(value) / 100.0
            except (TypeError, ValueError):
                pass
        elif key == "ui.lock_aspect":            # R8-B (interface page)
            self._sync_aspect_action(value)
            if value:
                self._aspect_realign()
            else:
                # "restore completely free resizing immediately": the guard
                # and any queued correction go away with the setting.
                self._aspect_forget()
        elif key == "video.crop_to_window":      # R11 (video page)
            # apply_live already pushed panscan via PROPERTY_MAP; route
            # through the ONE writer so actions/checkmark/OSD agree.
            self._sync_crop_action(value)
        elif key in ("ui.remember_geometry", "ui.cascade_offset_px"):
            # R10-C: intentionally NO live plumbing — both are read fresh
            # when the NEXT window opens (plan_open_geometry), which IS the
            # specified "affects the next window without restarting".
            pass
        elif key == "playback.video_off":          # §7 T1 audio-only mode
            self.core.set_property("vid", "no" if value else "auto")
        elif key == "video.scale_preset":          # §7 T1 scaler preset pack
            for prop, val in C.SCALE_PRESETS.get(str(value), {}).items():
                self.core.set_property(prop, val)

    # ---------------------------------------------------------- key handling
    def _in_text_field(self) -> bool:
        focus = self.get_focus()
        return isinstance(focus, Gtk.Text) or isinstance(focus, Gtk.Entry)

    def _on_key(self, _ctrl, keyval, keycode, state) -> bool:
        if self._in_text_field():
            return False
        name = Gdk.keyval_name(keyval)
        # R2-P3: Escape CANCELS an in-flight URL resolve before the
        # keymap gets a chance to run its "exit" binding on it.
        if name == "Escape" and self._cancel_resolve():
            return True
        ev = keymap.event_string(name, bool(state & Gdk.ModifierType.SHIFT_MASK),
                                 bool(state & Gdk.ModifierType.CONTROL_MASK),
                                 bool(state & Gdk.ModifierType.ALT_MASK),
                                 bool(state & Gdk.ModifierType.META_MASK))
        # event_string canonicalises single chars UPPERCASE ("m" → "M");
        # DEFAULT_BINDINGS are lowercase → case-insensitive second pass.
        binding = self.keymap.get(ev) if ev else None
        if binding is None and ev and ev != ev.lower():
            binding = self.keymap.get(ev.lower())
        if binding is None:
            return False
        if isinstance(binding, tuple) and binding[0] == "mpv-command":
            self.core.command(*binding[1])
            return True
        direction = -1.0 if ("left" in (ev or "") or "down" in (ev or "")) else 1.0
        fn = self._actions.get(binding)
        if fn is None:
            return False
        self.chrome.key()          # never re-arms the chrome timer (§4.6)
        try:
            fn(direction)
        except Exception:
            log.exception("action %s failed", binding)
        return True

    # ------------------------------------------------------------- handlers
    def _act_play_pause(self, _p=None) -> None:
        paused = bool(self.core.snapshot.get("pause"))
        self.core.set_property("pause", not paused)
        self.chrome.show_osd("pause" if not paused else "play")

    def _act_seek(self, direction, size: str) -> None:
        sign = 1.0 if (direction is None or float(direction) >= 0) else -1.0
        step = float(self.cfg.get(
            "ui.seek_step_long_s" if size == "long" else "ui.seek_step_s",
            30.0 if size == "long" else 5.0))
        self.core.command("seek", str(step * sign), "relative")
        self.chrome.keyboard_seek_osd(self.core.snapshot)

    def _act_volume(self, sign: float) -> None:
        step = 5.0                      # §4.7: ↑/↓ volume ±5
        cur = float(self.core.snapshot.get("volume") or 0.0)
        new = max(0.0, min(200.0, cur + sign * step))
        self.core.set_property("volume", new)
        self.cfg.set("audio.volume", new)
        self.chrome.show_osd(f"Volume {new:.0f}")

    def _act_mute(self, _p=None) -> None:
        # R6-B: an explicit mute choice wins over the transient
        # focus-mute (audio.focus_mute is a listening courtesy, not a
        # user decision) — and the toggle flips the PERSISTED value
        # (the binding is audio.muted), never the transient OR-ed flag.
        self._transient_muted = False
        new = not bool(self.cfg.get("audio.muted", False))
        self.core.set_property("mute", new)
        self.cfg.set("audio.muted", new)   # immediate persist (schema §5.2)
        self.chrome.show_osd("muted" if new else "unmuted")

    def _act_fullscreen(self, _p=None) -> None:
        # Own flag: X11 without a window manager never sends the state notify
        # back, so is_fullscreen() would stay False under Xvfb (P2 finding).
        if self._fs:
            self.unfullscreen()
            self._fs = False
        else:
            self.fullscreen()
            self._fs = True
        # R8-B: the geometry of a fullscreen transition belongs to the
        # transition, not to us — GTK hands back a whole storm of layout
        # events on both edges, and correcting any of them is exactly how a
        # window ends up at a size nobody asked for.  Settle first, then the
        # normal rules apply again (the size we had before fullscreen is
        # restored untouched, and if it is off-aspect the next drag fixes it).
        self._aspect_forget()
        self._aspect_free_until = time.monotonic() + 0.4
        # R5-D: draggability exists only while windowed (see _set_handle_active).
        self._set_handle_active(not self._fs)
        # R5-A: both edges re-arm the countdown — fullscreen retracts through
        # the SAME idle mechanism, and leaving fullscreen restores revealed
        # (the fresh wake-up reveals immediately via the normal tick).
        self.chrome.wake()
        self._apply_reveal(True)
        self._update_title()

    def _act_fullscreen_exit(self, _p=None) -> None:
        if self._fs or self.is_fullscreen():
            self.unfullscreen()
            self._set_handle_active(True)         # R5-D: drag back on
            self._fs = False
            # R8-B: the RESTORE storm is the dangerous edge — this is where a
            # naive lock snaps a 1920x1080 window down to 450x800 because it
            # still believes the pre-fullscreen size was the video's box.
            self._aspect_forget()
            self._aspect_free_until = time.monotonic() + 0.4
            self.chrome.wake()           # R5-A: restored chrome is revealed
            self._apply_reveal(True)
            self._update_title()

    def _act_fullscreen_state(self) -> bool:
        return bool(self._fs or self.is_fullscreen())

    def _act_stop(self, _p=None) -> None:
        self.core.command("stop")
        self.chrome.show_osd("stopped")

    def _repeat_cycle(self) -> None:
        """R5-C: repeat button — off -> all -> one -> off, on top of the
        very mpv properties the l/r keys cycle (never a parallel truth).
        R6-A: the mode write itself now lives in _set_repeat_mode, shared
        with the context-menu radio."""
        snap = self.core.snapshot
        mode = chrome.repeat_mode(snap.get("loop-file"),
                                  snap.get("loop-playlist"))
        self._set_repeat_mode(chrome.REPEAT_NEXT.get(mode, "off"))

    def _act_loop_file(self, _p=None) -> None:
        """Native `l` semantics KEPT (mpv's own cycle: no → 1 → 2 → …;
        smoke_repeat pins it).  R9-A: its PERSISTENCE is not here — every
        property change lands in the snapshot, so _sync_repeat is the one
        place that writes config for this path (no sync read allowed on
        the main thread, R9 — a snapshot-based "cycle then persist" here
        would have to race the async reply)."""
        self.core.command("cycle", "loop-file")
        self.chrome.show_osd("file loop: cycled (no → inf → N)")

    def _act_loop_playlist(self, _p=None) -> None:
        """Native `r` semantics KEPT; persistence via _sync_repeat
        (snapshot = the one landing point of every loop change)."""
        self.core.command("cycle", "loop-playlist")
        self.chrome.show_osd("playlist loop: cycled")

    def _act_screenshot(self, _p=None) -> None:
        self.core.command("screenshot")
        self.chrome.show_osd("screenshot saved")
        if bool(self.cfg.get("ui.screenshot_clipboard", False)):
            d = os.path.expanduser(str(self.cfg.get("screenshot.directory",
                                                    "~/Pictures")))
            GLib.timeout_add(
                300, lambda: (reelsee_io.NewestFileFinder(
                    d, ("mpv-shot",), (".png", ".jpg", ".jpeg"),
                    self._on_shot_file).start(), False)[1])

    def _on_shot_file(self, path) -> None:
        if self._closing:
            return
        if not path:
            self.chrome.show_toast("Screenshot for clipboard not found")
            return
        Gio.File.new_for_path(path).load_bytes_async(
            None, lambda src, res, _u=None: self._on_shot_bytes(src, res))

    def _on_shot_bytes(self, src, res) -> None:
        if self._closing:
            return
        try:
            data, _etag = src.load_bytes_finish(res)
            tex = Gdk.Texture.new_from_bytes(data)
            # P11 probe: this GTK build has no Gdk.Clipboard.set_texture;
            # a texture ContentProvider is the supported equivalent.
            clip = Gdk.Display.get_default().get_clipboard()
            clip.set_content(Gdk.ContentProvider.new_for_value(tex))
            self.chrome.show_toast("Screenshot copied to clipboard")
        except (GLib.Error, Exception):         # noqa: BLE001
            log.exception("clipboard copy failed")
            self.chrome.show_toast("Clipboard copy failed")

    def _act_speed(self, delta: float) -> None:
        cur = float(self.core.snapshot.get("speed") or 1.0)
        new = max(0.1, min(4.0, cur + delta))    # §7: 0.1–4.0
        self.core.set_property("speed", new)
        self.chrome.keyboard_seek_osd({**self.core.snapshot, "speed": new})

    # ---------------------------------------------------- chrome callbacks
    def _chrome_seek(self, seconds: float) -> None:
        self.core.command("set", "time-pos", f"{seconds:.3f}")

    def _chrome_volume(self, value: float) -> None:
        self.core.set_property("volume", value)
        self.cfg.set("audio.volume", value)

    def _chrome_speed(self, value: float) -> None:
        self.core.set_property("speed", value)
        self.cfg.set("playback.speed", value)
        self.chrome.keyboard_seek_osd({**self.core.snapshot, "speed": value})
