"""ReelseeWindow — one top-level window: Overlay(GLArea) + per-window core.

The main thread only ever READS core.snapshot (R9) and writes via
core.command / core.set_property (async mpv calls).  Teardown order follows
§0.6/R11: the render context is detached+freed (GL current) while the window
closes; mpv_terminate_destroy happens later on a worker.
"""
from __future__ import annotations

import logging
import math
import os
import threading
import time

import gi

import ab as AB
import aspect
import cadence
import chrome
import config as C
import ctxmenu
import engine
import keymap
import mpvlib as M
import reelsee_io
import tickdrive
import vidsup
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


# ---------------------------------------------------------------- R14-A menu
# GTK's own CSD titlebar gesture path ends at
# `Gdk.Toplevel.show_window_menu` (on X11 it sends _GTK_SHOW_WINDOW_MENU and
# mutter pops the WM window menu — the R14 defect).  The real fix is
# structural (R14-A: the stub titlebar cannot be a target, and the window
# itself claims button-3 during CAPTURE, so no code path reaches GTK's
# menu).  As belt-and-braces the call itself is refused at this seam: once
# this module is imported, a Python-level call to show_window_menu is a
# logged no-op returning False.  (A C-internal call from inside GTK would
# bypass a Python patch — which is exactly why the structural claim, not
# this refusal, is what kills the menu; see DSH-REPORT-R14.)
_SHOW_MENU_REFUSED_LOGGED = False


def _refused_show_window_menu(self, *_a, **_k) -> bool:
    global _SHOW_MENU_REFUSED_LOGGED
    if not _SHOW_MENU_REFUSED_LOGGED:
        _SHOW_MENU_REFUSED_LOGGED = True
        log.debug("R14-A: refused Gdk.Toplevel.show_window_menu "
                  "(ours is the only right-click menu)")
    return False


if not getattr(Gdk.Toplevel, "_reelsee_menu_refused", False):
    Gdk.Toplevel.show_window_menu = _refused_show_window_menu
    Gdk.Toplevel._reelsee_menu_refused = True


# ------------------------------------------------- R17-A C titlebar gestures
# WHY this block exists (operator-confirmed on the real GNOME/XWayland
# desktop; reproduced at WIRE level in scratch/r17/probe_wm_menu6.py):
# `Gtk.WindowHandle` installs its OWN gestures in C at construction — a
# `Gtk.GestureClick` (button 0 = ANY button, bubble phase) plus a
# `Gtk.GestureDrag`.  That click gesture implements the three
# `gtk-titlebar-*-click` settings, and for button 3 its C code calls
# `gdk_toplevel_show_window_menu()`; on X11 that sends the
# `_GTK_SHOW_WINDOW_MENU` client message and mutter pops the WM window
# menu — the R14/R17 defect.  Two consequences, both measured here:
#
#   * R14's Python monkeypatch of `Gdk.Toplevel.show_window_menu` can
#     NEVER run on that path: the C code dispatches through the vfunc
#     (`GDK_TOPLEVEL_GET_IFACE(toplevel)->show_window_menu`), which never
#     performs a Python attribute lookup.  R14's own gate spy therefore
#     counted 0 SPURIOUSLY — and Xvfb has no window manager, so even a
#     real `_GTK_SHOW_WINDOW_MENU` message vanishes into the void there.
#   * `gtk-titlebar-right-click` is consulted per click, and GNOME's
#     xsettings daemon re-pushes `Gtk/TitlebarRightClick` to the client at
#     any time, so R5-D's startup-only "none" loses on a real desktop.
#
# The fix is structural and cannot be routed around: the C gesture is a
# real `Gtk.GestureClick` visible in `observe_controllers()`, so it can be
# `remove_controller()`d.  Probe-proven: with a WM advertising
# `_GTK_SHOW_WINDOW_MENU` in `_NET_SUPPORTED`, right-pressing a plain
# `Gtk.WindowHandle` puts 1 window-menu client message on the wire while
# the C click gesture is present and 0 after removing exactly that one
# controller — the `Gtk.GestureDrag` (drag-anywhere R5-D, header drag
# R15-A) survives untouched.

#: The three `GtkSettings` keys GTK's C titlebar click gesture consults.
#: They are ENUM-typed strings ('none' | 'menu' | 'maximize' | 'shade'),
#: NOT booleans — `False` is not a valid value on this build (probe:
#: `Gtk.Settings.get_property("gtk-titlebar-right-click")` returns the
#: STRING 'menu' by default), so 'none' is the value that means "off".
TITLEBAR_CLICK_SETTINGS = ("gtk-titlebar-double-click",
                           "gtk-titlebar-middle-click",
                           "gtk-titlebar-right-click")
TITLEBAR_CLICK_OFF = "none"


def describe_controllers(widget) -> list[str]:
    """Controller inventory of `widget`, naming the two properties that
    identify GTK's pre-installed titlebar gestures (button + phase).

    The phase is what tells the app's OWN capture-phase `Gtk.GestureDrag`
    apart from the `Gtk.GestureDrag` a `Gtk.WindowHandle` pre-installs in
    C (probe: fresh handle = `GestureDrag(button=1,phase=bubble)` +
    `GestureClick(button=0,phase=bubble)`), so the gate can assert on the
    exact set instead of a bare class name."""
    out = []
    for c in widget.observe_controllers():
        name = type(c).__name__
        if isinstance(c, (Gtk.GestureClick, Gtk.GestureDrag)):
            name += (f"(button={c.get_button()}"
                     f",phase={c.get_propagation_phase().value_nick})")
        out.append(name)
    return out


def strip_handle_click_gestures(widget, what: str) -> dict:
    """R17-A: remove every `Gtk.GestureClick` GTK pre-installed on a
    `Gtk.WindowHandle` (the C titlebar click gesture — see the block
    above), and return an audit dict for the gate to assert on.

    The `Gtk.GestureDrag` is KEPT on purpose: drag-anywhere (R5-D), header
    drag (R15-A) and "a drag release never reaches the click gesture, so a
    drag never toggles pause" all depend on it.

    `before`/`after` are the GestureClick counts around the removal; a
    `before` of 0 means GTK stopped pre-installing the gesture (a GTK
    change the gate must notice, not silently pass over).
    """
    before = [c for c in widget.observe_controllers()
              if isinstance(c, Gtk.GestureClick)]
    audit = {"what": what, "before": len(before),
             "kinds_before": describe_controllers(widget)}
    for c in before:
        widget.remove_controller(c)
    after = [c for c in widget.observe_controllers()
             if isinstance(c, Gtk.GestureClick)]
    audit["after"] = len(after)
    audit["kinds_after"] = describe_controllers(widget)
    audit["drag_kept"] = any(isinstance(c, Gtk.GestureDrag)
                             for c in widget.observe_controllers())
    log.debug("R17-A: %s handle click gestures before=%d after=%d "
              "(%s -> %s)", what, audit["before"], audit["after"],
              audit["kinds_before"], audit["kinds_after"])
    return audit


_titlebar_reapplying = False
#: `notify::` handler ids installed by `harden_titlebar_click_settings()`.
#: Exposed (not private) so a gate can disconnect them, drive GTK's C
#: titlebar path with the setting switched back to 'menu', and prove the
#: STRUCTURAL removal — not the pin — is what keeps `_GTK_SHOW_WINDOW_MENU`
#: off the wire; the gate then calls `harden_titlebar_click_settings()`
#: again, which re-pins and re-installs the watches.
TITLEBAR_PIN_IDS: list[int] = []


