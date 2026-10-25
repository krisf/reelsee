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
import chrome
import config as C
import ctxmenu
import engine
import keymap
import mpvlib as M
import reelsee_io
import watchdog as WD
from mpvcore import MpvCore
from render import RenderView

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Pango", "1.0")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango  # noqa: E402

log = logging.getLogger("reelsee.window")

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
}


def _abs_entry(u):
    """m3u entries must survive the playlist being saved anywhere: mpv
    resolves relative paths against the m3u file's directory (§P11)."""
    if not u or reelsee_io.is_url(u):
        return u
    return os.path.abspath(u)


class ReelseeWindow(Adw.ApplicationWindow):
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

        # R5-A: the ENTIRE chrome retracts — real titlebar (min/max/close)
        # included.  Adw.ApplicationWindow + Adw.ToolbarView with an
        # Adw.HeaderBar top bar (window controls + centred, ellipsized
        # filename as title_widget); ToolbarView.set_reveal_top_bars() is
        # THE one and only retraction animation (Looksee mechanism) — no
        # hand-rolled opacity on the title bar.  The GLArea + chrome
        # overlays live in the ToolbarView content slot.
        self._title_label = Gtk.Label(label="REELSEE", xalign=0.5)
        self._title_label.set_ellipsize(Pango.EllipsizeMode.END)
        self._title_label.set_max_width_chars(48)   # never width-chars
        self.header = Adw.HeaderBar(title_widget=self._title_label)
        self.header.add_css_class("reelsee-black")
        # start/end title buttons (min/max/close) show by default; the
        # decoration layout is pinned app-wide in reelsee.run() (R5-A).
        self._toolbar = Adw.ToolbarView()
        self._toolbar.add_top_bar(self.header)
        self._toolbar.set_content(self.overlay)
        self.set_content(self._toolbar)

        self.chrome = chrome.Chrome(
            self, config,
            on_seek=self._chrome_seek, on_volume=self._chrome_volume,
            on_speed=self._chrome_speed,
            on_ab=lambda which: self._ab_hook("set_a" if which == "a"
                                              else "set_b"),
            on_repeat=self._repeat_cycle,
            on_decode=self._toggle_decode)   # R7-B: the decode toggle
        for c in self.chrome.overlay_children():
            # R5-B: ui.show_top_menu off ⇒ the in-video menu bar (playlist/
            # chapters/tracks buttons + filename label) is NOT in the widget
            # tree at all — not merely hidden.
            if c is self.chrome.top and not bool(
                    config.get("ui.show_top_menu", True)):
                continue
            self.overlay.add_overlay(c)
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

        self.set_default_size(960, 540)
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
        """Fresh snapshot-derived model, popped up AT the pointer.  The
        ui.show_top_menu setting has zero influence: this path never
        touches chrome.top (it exists or it does not — the menu is the
        same object either way)."""
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
        routes to the SAME command the top-menu popover or the keymap
        already issues — this wires, it never reimplements."""
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
        self._ctx_repeat = Gio.SimpleAction.new_stateful(
            "repeat", GLib.VariantType("s"), GLib.Variant("s", "off"))
        self._ctx_repeat.connect(
            "change-state",
            # NOTE: Variant.get_string() here returns the str directly,
            # NOT the (str, consumed) tuple — get_string()[0] would
            # slice "one" to "o" (probe-proven).  unpack() is unambiguous.
            lambda a, v: self._set_repeat_mode(v.unpack(), action=a))
        ag.add_action(self._ctx_repeat)
        self._rep_mode = "off"
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
        the truth stays the two mpv loop properties (R5-C contract)."""
        loop_file, loop_playlist = chrome.repeat_props(mode)
        self.core.command("set", "loop-file", loop_file)
        self.core.command("set", "loop-playlist", loop_playlist)
        if action is not None:
            action.set_state(GLib.Variant("s", mode))
        self._rep_mode = mode
        self.chrome.set_repeat(mode)     # immediate feedback (pre-snapshot)
        self.chrome.show_osd("Repeat: " + mode)

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

    def _apply_reveal(self, visible: bool) -> None:
        """R5-A: THE single retraction mechanism for the real titlebar —
        Adw.ToolbarView's own reveal transition, driven by the same
        ChromeState decision that fades the in-content bars.  No second
        hand-rolled animation anywhere (Looksee pattern)."""
        self._toolbar.set_reveal_top_bars(bool(visible))

    def _apply_top_menu_setting(self, on: bool) -> None:
        """R5-B: ui.show_top_menu — off means the in-video menu bar (the
        playlist/chapters/tracks buttons + filename label, i.e. the whole
        Chrome.top) is REMOVED from the widget tree — it takes no space,
        receives no clicks, cannot be tab-focused — and back into the tree
        on a live toggle to on.  The headerbar title (R5-A) keeps the
        filename, and every action keeps its existing shortcut
        (a/v subs+audio, l/r loops, b/n playlist, PgUp/PgDn chapters)."""
        top = self.chrome.top
        parent = top.get_parent()
        if on:
            if parent is None:
                self.overlay.add_overlay(top)
                top.set_child_visible(self.chrome.bars_visible)
        elif parent is not None:
            if isinstance(parent, Gtk.Overlay):
                parent.remove_overlay(top)   # GtkOverlay has no remove()
            else:
                parent.remove(top)

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
        # R5-A: the headerbar's centred title_widget shows the FILENAME —
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
        self._closing = True
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
        # R5-A: an open popover must hold the chrome (and the headerbar)
        # revealed — retracting its anchor would strand the menu — and the
        # idle timer re-arms when it closes.
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
        self.chrome.add_top_menu("playlist",
                                 "Playlist  (b/n prev/next)"
                                 ).set_popover(self._pl_pop)
        # ---- chapters (§7 chapter list + next/prev) ----
        self._ch_list = Gtk.ListBox()
        self._ch_list.connect("row-activated", self._activated)
        self._ch_pop = self._popover_of(self._scrolled(self._ch_list))
        self.chrome.add_top_menu("chapters",
                                 "Chapters  (PgUp/PgDn)"
                                 ).set_popover(self._ch_pop)
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
        self.chrome.add_top_menu("tracks",
                                 "Audio / video / subtitle tracks"
                                 ).set_popover(self._tr_pop)

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
        mode = chrome.repeat_mode(self.core.snapshot.get("loop-file"),
                                  self.core.snapshot.get("loop-playlist"))
        if mode != self._rep_mode:
            self._rep_mode = mode
            self._ctx_repeat.set_state(GLib.Variant("s", mode))

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
        elif key == "ui.show_top_menu":
            self._apply_top_menu_setting(bool(value))
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
        self.core.command("cycle", "loop-file")
        self.chrome.show_osd("file loop: cycled (no → inf → N)")

    def _act_loop_playlist(self, _p=None) -> None:
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