def _force_titlebar_off(settings, prop: str) -> None:
    """Write 'none' to one titlebar-click setting unless it already says
    so; the reentrancy flag stops our own write from re-entering the
    notify handler."""
    global _titlebar_reapplying
    if _titlebar_reapplying:
        return
    try:
        if settings.get_property(prop) == TITLEBAR_CLICK_OFF:
            return
        _titlebar_reapplying = True
        settings.set_property(prop, TITLEBAR_CLICK_OFF)
    except Exception as exc:                        # noqa: BLE001
        log.debug("R17-A: could not pin %s: %s", prop, exc)
    finally:
        _titlebar_reapplying = False


def harden_titlebar_click_settings() -> bool:
    """Belt-and-braces (the structural removal above is the real fix): pin
    all three `gtk-titlebar-*-click` settings to 'none' on the default
    screen, and RE-PIN them on every notify — GNOME's xsettings daemon
    pushes `Gtk/TitlebarRightClick` back to the client at any time, which
    is exactly how R5-D's startup-only setting lost on the real desktop.
    Returns False when there is no Gtk.Settings (no display).  Safe to call
    more than once: the watches are (re)installed only when none are live,
    so a gate that disconnected them gets them back."""
    settings = Gtk.Settings.get_default()
    if settings is None:
        return False
    for prop in TITLEBAR_CLICK_SETTINGS:
        _force_titlebar_off(settings, prop)
    if not TITLEBAR_PIN_IDS:
        for prop in TITLEBAR_CLICK_SETTINGS:
            try:
                TITLEBAR_PIN_IDS.append(settings.connect(
                    f"notify::{prop}",
                    lambda _s, _p, k=prop: _force_titlebar_off(settings, k)))
            except Exception as exc:                # noqa: BLE001
                log.debug("R17-A: no notify watch for %s: %s", prop, exc)
    return True


#: Floor for the drag distance that starts a window move (R17-C).  A click
#: must never be mistaken for a move: `Gtk.GestureDrag::drag-begin` fires on
#: the PRESS on this GTK build (measured — see _on_move_drag_begin), so the
#: move has to wait for real displacement.
MOVE_DRAG_MIN_PX = 4.0


def _drag_threshold_px() -> float:
    """How far the pointer must travel before an app-owned drag starts a
    window move: the desktop's own `gtk-dnd-drag-threshold` (8 px on this
    build), floored at MOVE_DRAG_MIN_PX so a missing or mis-set setting
    cannot turn every click into a move."""
    px = 0
    try:
        st = Gtk.Settings.get_default()
        if st is not None:
            px = int(st.get_property("gtk-dnd-drag-threshold"))
    except Exception as exc:                        # noqa: BLE001
        log.debug("R17-C: gtk-dnd-drag-threshold unreadable (%s)", exc)
    if px <= 0:
        px = 8
    return max(MOVE_DRAG_MIN_PX, float(px))


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
    # R16-B: the EFFECTIVE video-track state ("auto"/"no"/track id).  The
    # hidden-video suppression writes `vid` and the gate must assert what
    # mpv actually holds, not what the app intended to write — so it is
    # observed like everything else (R9: no sync getter).
    "vid": M.MPV_FORMAT_STRING,
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
        # R16-A: the adaptive tick driver (src/tickdrive.py).  Created with
        # the sources — everything that can happen before then is a
        # construction-time no-op for the ladder (see _tick_arm).
        self._driver: tickdrive.TickDriver | None = None
        self._tick_pin = cadence.pin_from_env(os.environ)
        # R16-B: hidden-video suppression.  The POLICY is pure (src/vidsup.py,
        # unit-tested as a truth table); this side is plumbing: one `vid`
        # writer plus a `notify::mapped` handler.  `_vid_release` is kept as
        # the gate-visible trace of what an unmap stopped.
        self._vidsup = vidsup.VidSuppression(
            bool(config.get("playback.suppress_video_when_hidden", True)))
        self._vid_release: str | None = None
        # R16-C: the thread count already handed to this window (None = never
        # written, i.e. lavc's own default is still in force).
        self._lavc_threads: str | None = None
        # R16-C: last OBSERVED hwdec-current (the edge that recomputes the
        # budget; None until the first frame decodes).
        self._hwdec_seen = None
        # R16-D: focus recency (the app bumps it on every focus-in).  The
        # decode plan demotes the window that gained focus LAST, so "last"
        # needs a total order, not a timestamp.
        self._focus_seq = 0
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
        # R16-B: a decoder switch ends with `set vid auto`; the hook re-states
        # what vid must be afterwards, so a hidden window stays off.
        self.core.hwdec_hook = self._reassert_vid
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
        # R17-A: GTK's own C click gesture goes the moment the handle
        # exists — it is the thing that pops the Mutter window menu on
        # right-click over the video, and no Python-level patch can stop
        # it (see the block at the top of this module).
        self._handle_click_audit = strip_handle_click_gestures(
            self._handle, "video")
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
        # R14-A: the stub exists ONLY for the CSD machinery — nothing may
        # ever be a target of it.  GTK's titlebar gestures (drag to move,
        # double-click to maximise, right-click => show_window_menu) are
        # driven by events targeting the titlebar subtree; a widget that
        # cannot be targeted hands them no events, so button-3 over the
        # top of the window reaches OUR capture controller instead (below).
        self._titlebar_stub.set_can_target(False)
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
            on_decode=self._toggle_decode,   # R7-B: the decode toggle
            on_activity=self._tick_arm)      # R16-A: input re-arms the cadence
        # R12: the top strip is an overlay child too — added FIRST so the
        # z-order matches the old contract (OSD/toast paint over it, the
        # bottom bar stays topmost).
        # R15-A: THE HANDLE IS THE OVERLAY CHILD, not the bar.  A
        # Gtk.WindowHandle carrying the bar makes press-and-drag on any
        # non-button part of the header a real window move (same
        # mechanism the video handle has always used); buttons claim
        # their own press, so their click can never turn into a move.
        # Aligned START/FILL exactly like the bare strip did — the
        # handle sizes to its child (valign START), so it never covers
        # the video below the bar or reserves layout space (R12's
        # byte-identical allocation invariant is untouched).
        self._top_handle = Gtk.WindowHandle()
        # R17-A: same C click gesture, same removal — right-click over the
        # header must open OUR menu, not mutter's.
        self._top_handle_click_audit = strip_handle_click_gestures(
            self._top_handle, "header")
        self._top_handle.set_valign(Gtk.Align.START)
        self._top_handle.set_halign(Gtk.Align.FILL)
        self._top_handle.set_child(self.top_bar)
        self.overlay.add_overlay(self._top_handle)
        for c in self.chrome.overlay_children():
            # R8-A: every child is added UNCONDITIONALLY — the deleted top
            # menu bar is not returned at all (it exists for NO config).
            self.overlay.add_overlay(c)
        # R12: the top strip fades as ONE group with the bottom bar
        # (opacity + can_target only — see Chrome.set_reveal).
        # R15-A: BOTH the handle and the bar are in the group — a hidden
        # header cannot move the window, so the handle's can_target must
        # follow the chrome decision too (a can_target=True handle over
        # an invisible bar would be an invisible drag strip).
        self.chrome.add_reveal_widget(self.top_bar)
        self.chrome.add_reveal_widget(self._top_handle)
        # ---- R17-A/C: everything the removed C click gesture used to do,
        # owned by the app instead.  Two pieces:
        #   * window move: an app-owned drag on each handle that calls
        #     Gdk.Toplevel.begin_move at the threshold (R17-C — the ONE
        #     shipped mechanism; see DSH-REPORT-R17);
        #   * header double-click => maximise/restore (the behaviour the
        #     removed C gesture carried via gtk-titlebar-double-click).
        # The video area's own double-click => fullscreen (R6) is NOT
        # affected by the removal: it lives on the GLArea, so it never
        # depended on the handle's click gesture.
        self._move_initiated = 0
        self._move_drags = {}
        self._move_pending = {}
        self._move_threshold = _drag_threshold_px()
        for _h, _which in ((self._handle, "video"),
                           (self._top_handle, "header")):
            _drag = Gtk.GestureDrag()
            _drag.set_button(1)          # PRIMARY only: a right-drag must
            # stay a no-op (R6-A: no menu, no move) exactly as before
            _drag.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
            # R17-C: begin_move fires at the THRESHOLD CROSSING, not at
            # drag-begin.  On this GTK build drag-begin is emitted on the
            # PRESS itself, so claiming there would swallow every
            # click-to-pause in the video area (measured: a plain
            # press+release at the centre started a move and the click
            # never reached the GLArea's own gesture).  The gate drives
            # exactly that input, so the bug cannot come back quietly.
            _drag.connect("drag-begin", self._on_move_drag_begin, _which)
            _drag.connect("drag-update", self._on_move_drag_update, _which)
            # GestureDrag has exactly three signals (drag-begin/update/end —
            # introspected); a cancelled sequence ends with drag-end too, so
            # one handler covers both.
            _drag.connect("drag-end", self._on_move_drag_end, _which)
            _h.add_controller(_drag)
            self._move_drags[_which] = _drag
        self._hdr_dblclick = Gtk.GestureClick()
        self._hdr_dblclick.set_button(1)
        self._hdr_dblclick.connect("pressed", self._on_header_press)
        # On the STRIP, not the handle: the handle must end up carrying no
        # Gtk.GestureClick at all (R17-A's structural assertion), and the
        # strip fills the handle (halign FILL), so every non-button press in
        # the header still reaches this gesture.  A press on a window
        # control button is claimed by the button first — and is excluded by
        # hit-test anyway, so double-clicking Close closes instead of
        # maximising.
        self.top_bar.add_controller(self._hdr_dblclick)
        self._titlebar_pinned = harden_titlebar_click_settings()
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
        # R14-A: button-3 is OWNED by the toplevel.  The area-level
        # controller above never sees a press whose event-widget is not the
        # video (top chrome, bottom bar, the stub-titlebar strip, the CSD
        # edges), and GTK's titlebar path turns exactly those presses into
        # Gdk.Toplevel.show_window_menu — the WM menu eating our menu.  This
        # controller runs during CAPTURE at the window — before any
        # descendant handler can bubble — and opens OUR popover at the
        # pointer.  Claiming is not reachable from this binding (pointer
        # sequences marshal as None, gtk_gesture_set_state(None) is
        # refused) and a descendant CLAIM — GtkRange's internal gesture
        # claims EVERY button, so the seek bar does — silences our
        # ``released`` for the rest of the sequence.  The base Gesture
        # ``end`` signal still fires for a DENIED capture gesture (proved
        # empirically), so the open is driven from ``end`` and the R6-A
        # 12 px drift rule measures device-position reads taken at press
        # and at end — same coordinate space, so the CSD shadow inset
        # cancels out without ever being named.  Ownership is
        # order-based: capture arms _rclick_top_pt first and the
        # area-level fallback (kept for presses this one somehow misses)
        # disarms itself on sight of the marker.  One popover, never a
        # double-open.
        self._rclick_top_pt = None
        rclick_top = Gtk.GestureClick()
        rclick_top.set_button(3)
        rclick_top.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        rclick_top.connect("pressed", self._on_rclick_top_press)
        rclick_top.connect("end", self._on_rclick_top_end)
        rclick_top.connect("cancel", self._on_rclick_top_cancel)
        self.add_controller(rclick_top)
        self._rclick_top = rclick_top

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
        # R14-B: WM_NORMAL_HINTS/PAspect publishing.  The debounce is
        # SEPARATE from the correction debounce on purpose: republishing a
        # property must never be entangled with resizing the window.
        self._aspect_hints_id = None      # one-shot republish timer
        self._aspect_hints_seen = None    # last pair seen; the one
        #   transient-fullscreen fallback memory (R14-B) — forgotten at
        #   stop and at every file-load edge
        self._aspect_hints_wsrc = None    # io watch: defend the property

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
        # R16-A: THE 100 Hz-FOREVER TICK IS GONE.  One fixed 10 ms source
        # used to drive chrome reveal AND the render/event pump for every
        # window, visible or not, forever; N windows meant N x 100 Hz of
        # Python through one GIL.  The work is now TWO sources with an
        # adaptive ladder (src/cadence.py decides, src/tickdrive.py applies):
        # the CHROME source runs at 10 ms during fades/activity, 100 ms
        # while visible+stable and has NO SOURCE AT ALL while the chrome is
        # hidden; the PUMP source drains mpv frames/events at 10 ms while
        # pictures are arriving and 100 ms while nothing is — it never
        # stops, because a stopped pump is a frozen picture and a stalled
        # playlist.  Both current intervals are introspectable
        # (tick_interval_ms / pump_interval_ms) and the gates read them.
        self._driver = tickdrive.TickDriver(self, pin_ms=self._tick_pin)
        self._driver.start()
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
        # R14-B: a WM that maximizes us (or un-maximizes) changes lock
        # eligibility without ever meeting an action of ours — republish
        # the size hints on the property signal, debounced.
        self.connect("notify::maximized", self._aspect_hints_soon)
        self.connect("notify::fullscreened", self._aspect_hints_soon)

        # R-audio focus policy (§4.10): only the focused window keeps audio
        self.connect("notify::is-active", self._on_active)

        # R16-B: minimize/unmap == stop decoding what nobody can see (and
        # ONLY that — fullscreen and normal-state changes keep us mapped).
        self.connect("notify::mapped", self._on_mapped_changed)

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
        # R14-A: the area-level controller is the FALLBACK.  This GTK
        # binding exposes no way to CLAIM a sequence from Python (no
        # set_sequence / get_current_event_sequence in the introspection
        # data), so ownership is enforced by propagation ORDER instead:
        # the toplevel CAPTURE-phase handler below runs before this
        # bubbling one for every button-3 press; when it armed itself for
        # this press, the marker is set and we stand down — one press can
        # never open two menus.
        if self._rclick_top_pt is not None:
            self._rclick_pt = None
            return
        self._rclick_pt = (float(x), float(y)) if int(n_press) == 1 else None

    def _on_rclick_release(self, _g, n_press, x, y, *_a) -> None:
        pt, self._rclick_pt = self._rclick_pt, None
        if int(n_press) != 1 or pt is None:
            return
        if abs(float(x) - pt[0]) > 12 or abs(float(y) - pt[1]) > 12:
            return          # a right-button DRAG: no menu, no drag, no-op
        self._open_context_menu(float(x), float(y))

    # ------------------------------------- R14-A toplevel button-3 owner --
    def _rclick_dev_pt(self):
        """Pointer in SURFACE coordinates (0,0 = X surface top-left), or
        None.  Independent of gesture propagation, so it is still exact
        while a descendant has claimed the button sequence."""
        surf = self.get_surface()
        if surf is None:
            return None
        try:
            seat = self.get_display().get_default_seat()
            res = surf.get_device_position(seat.get_pointer())
        except Exception:
            return None
        # this binding marshals as (grab_ok, x, y, ...):
        if not isinstance(res, tuple) or len(res) < 3 or not res[0]:
            return None
        return float(res[1]), float(res[2])

    def _on_rclick_top_press(self, _gesture, n_press, x, y, *_a) -> None:
        # CAPTURE phase on the toplevel: this runs for EVERY button-3
        # press in the window — video, top chrome, bottom bar (incl. the
        # seek bar), the stub strip (can_target False now, so the press
        # is targeted to the content below it), CSD edges — BEFORE any
        # descendant's bubbling handler, and before GTK's titlebar
        # machinery can route anything to show_window_menu (refused at
        # import anyway).  The menu opens when the sequence ENDS; the
        # press stores both the gesture-space point (widget coords) and
        # the same instant in device space (surface coords), so drift is
        # measured within one coordinate space and the constant offset
        # between the two cancels out.
        self._rclick_top_pt = None
        if int(n_press) != 1:
            return
        self._rclick_top_pt = (float(x), float(y), self._rclick_dev_pt())

    def _on_rclick_top_end(self, _gesture, *_a) -> None:
        # Gesture::end — fires for this capture gesture even after a
        # descendant CLAIM has silenced ``released`` (the seek-bar case).
        pt, self._rclick_top_pt = self._rclick_top_pt, None
        if pt is None:
            return
        x, y, dev0 = pt
        dev1 = self._rclick_dev_pt()
        if dev0 is not None and dev1 is not None:
            dx, dy = dev1[0] - dev0[0], dev1[1] - dev0[1]
            if abs(dx) > 12 or abs(dy) > 12:
                return      # right-button DRAG: no menu (the R6-A rule)
            self._open_context_menu(x + dx, y + dy)
        else:
            self._open_context_menu(x, y)

    def _on_rclick_top_cancel(self, *_a) -> None:
        self._rclick_top_pt = None

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
            # R14-B: and the DECLARE goes too — a WM that still enforced
            # PAspect would undo the crop on the next drag.
            self._aspect_hints_apply()
            return "  ·  aspect lock paused"
        self._aspect_realign()
        self._aspect_hints_apply()     # R14-B: lock back, hints back
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

    def _set_top_handle_active(self, on: bool) -> None:
        """R15-A: header draggability is WINDOWED-ONLY, the exact rule R5-D
        established for the video handle.  Fullscreen swaps the header's
        Gtk.WindowHandle OUT and re-parents the bare strip into the very
        same slot (first overlay child, so the z-order contract holds),
        making a fullscreen header drag structurally a no-op.  Windowed
        re-mounts the handle around the strip."""
        h = self._top_handle
        if on:
            if h.get_parent() is self.overlay:
                return
            if self.top_bar.get_parent() is self.overlay:
                self.overlay.remove_overlay(self.top_bar)
            h.set_child(self.top_bar)
            # first overlay child = right after the base (video) child
            h.insert_after(self.overlay, self.overlay.get_child())
        else:
            if h.get_parent() is None and (
                    self.top_bar.get_parent() is self.overlay):
                return
            if h.get_parent() is not None:
                self.overlay.remove_overlay(h)
            h.set_child(None)
            self.top_bar.insert_after(self.overlay,
                                      self.overlay.get_child())

    def _build_top_chrome(self) -> Gtk.Box:
        """R12: the top overlay strip — media title plus OUR OWN window
        controls (minimise / maximise-restore / close).  It is an overlay
        child, not a titlebar: retracting it only fades opacity, and no
        button starts a window drag (R15-A wrapped the strip in a
        Gtk.WindowHandle, but a press on a button is claimed by the
        button itself and can never reach the handle's drag gesture;
        the video's click gesture underneath is equally unreachable).

        R15-C: FLUSH at the window edges — no margins on any side, so
        the strip's dark background IS the top edge of the client area
        (a margin here renders as a transparent rim over live video,
        which is exactly what round 15 removes).  Breathing room lives
        INSIDE: title-label padding and the buttons' own padding.  The
        strip still holds no layout slot, so R12's byte-identical
        video allocation (revealed vs hidden) is untouched."""
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        bar.set_valign(Gtk.Align.START)
        bar.set_halign(Gtk.Align.FILL)
        bar.add_css_class("reelsee-chrome")
        self._title_label.set_hexpand(True)
        self._title_label.set_margin_start(8)     # spacing INSIDE the
        self._title_label.set_margin_end(4)       # flush strip
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
        self.btn_close.set_margin_end(8)
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

    # -------------------------------- R17-C: app-owned window move --
    def _on_move_drag_begin(self, gesture, start_x, start_y, which) -> None:
        """NOTHING happens here but recording the origin.

        `Gtk.GestureDrag::drag-begin` is emitted on the PRESS, not when the
        pointer has travelled (measured on this GTK 4.22.4 build: a plain
        press+release in the middle of the video area emitted drag-begin and
        a begin_move there swallowed click-to-pause entirely).  The move is
        therefore started at the THRESHOLD CROSSING, in _on_move_drag_update,
        exactly as the round-17 brief specifies; a press that never travels
        stays a click for the app's own click gestures.
        """
        if self._closing:
            return
        self._move_pending[which] = {
            "x": float(start_x),
            "y": float(start_y),
            "thr": _drag_threshold_px(),
            "dev": gesture.get_current_event_device(),
            "button": gesture.get_current_button(),
            "time": gesture.get_current_event_time(),
        }

    def _on_move_drag_update(self, gesture, off_x, off_y, which) -> None:
        """Threshold crossing => start the move, once."""
        pend = self._move_pending.get(which)
        if pend is None:
            return
        thr = pend.get("thr") or self._move_threshold
        if math.hypot(float(off_x), float(off_y)) < thr:
            return
        self._move_pending.pop(which, None)
        self._begin_move(gesture, pend, which)

    def _on_move_drag_end(self, gesture, *_a) -> None:
        """Released before the threshold: forget the origin so the press
        stays a click (and a later drag on the other handle starts clean)."""
        self._move_pending.clear()

    def _begin_move(self, gesture, pend: dict, which: str) -> None:
        """R17-C: THE app-owned window move — one mechanism, shipped on
        purpose.  `Gdk.Toplevel.begin_move` IS introspectable on this
        build (`begin_move(self, device:Gdk.Device, button:int, x:float,
        y:float, timestamp:int)`) and is reachable from Python through the
        toplevel SURFACE (`widget.get_surface()`), so the move is started
        with the CURRENT event's device, button and time — no raw Xlib
        client message, no `_NET_WM_MOVERESIZE` reimplementation, no
        dependency on `src/x11pos.py`.  On X11 GDK turns the call into the
        same `_NET_WM_MOVERESIZE` MOVE the C gesture used to send (probe:
        the x we passed appears verbatim in the wire message), so the WM
        drives the move and the observable behaviour of R5-D/R15-A is
        unchanged.

        The sequence is CLAIMED afterwards: the retained C `GestureDrag`
        (kept on purpose, R17-A) would otherwise initiate a SECOND move —
        probe-proven 2 dir-8 messages without the claim, 1 with it.  The
        claim is also what keeps "a drag never toggles pause" working: the
        app's click gestures are cancelled with the sequence.

        If begin_move raises there is no claim, so the retained C drag
        still moves the window — a graceful fallback, never an exception
        on the main thread.
        """
        if self._closing:
            return
        top = self.get_surface()
        if top is None or not isinstance(top, Gdk.Toplevel):
            return
        dev = pend.get("dev")
        if dev is None:
            dev = gesture.get_current_event_device()
        if dev is None:
            # A gesture with no live sequence reports no device (a11y- or
            # test-synthesised drags); fall back to the seat pointer so the
            # move still starts instead of silently refusing.
            disp = self.get_display()
            seat = disp.get_default_seat() if disp is not None else None
            dev = seat.get_pointer() if seat is not None else None
        if dev is None:
            log.debug("R17-C: %s drag has no pointer device; native drag "
                      "stands", which)
            return
        try:
            button = int(pend.get("button") or gesture.get_current_button()
                         or 1)
            when = int(pend.get("time") or gesture.get_current_event_time())
            top.begin_move(dev, button, float(pend["x"]), float(pend["y"]),
                           when)
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        except Exception as exc:                    # noqa: BLE001
            log.debug("R17-C: %s begin_move refused (%s); native drag "
                      "stands", which, exc)
            return
        self._move_initiated += 1
        log.debug("R17-C: %s drag initiated begin_move(button=%d, t=%d)",
                  which, button, when)

    # --------------------------- R17-A: header double-click maximise --
    def _on_header_press(self, _gesture, n_press, x, y, *_a) -> None:
        """Double-click on the header toggles maximise/restore — the
        behaviour the removed C click gesture carried via
        `gtk-titlebar-double-click`.  Presses inside a window control
        button belong to the button (double-clicking Close must close, not
        maximise), so they are excluded by hit-test."""
        if int(n_press) < 2 or self._closing:
            return
        if self._header_hit_button(x, y):
            return
        self._on_maxrestore_clicked()

    def _header_hit_button(self, x: float, y: float) -> bool:
        """True when a press at header-strip coordinates (x, y) lands on one
        of the three window control buttons.

        `Gtk.Widget.compute_bounds` is a vfunc binding: it returns
        `(success, Graphene.Rect)`, NOT a bare rect — and `Graphene.Rect`
        exposes its fields ONLY through getters (`get_x/get_y/get_width/
        get_height`; there is no `.x` attribute).  Reading `.x` raises
        AttributeError on the GTK main thread, so the success flag is
        checked first and the getters are used."""
        for b in (self.btn_min, self.btn_max, self.btn_close):
            ok, r = b.compute_bounds(self.top_bar)
            if not ok or r is None:
                continue
            rx, ry = r.get_x(), r.get_y()
            if rx <= x < rx + r.get_width() and ry <= y < ry + r.get_height():
                return True
        return False

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
            # R16-A: focus is a STATE change — fast cadence first.
            self._tick_arm("focus")
            self.app.on_window_focus(self)

    def transient_mute(self, on: bool) -> None:
        """Focus mute (audio.focus_mute): mpv-side only — never persisted,
        never touches the settings binding."""
        self._transient_muted = bool(on)
        self.core.set_property(
            "mute", bool(on) or bool(self.cfg.get("audio.muted", False)))

    # ------------------------------------- R16-B: hidden-video suppression --
    # A minimized window is an UNMAPPED surface: the user cannot see a single
    # frame of it, yet mpv kept decoding (a 4K stream at full rate) to feed a
    # render path nobody looks at — CPU/GPU bandwidth the visible windows
    # need.  So `vid` goes off with the surface and back with it.
    #
    # The trigger is mapping ONLY (`notify::mapped`; under X11/XWayland an
    # iconified toplevel is unmapped by the server, which is exactly the case
    # the operator's session produces).  Fullscreen enter/leave keeps the
    # surface mapped and therefore never touches this.  Nothing else is
    # written: audio is never muted here, `pause` is never written, and no
    # window is ever auto-paused — the position keeps advancing, which is
    # what "I minimized it and it carried on" means.
    def _vid_write(self, value: str, why: str) -> None:
        """The ONE writer of mpv's `vid` property (R16-B)."""
        self.core.set_property("vid", value)
        log.debug("vid=%s (%s)", value, why)

    def _apply_vid(self, value: str, why: str) -> None:
        """Record/apply the USER's own video-track choice (audio-only switch,
        an explicitly picked track, `auto`).  While a suppression holds the
        decoder off the EFFECTIVE value stays `no` — a file loading into a
        hidden window must not turn decoding back on — but the choice is
        remembered, so the remap restores THAT and not a bland `auto` that
        quietly picks a different track."""
        eff = self._vidsup.user_wants(value)
        self._vid_write(eff, why)
        if eff != vidsup.norm_vid(value):
            log.debug("vid=%s recorded for remap (%s)", eff, why)

    # The bits `Gdk.Surface.get_state()` was MEASURED to report (0x10001) on a
    # toplevel the window manager unmapped, while `Gtk.Window.mapped` stayed
    # True (scratch/probe_state_x.py, round 16; the same reading came from
    # scratch/probe_mapped.py).  This GTK build exposes neither
    # `Gdk.SurfaceState` to Python (`AttributeError`) nor
    # `gdk_surface_state_get_type` in libgtk-4, so the mask is the OBSERVED
    # number, not an enum name — see DSH-REPORT-R16.md for the measurements.
    _SURFACE_STATE_UNMAPPED_BITS = 0x10001

    def _surface_state(self) -> int:
        """Cached C read of the surface state flags (no round trip).  A seam
        the hiddensuppress gate drives to replay the window-manager case."""
        surf = self.get_surface()
        if surf is None:
            return 0
        try:
            return int(surf.get_state())
        except Exception:                       # noqa: BLE001
            return 0

    def _surface_hidden(self) -> bool:
        """R16-B: is this window's surface off-screen?

        TWO independent facts, OR-ed, because neither alone is enough and
        both were MEASURED here, not assumed:

        * `Gtk.Window.mapped` — GTK's own mapping.  It follows
          `set_visible()` (and a Wayland hide), but under the X11 backend it
          does NOT follow an unmap the WINDOW MANAGER performs: GTK4 has no
          signal for a surface state change, so a real GNOME minimize leaves
          `get_mapped()` True.
        * the surface state bits the X11 backend leaves set once the WM has
          unmapped us (`_SURFACE_STATE_UNMAPPED_BITS`).

        Fullscreen enter/leave sets neither (measured: state stays 0 and the
        surface stays mapped), so it never suppresses."""
        if not self.get_mapped():
            return True
        return bool(self._surface_state() & self._SURFACE_STATE_UNMAPPED_BITS)

    def _on_mapped_changed(self, *_a) -> None:
        """R16-B: minimize/unmap stops decoding, remap restarts it.

        Nothing but `vid` is ever written here: audio is never muted,
        `pause` is never written, no window is ever auto-paused — the
        position keeps advancing, which is what "I minimized it and it
        carried on" means."""
        self._sync_hidden_suppression("notify::mapped")

    def _sync_hidden_suppression(self, why: str) -> None:
        """Reconcile mpv with the CURRENT hidden state.

        Called from `notify::mapped` AND from the pump tick (`tick_pump`,
        which R16-A never suspends — a minimized window still has a pump,
        so it still notices it was minimized).  `VidSuppression.sync` is
        transition-only, so a 100 ms re-statement writes nothing until the
        state actually flips."""
        if self._closing:
            return
        hidden = self._surface_hidden()
        if hidden == self._vidsup.hidden:
            return
        val = self._vidsup.sync(hidden)
        if hidden:
            self._vid_release = val
            if val is not None:
                self._vid_write("no", f"unmap ({why})")
                log.info("hidden-video suppress (%s, %s): vid=no (audio, "
                         "pause and position untouched)", why, self._vidsup.user_vid)
            else:
                log.debug("hidden-video suppress (%s): nothing to stop "
                          "(setting off, or video already off)", why)
        else:
            self._vid_release = None
            if val is not None:
                self._vid_write(val, f"remap ({why})")
                log.info("hidden-video restore (%s): vid=%s", why, val)

    def _reassert_vid(self, _mode=None) -> None:
        """R16-B: after anything that toggles `vid` (a decoder switch via
        apply_hwdec, an engine retry), re-state `no` when `no` is what this
        window must hold.  The proven switch ends with `set vid auto`, which
        would otherwise (a) bring a hidden window's decoder back to life and
        (b) undo the user's audio-only switch.

        Deliberately narrow: with nothing to hold, NOTHING is written — the
        window-open path must not get a stray `vid=no` before the surface
        has ever mapped."""
        if self._vidsup.suppressed or self._vidsup.user_vid == "no":
            self._vid_write("no", "hwdec re-assert")

    def _set_suppress_enabled(self, flag) -> None:
        """The live settings flip (playback.suppress_video_when_hidden)."""
        val = self._vidsup.set_enabled(flag)
        if val is not None:
            self._vid_write(val, "suppress setting")

    # ------------------------------------------------- R16-C thread budget
    def _cpu_decoding(self) -> bool:
        """Is this window ACTUALLY decoding on the CPU?

        The observed `hwdec-current` snapshot value is the only honest signal
        (R9: the main thread never calls a sync mpv getter; R2-P2: a
        configured GPU engine that silently failed must read CPU, never GPU).
        `unknown` — before the first frame — counts as not-yet-CPU, so a
        window that has not decoded anything is never handed a number."""
        return WD.decode_effective(
            self.core.snapshot.get("hwdec-current")) == "off"

    def apply_thread_budget(self, threads: int) -> None:
        """R16-C: hand this window its share of the CPU decode budget.

        ONLY CPU-decoded windows are touched — the brief forbids rewriting a
        GPU window's lavc thread count (harmless, but it makes the setting
        untraceable).  `threads <= 0` means auto, and mpv has no way back to
        auto once a number has been written, so a window that opens as the
        only window is simply never given one.

        Honest limitation: `vd-lavc-threads` is runtime-settable (measured:
        `set_property_string` returns success) but lavc applies it when the
        decoder is (re)initialised, so a change lands on the next file load or
        decoder re-init — never mid-stream.  Nothing here forces a re-init,
        because forcing one would blank the picture for the sake of a thread
        count."""
        if self._closing or threads <= 0 or not self._cpu_decoding():
            return
        want = str(int(threads))
        if self._lavc_threads == want:
            return
        self._lavc_threads = want
        try:
            self.core.set_property("vd-lavc-threads", want)
            log.info("decode thread budget: %s threads (cpu-decoded window)",
                     want)
        except Exception:                       # noqa: BLE001
            log.exception("thread budget write failed")

    # ------------------------------------------------------ R16-D decode plan
    def gpu_decoding(self) -> bool:
        """R16-D: is this window ACTUALLY decoding on a GPU?  `unknown` (no
        frame decoded yet) is deliberately False — a window nobody has seen
        decode anything is not a stream the GPU is carrying."""
        return WD.decode_effective(
            self.core.snapshot.get("hwdec-current")) == "on"

    def demote_to_cpu(self, reason: str = "plan") -> None:
        """R16-D: move THIS window to CPU decode.

        The mechanism is the one AGENTS.md proves: `set hwdec` alone never
        reinitialises the decoder, so this goes through apply_engine →
        apply_hwdec, which performs set hwdec → set vid no → set vid auto and
        leaves the position alone.  `_decode_auto` marks the demotion as
        AUTOMATIC (the indicator reads "CPU (auto)"), never a user override —
        the same flag R7-B introduced for watchdog demotes."""
        if self._closing:
            return
        try:
            cpu = engine.select_engine("cpu", engine.enumerate_engines())
            engine.apply_engine(self.core, cpu, self.cfg)
            self._set_decode_flags(self._decode_user, True)
            log.info("decode plan: demoted to CPU (%s)", reason)
        except Exception:                       # noqa: BLE001
            log.exception("decode plan demotion failed (%s)", reason)

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
        self._apply_vid("no" if self.cfg.get("playback.video_off", False)
                        else "auto", "file-load")

    def _on_file_loaded(self, _core, _payload) -> None:
        c = self.cfg
        # R16-A: a new file is a STATE change — it can show the chrome, and
        # it definitely starts pictures moving.  Fast cadence from here.
        self._tick_arm("file")
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
        # R14-B: the video size is a fact that ARRIVES (dwidth/dheight land
        # after file-loaded, and vanish on stop) — whenever the pair the
        # hints would publish changes, re-declare it.  Diffed on purpose:
        # this listener runs at least every 50 ms, and a property write per
        # tick would be a new performance bug in the shape fix.
        pair = self._aspect_hints_pair()
        if pair != self._aspect_hints_seen:
            self._aspect_hints_seen = pair
            self._aspect_hints_soon()
        # R16-C: a window's decode path is a fact that ARRIVES — hwdec-current
        # is unknown until the first frame decodes, and a watchdog demotion
        # flips it later.  The thread budget is a function of how many windows
        # are CPU-decoded, so the budget is recomputed on that edge.  Diffed
        # on purpose: this listener runs at least every 50 ms.
        hw = self.core.snapshot.get("hwdec-current")
        if hw != self._hwdec_seen:
            self._hwdec_seen = hw
            hook = getattr(self.app, "on_decode_state_changed", None)
            if hook is not None:
                hook(self)
            else:
                retune = getattr(self.app, "retune_decode_threads", None)
                if retune is not None:
                    retune()

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

    def _tick_arm(self, _why: str = "") -> None:
        """R16-A: the ONE cadence re-arm entry point.

        Every input and state edge reaches it — pointer motion/click/hover/
        leave and key, menu open/close, OSD/toast presentation and the
        observed play/pause edge through `Chrome.on_activity`, plus window
        focus and file load here.  It runs BEFORE any visual work: the fast
        cadence (and, after an idle stretch, the chrome source itself) is
        back before a single widget is touched, so a suspended window cannot
        swallow the first motion.
        """
        d = self._driver
        if d is None:
            return                    # still constructing: nothing to wake
        d.arm()

    @property
    def tick_interval_ms(self) -> int | None:
        """R16-A: the chrome source's CURRENT interval — 10 / 100 / None
        (None == no source at all, i.e. suspended while hidden).  The
        introspectable surface the gates read; never a re-derivation."""
        d = self._driver
        return None if d is None else d.current_ms

    @property
    def pump_interval_ms(self) -> int | None:
        """R16-A: the pump source's CURRENT interval (10 / 100; the pump is
        never suspended while the window lives)."""
        d = self._driver
        return None if d is None else d.pump_current_ms

    def tick_reveal(self) -> None:
        """The CHROME half of the old tick: one ChromeState step, applied."""
        self._apply_reveal(self.chrome.tick())

    def tick_pump(self) -> None:
        """The PUMP half of the old tick — everything that must keep being
        drained no matter what the chrome is doing (R16-A split)."""
        self.view.poll()
        self.core.poll()
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
            # R17-D: while the file is still buffering the engagement
            # deadline is FROZEN — an engine that engages late because the
            # cache was still filling is not an engagement failure.
            snap = self.core.snapshot
            self._engagement.feed(
                snap.get("hwdec-current"),
                buffering=WD.is_buffering(snap.get("paused-for-cache"),
                                          snap.get("cache-buffering-state")))
        # R16-B: the pump is the ONE place a minimized window still runs
        # (R16-A never suspends it), so it is where the WM-iconified case is
        # noticed.  Transition-only: no mpv write until the state flips.
        self._sync_hidden_suppression("pump")

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
                    # R16-C's thread retune rides on the hwdec-current edge
                    # this demote produces, so nothing else is needed here.
                    self._set_decode_flags(self._decode_user, True)
        except Exception:                       # noqa: BLE001
            log.exception("watchdog tick failed")
        return True

    def _on_close_request(self, *_a) -> bool:
        # §0.6: probes/state first (nothing yet in P2), then GL-current
        # render-context teardown; terminate happens on the app's worker.
        self._cancel_single()            # R5-D: no pause toggle post-mortem
        # R16-A: BOTH cadence sources die here (chrome + pump).  The
        # `_closing` guard in the callbacks is the belt for paths that never
        # fire close-request.
        if self._driver is not None:
            self._driver.stop()
        GLib.source_remove(self._wd_id)
        if self._resume_id is not None:
            GLib.source_remove(self._resume_id)
            self._resume_id = None
        # R8-B: a pending correction or a file-poll outliving the window
        # would resize a dead surface (and spam a traceback per tick).
        for _sid in ("_aspect_debounce_id", "_aspect_guard_id",
                     "_aspect_file_id", "_aspect_hints_id",
                     "_aspect_hints_wsrc"):
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
        if not self.get_mapped():
            # R16-B: an unmapped window has no live rect worth sampling —
            # GTK reports the last realized size, and treating that as news
            # is how a minimize turns into a resize storm on restore.
            return True
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
        # R14-B: the XID (and with a replaced surface, a BRAND-new one,
        # whose WM_NORMAL_HINTS start empty) only exists while the surface
        # is real — re-declare the hints on every re-wire, debounced so a
        # fullscreen surface swap is ONE property write, not the storm.
        # The clobber watch is per window too: disarmed here, the
        # resulting republish re-arms it on the NEW window.
        if self._aspect_hints_wsrc is not None:
            GLib.source_remove(self._aspect_hints_wsrc)
            self._aspect_hints_wsrc = None
        self._aspect_hints_soon()

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
        if not self.get_mapped():
            # R16-B: no corrections on a hidden surface.  A minimized window
            # that "corrects" itself asks for a resize nobody can see, and
            # the answer comes back as a burst the moment it maps again.
            return
        same = (size == self._aspect_size)
        self._aspect_size = size
        if not same:
            # R14-B: GDK rewrites WM_NORMAL_HINTS itself when its own
            # content min-size changes — a real resize can silently wipe
            # our PAspect words, so every genuine size change re-declares
            # them (debounced; a repaint storm never gets here).
            self._aspect_hints_soon()
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
        if not self.get_mapped():
            # R16-B: THE choke point — no aspect correction can reach a
            # hidden surface from ANY caller (layout storm, file-load fit,
            # realign), so a minimized window never queues a resize that
            # lands as a storm the moment it maps again.
            log.debug("aspect correction skipped while unmapped: %s", size)
            return
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

    # ------------------------------------------------ R14-B: WM_NORMAL_HINTS
    def _aspect_hints_pair(self) -> tuple[int, int] | None:
        """The PAspect pair for the video on screen NOW, or None.

        Source rule, the same one §R8-B established for the correction:
        the OBSERVED display size (dwidth/dheight — what a rotated or
        vertical file actually occupies), falling back to the coded size
        only until the display size has arrived.  Never the container,
        never config.video_aspect, never a re-derived value.

        One measured edge forces exactly ONE concession: entering
        fullscreen re-inits the video output — dwidth/dheight go blank
        and are NOT re-emitted while the file keeps playing (measured on
        HEAD under both Xvfb and a real X display; §R8-B's live-snapshot
        correction goes blind at the very same moment, for the very same
        reason).  The requirement still demands RESTORE on fullscreen
        leave, so with no live dimensions we fall back to the last pair
        seen — but only while the core still holds a file (`idle-active`
        False).  Real boundaries forget the memory outright: _act_stop
        and every file-load edge, the idle flag itself covers EOF unload.
        This is a memory of an OBSERVED value at a known-transient edge,
        not a parallel source of truth."""
        snap = self.core.snapshot or {}
        for wk, hk in (("dwidth", "dheight"), ("width", "height")):
            pair = aspect.paspect_pair(snap.get(wk), snap.get(hk))
            if pair is not None:
                return pair
        if self._aspect_hints_seen is None:
            return None
        if snap.get("idle-active"):
            self._aspect_hints_seen = None        # the file is gone
            return None
        return self._aspect_hints_seen

    def _aspect_hints_apply(self) -> None:
        """Declare the lock in the ONE language a window system hears on
        X11: WM_NORMAL_HINTS with PAspect (R14-B).  GTK 4 deleted geometry
        hints, so without this the window manager never even tries to keep
        the ratio during a drag, and §R8-B's post-hoc correction is a
        mid-drag fight the compositor always wins.

        Cleared whenever the lock is not ACTIVE — the single truth
        aspect_lock_active (setting off, crop-to-window on per R11,
        fullscreen, maximized) — or there is no video to lock to.  The
        post-hoc correction keeps running untouched: with hints honoured
        it usually has nothing to correct (its >1 px gate is already
        satisfied), on Wayland and on hint-ignoring servers it remains
        the whole mechanism.  Both x11pos entries degrade to False —
        publish failure is invisible, the correction path simply keeps
        its old job."""
        if self._closing:
            return
        pair = self._aspect_hints_pair() if self._aspect_locked() else None
        if pair is None:
            x11pos.clear_aspect_ratio(self)
            self._hints_watch(False)
        else:
            x11pos.set_aspect_ratio(self, pair[0], pair[1])
            self._hints_watch(True)

    def _hints_watch(self, want: bool) -> None:
        """R14-B, and the reason this is a WATCH and not a one-shot:
        measured with a PropertyChange subscription on our own window
        (scripts/smoke_aspecthints.py, storm_* phases), GDK's X11 backend
        rewrites WM_NORMAL_HINTS WHOLESALE — its own flags and min-size,
        PAspect gone — on EVERY DRAWN FRAME (~2 notifications per frame
        at 30 fps), and writes nothing at all while paused.  A publish
        made at a state edge therefore survives pauses, idling and every
        listed refresh edge, but is erased within one frame of playback.
        So while the declaration is up we hold an event selection on it
        (per-client — GDK pays nothing) and repair on notice.  The repair
        is a property write, never a resize: §R8-B's geometry-storm rules
        are untouched and cannot be entered from here.

        Event selection is per window: a replaced surface is a NEW XID
        with a fresh (empty) property, so _wire_aspect_surface drops the
        arm and the resulting republish re-arms on the right window."""
        if self._closing:
            return
        if want:
            if self._aspect_hints_wsrc is not None:
                return
            fd = x11pos.watch_hints(self)         # selects + hands us fd
            if fd is None:
                return                            # Wayland / no Xlib
            self._aspect_hints_wsrc = GLib.io_add_watch(
                fd, GLib.IO_IN, self._on_hints_io)
        elif self._aspect_hints_wsrc is not None:
            GLib.source_remove(self._aspect_hints_wsrc)
            self._aspect_hints_wsrc = None
            x11pos.unwatch_hints(self)

    def _on_hints_io(self, _source, cond) -> bool:
        """Our X connection has readable data: drain it (unread events
        would pile up forever on the shared connection), and if anyone
        touched WM_NORMAL_HINTS, re-declare NOW — undebounced, because
        the arithmetic of a debounced repair is fatal: GDK rewrites at a
        ~33 ms cadence while drawing, so any debounce LONGER than that
        leaves the property cleared for most of every second.  The repair
        is property-only (never a resize, so §R8-B's geometry-storm rules
        cannot be entered from here) and it self-converges: our own write
        echoes back as one more notice, the second apply reads the
        already-exactly-right words and writes NOTHING (x11pos early-
        return), so notice storms end after one round-trip each.  A dead
        socket (ERR|HUP) drops the watch; x11pos drops the connection on
        its own next use."""
        if self._closing or cond & (GLib.IO_ERR | GLib.IO_HUP
                                    | GLib.IO_NVAL):
            self._aspect_hints_wsrc = None
            return False
        if x11pos.hint_notices(self):
            self._aspect_hints_apply()
        return self._aspect_hints_wsrc is not None

    def _aspect_hints_soon(self, *_a) -> None:
        """Debounced republish for storm-prone EDGES (layout size change,
        snapshot churn, surface re-wire): ONE timer, ~200 ms — the
        property write is cheap and idempotent, but the read-back behind
        it should not happen ten times a second during a drag.  (The
        per-frame clobber fight is NOT routed through here — see
        _on_hints_io, which must repair immediately.)"""
        if self._closing or self._aspect_hints_id is not None:
            return
        self._aspect_hints_id = GLib.timeout_add(200, self._aspect_hints_now)

    def _aspect_hints_now(self) -> bool:
        self._aspect_hints_id = None
        self._aspect_hints_apply()
        return False

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
        # R14-B: a file-load boundary makes the previous clip's pair
        # meaningless — forget it so the transient fallback below can
        # never publish the OLD video's ratio into the new file's window.
        self._aspect_hints_seen = None
        a = aspect.aspect_of(self.core.snapshot)
        if a is None:
            if left > 1:
                self._aspect_file_id = GLib.timeout_add(
                    100, self._aspect_on_file, left - 1)
                return False
            self._aspect_file_id = None
            # R14-B: gave up — there is NO video to lock to (audio-only,
            # or a file that never reports a size); hints come off so the
            # box is free again.
            self._aspect_hints_apply()
            self._geom_consume()         # nothing to refit anyway (R10-B.1)
            return False                 # audio-only / idle: leave it alone
        self._aspect_file_id = None
        self._aspect_applied = None      # new video: new aspect, retries OK
        # R14-B: the NEW file's ratio goes to the window system NOW — even
        # on the branches that do not resize (adopted geometry, already-
        # right shape), because "don't move the window" never meant "let
        # the WM resize it to any shape".
        self._aspect_hints_apply()
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
        # R14-B: the DECLARE follows the decision — hints on with the lock,
        # off without it (off is total, and that includes the hints).
        self._aspect_hints_apply()
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
                lambda kind=kind, t=t: self._track_pick(
                    kind, prop.get(kind, kind),
                    "no" if t.get("selected") else str(t.get("id")))))
        for kind, label in (("video", "no video (audio only)"),
                            ("audio", "no audio"), ("sub", "no subtitles")):
            self._tr_list.append(self._row(
                f"{'●' if self.core.snapshot.get(prop[kind]) in (False, 'no') else '○'}  {label}",
                lambda kind=kind: self._track_pick(kind, prop[kind], "no")))

    def _track_pick(self, kind: str, prop: str, value: str) -> None:
        """Track menu write.  A VIDEO pick is a statement about whether we
        decode video at all, so it goes through `R16-B`'s `_apply_vid` — a
        hidden window must restore the user's choice, not "auto".  Audio and
        subtitle tracks are plain property writes."""
        if kind == "video":
            self._apply_vid(value, "track menu")
            return
        self.core.command("set", prop, value)

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
            self._aspect_hints_apply()           # R14-B: same declare,
                                                 # settings-row writer
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
            self._apply_vid("no" if value else "auto", "setting video_off")
        elif key == "playback.suppress_video_when_hidden":
            # R16-B: live flip.  Turning it OFF while a window is minimized
            # hands the decoder back at once; turning it ON while minimized
            # stops it.  No restart, no window rebuild.
            self._set_suppress_enabled(value)
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
        # R15-A: the HEADER handle follows the exact same windowed-only
        # rule — fullscreen must not present a draggable strip either.
        self._set_top_handle_active(not self._fs)
        # R5-A: both edges re-arm the countdown — fullscreen retracts through
        # the SAME idle mechanism, and leaving fullscreen restores revealed
        # (the fresh wake-up reveals immediately via the normal tick).
        self.chrome.wake()
        self._apply_reveal(True)
        self._update_title()
        # R14-B: fullscreen is not ours to constrain — _aspect_locked()
        # now says "inactive" (or, on exit, "active" again) and the hints
        # follow the same truth as the correction.
        self._aspect_hints_apply()

    def _act_fullscreen_exit(self, _p=None) -> None:
        if self._fs or self.is_fullscreen():
            self.unfullscreen()
            self._set_handle_active(True)         # R5-D: drag back on
            self._set_top_handle_active(True)     # R15-A: header drag too
            self._fs = False
            # R8-B: the RESTORE storm is the dangerous edge — this is where a
            # naive lock snaps a 1920x1080 window down to 450x800 because it
            # still believes the pre-fullscreen size was the video's box.
            self._aspect_forget()
            self._aspect_free_until = time.monotonic() + 0.4
            self.chrome.wake()           # R5-A: restored chrome is revealed
            self._apply_reveal(True)
            self._update_title()
            # R14-B: windowed again — the ratio is declared again (the
            # freeze above guards RESIZES, never the property write).
            self._aspect_hints_apply()

    def _act_fullscreen_state(self) -> bool:
        return bool(self._fs or self.is_fullscreen())

    def _act_stop(self, _p=None) -> None:
        self.core.command("stop")
        # R14-B: stop means "no video to lock to" — take the hints off
        # NOW.  An _aspect_hints_apply() here would be wrong: the mpv
        # dwidth→0 event has not arrived yet, so the (stale) snapshot
        # would RE-publish the old pair.  The snapshot diff re-clears
        # idempotently when the event lands (no second server write).
        self._aspect_hints_seen = None     # no video, no memory of one
        x11pos.clear_aspect_ratio(self)
        self._hints_watch(False)
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
