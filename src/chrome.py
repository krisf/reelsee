"""Chrome — pure ChromeState (§4.6) + monochrome GTK chrome widgets + OSD/toast.

Rules encoded in ChromeState (PLAN §4.6, gate-tested in
tests/test_chrome_state.py):
  * hidden ONLY while playing — pause/stop/EOF always shows;
  * 3000 ms of pointer inactivity hides;
  * any pointer motion / click / wheel shows and RE-ARMS;
  * hovering the bottom 80 px keeps it visible;
  * a keyboard never re-arms the timer (keyboard seek while hidden shows a
    1200 ms transient OSD — time + speed — without revealing the bars).
GTK side applies visibility as opacity (180 ms CSS transition) + blank cursor.
"""
from __future__ import annotations

import time

IDLE_MS_DEFAULT = 3000.0
BOTTOM_HOVER_PX = 80
JUMP_FACTOR = 4.0          # dt > timeout*4 (or < 0) is a clock jump: re-arm


# --------------------------------------------------------------------- repeat
# R5-C: the bottom-bar repeat button cycles off → all → one → off on top of
# the SAME two mpv properties the "l"/"r" keys toggle, so the icon is always
# DERIVED from the snapshot (single source of truth; a raw key press repaints
# the button too).  Pure helpers — unit-testable without a display.

REPEAT_MODES = ("off", "all", "one")
REPEAT_NEXT = {"off": "all", "all": "one", "one": "off"}
REPEAT_ICONS = {"off": "media-playlist-repeat-symbolic",
                "all": "media-playlist-repeat-symbolic",
                "one": "media-playlist-repeat-song-symbolic"}

# R6-B: the FINAL bottom bar, left→right — the single source of truth for
# both the widget build below and the unit/smoke order gates.  There is
# deliberately NO "stop" here: the stop BUTTON is gone from the tree
# (the win.stop action survives for keyboard/palette).
# R7: "decode" is appended LAST — the rightmost control of the bottom bar
# (immediately after mute), one widget that is BOTH indicator and toggle.
BOTTOM_ORDER = ("play_pause", "prev", "next", "a", "b", "position",
                "seek", "duration", "repeat", "speed", "volume", "mute",
                "decode")


# --------------------------------------------------------------------- decode
# R7: the decode indicator+toggle.  The state mapping is PURE and derived
# ONLY from the observed `hwdec-current` snapshot value through the ONE
# authoritative classifier (watchdog.mode_base + HW_REAL_ON) — never from
# the configured/requested engine mode, so the label can never claim GPU
# because engine.decoder asks for it (R2-P2/I1).

DECODE_LABELS = {"gpu": "GPU", "cpu": "CPU", "cpu_auto": "CPU (auto)",
                 "idle": "…"}


def decode_state(hwdec_current, user_cpu: bool = False,
                 auto_cpu: bool = False) -> str:
    """Map the OBSERVED hwdec-current (+ who chose CPU) to an indicator
    state: "gpu" | "cpu" | "cpu_auto" | "idle".

    `user_cpu` — CPU was forced by the user for this video (R7-B toggle);
    `auto_cpu` — CPU was reached by the automatic watchdog/engagement
    fallback.  A user override outranks the automatic marker; automatic
    CPU is NEVER labelled as if the user chose it.  No configured mode is
    an input, by construction."""
    import watchdog as WD
    eff = WD.decode_effective(hwdec_current)
    if eff == "on":
        return "gpu"
    if eff == "off":
        return "cpu_auto" if (auto_cpu and not user_cpu) else "cpu"
    return "idle"


def decode_click_action(state: str) -> str:
    """What a click on the control WILL do, phrased from the current
    state (R7-A tooltip / a11y)."""
    if state == "gpu":
        return "click: force CPU decode for this video only"
    return "click: attempt the configured engine again (GPU)"


def decode_tooltip(state: str, hwdec_current) -> str:
    """Tooltip naming the exact observed value, the user-visible state and
    what a click will do (R7-A)."""
    obs = (f"hwdec-current: {hwdec_current}" if hwdec_current not in
           (None, "") else "hwdec-current: not observed yet")
    if state == "gpu":
        head = "Hardware decode ON"
    elif state == "cpu":
        head = "CPU decode (your choice — this video)"
    elif state == "cpu_auto":
        head = "CPU decode (automatic fallback)"
    else:
        head = "Decoder not reported yet"
    return f"{head}  —  {obs}  —  {decode_click_action(state)}"


def playpause_icon(playing: bool) -> str:
    """Icon name for the play/pause button — mapped from the OBSERVED
    pause flag, never from local bookkeeping."""
    return ("media-playback-pause-symbolic" if playing
            else "media-playback-start-symbolic")


def mute_icon(muted: bool) -> str:
    """Icon name for the mute button — mapped from the OBSERVED mute flag
    (config audio.muted is applied at load, so this also proves it)."""
    return ("audio-volume-muted-symbolic" if muted
            else "audio-volume-high-symbolic")


def _mpv_loop_on(value) -> bool:
    """mpv loop-* values are strings: "no" | "inf" | "force" | "N" (or None
    before first observation).  Anything that loops counts as on."""
    if value is None:
        return False
    s = str(value)
    if s in ("no", "nil"):
        return False
    if s in ("inf", "force"):
        return True
    try:
        return float(s) > 0
    except ValueError:
        return False


def repeat_mode(loop_file, loop_playlist) -> str:
    """Derive the repeat mode from the two mpv property values.
    loop-file wins over loop-playlist: with one item looped, looping that
    item IS 'one', whatever the playlist flag says."""
    if _mpv_loop_on(loop_file):
        return "one"
    if _mpv_loop_on(loop_playlist):
        return "all"
    return "off"


def repeat_props(mode):
    """(loop-file, loop-playlist) strings to WRITE for a mode — always
    both, so cycling never leaves two loops stacked."""
    if mode == "one":
        return ("inf", "no")
    if mode == "all":
        return ("no", "inf")
    return ("no", "no")


class ChromeState:
    """Pure state machine, injected clock (ms) — no GTK, fully unit-tested."""

    def __init__(self, timeout_ms: float = IDLE_MS_DEFAULT, clock=None):
        self.clock = clock or (lambda: time.monotonic() * 1000.0)
        self.timeout_ms = float(timeout_ms)
        self.playing = True
        self.bottom_hover = False
        self._last_motion_ms = self.clock()
        self.visible = True
        self.reason = "start"

    # ---------------------------------------------------------------- inputs
    def _t(self, now_ms):
        return self.clock() if now_ms is None else float(now_ms)

    def on_pointer_motion(self, now_ms=None) -> None:
        self._last_motion_ms = self._t(now_ms)
        self._show("motion")

    def on_pointer_click(self, now_ms=None) -> None:
        self._last_motion_ms = self._t(now_ms)
        self._show("click")

    def on_pointer_leave(self, now_ms=None) -> None:
        # leaving re-arms the countdown ONE last time, drops hover state
        self._last_motion_ms = self._t(now_ms)
        self.bottom_hover = False

    def on_playing_changed(self, playing: bool, now_ms=None) -> None:
        self.playing = bool(playing)
        self._last_motion_ms = self._t(now_ms)   # fresh countdown on either edge
        if not self.playing:
            self._show("not-playing")

    def on_key(self, now_ms=None) -> None:
        """Keyboard NEVER re-arms the timer (§4.6) — no-op for the state."""

    def set_bottom_hover(self, inside: bool, now_ms=None) -> None:
        inside = bool(inside)
        if inside != self.bottom_hover:
            self.bottom_hover = inside
            # both edges re-arm: leaving restarts the countdown from release
            self._last_motion_ms = self._t(now_ms)
            if inside:
                self._show("hover")

    # ---------------------------------------------------------------- output
    def _show(self, reason: str) -> None:
        self.visible = True
        self.reason = reason

    def tick(self, now_ms=None):
        """Returns (visible, reason)."""
        t = self._t(now_ms)
        if not self.playing:
            self._show("not-playing")
        elif self.bottom_hover:
            self._show("hover")
        else:
            dt = t - self._last_motion_ms
            if dt < 0.0 or dt > self.timeout_ms * JUMP_FACTOR:
                self._last_motion_ms = t          # clock jump: re-arm, keep
                self._show("clock-jump")
            elif dt >= self.timeout_ms:
                self.visible = False
                self.reason = "idle"
            # else: state unchanged
        return self.visible, self.reason


# =============================================================================
# GTK side — imported only in the app (render-style top-level gi, safe headless)
# =============================================================================
import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Graphene", "1.0")
from gi.repository import Gdk, GLib, Gtk, Graphene  # noqa: E402


def _rect(x, y, w, h):
    """Graphene.Rect for snapshot.add/append_color (init returns self)."""
    return Graphene.Rect().init(float(x), float(y), float(w), float(h))

BLACK_CSS = b"""
window.reelsee-black { background-color: #000000; }
/* R5-A: the real titlebar rides the same OLED-black skin (Looksee 13.1.1:
 * the class goes on window + headerbar ONLY, never on popovers/buttons). */
headerbar.reelsee-black {
    background-color: #000000;
    color: #e6e6e6;
    box-shadow: none;
    border-bottom: none;
}

/* R5-C: repeat toggle, off state = same glyph dimmed */
button.reelsee-repeat-off,
button.reelsee-repeat-off image { opacity: 0.45; }

/* R7: decode indicator - CPU is the dimmed state, GPU reads normal.
 * (Text button; the label node carries the text.) */
button.reelsee-decode-cpu { opacity: 0.55; }
button.reelsee-decode-cpu label { opacity: 0.8; }

box.reelsee-chrome {
    background-color: alpha(#000000, 0.78);
    transition-property: opacity;
    transition-duration: 180ms;
}
label.reelsee-chrome, button.reelsee-chrome { color: #e6e6e6; }
button.reelsee-chrome { background: transparent; border: 1px solid #3a3a3a;
                        border-radius: 4px; padding: 1px 7px; }
button.reelsee-chrome:hover { border-color: #9a9a9a; }
label.reelsee-osd { color: #f2f2f2; background-color: alpha(#000000, 0.65);
                    border-radius: 4px; padding: 2px 10px; }
scale.reelsee-chrome trough { min-height: 4px; background-color: #444444; }
scale.reelsee-chrome highlight { background-color: #e6e6e6; }
/* R3-P3(N4): the Default theme sizes sliders as min-*=18px with
 * margin:-9px, so shrinking min-size to 10px left the slider gizmo
 * measuring 10 - 2*9 = -8: GTK then warns "GtkGizmo (slider) reported
 * min height/width -8" on every startup.  Clamping the margin to 0 keeps
 * the reported measurement >= 0. */
scale.reelsee-chrome slider { background-color: #e6e6e6; min-width: 10px;
                              min-height: 10px; border-radius: 5px;
                              margin: 0px; }
button.reelsee-ab-on { background-color: #ffffff; color: #000000;
                       border-color: #ffffff; }
"""

_installed_displays: list = []


def install_black_css() -> None:
    """ONE scoped provider per display (Looksee pattern, PLAN §3 row): class
    `reelsee-black`, strictly monochrome."""
    display = Gdk.Display.get_default()
    if display is None:
        return
    for d, _p in _installed_displays:
        if d is display:
            return
    provider = Gtk.CssProvider()
    provider.load_from_data(BLACK_CSS)
    Gtk.StyleContext.add_provider_for_display(
        display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    _installed_displays.append((display, provider))


def fmt_time(seconds) -> str:
    try:
        s = int(max(0.0, float(seconds)))
    except (TypeError, ValueError):
        return "--:--"
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


class _SeekScale(Gtk.Scale):
    """Seek bar that draws A/B markers (§5.4): two 3 px white ticks plus a
    12 %-white band between them.  Markers are stored in SECONDS; the
    fraction is computed at snapshot time from the current duration, so the
    drawing follows window resizes and late-arriving durations for free."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.ab_a: float | None = None
        self.ab_b: float | None = None
        self.ab_dur: float = 0.0

    def set_ab(self, a, b) -> None:
        self.ab_a = a
        self.ab_b = b
        self.queue_draw()

    def set_dur(self, dur: float) -> None:
        if dur and dur != self.ab_dur:
            self.ab_dur = float(dur)
            self.queue_draw()

    def _x(self, frac: float, width: float) -> float:
        inset = min(13.0, width / 4.0)       # slider handle travel margin
        return inset + max(0.0, min(1.0, frac)) * (width - 2.0 * inset)

    def do_snapshot(self, snapshot):  # noqa: N802 (GTK virtual)
        Gtk.Scale.do_snapshot(self, snapshot)
        if self.ab_a is None or self.ab_dur <= 0:
            return
        w, h = self.get_width(), self.get_height()
        if w <= 0 or h <= 0:
            return
        xa = self._x(self.ab_a / self.ab_dur, w)
        if self.ab_b is not None:
            xb = self._x(self.ab_b / self.ab_dur, w)
            white12 = Gdk.RGBA()
            white12.parse("#ffffff")
            white12.alpha = 0.12
            snapshot.append_color(white12, _rect(xa, 0, xb - xa, h))
            white = Gdk.RGBA()
            white.parse("#ffffff")
            for x in (xa, xb):
                snapshot.append_color(white, _rect(x - 1.5, 0, 3.0, h))
        else:
            white = Gdk.RGBA()
            white.parse("#ffffff")
            snapshot.append_color(white, _rect(xa - 1.5, 0, 3.0, h))


class Chrome:
    """Owns the bottom bar, OSD + toast labels and the ChromeState.

    `window` is the ReelseeWindow (used for GLib timers, cursor, actions).
    """

    SPEED_PRESETS = (1.0, 1.25, 1.5, 2.0, 0.5, 0.75)

    def __init__(self, window, cfg, on_seek=None, on_volume=None,
                 on_speed=None, on_ab=None, on_repeat=None, on_decode=None):
        self.win = window
        self.cfg = cfg
        self.on_seek = on_seek
        self.on_volume = on_volume
        self.on_speed = on_speed
        self.on_ab = on_ab
        self.on_repeat = on_repeat
        self.on_decode = on_decode
        # R9-A: the first paint shows the PERSISTED mode (icon AND dim —
        # _update_repeat reads this seed), so a restart never flickers
        # through "off".  Not a parallel truth: it is this widget's own
        # display cache, and update_snapshot re-derives it from the mpv
        # properties every frame — the seeded engine init options make
        # the first observed value agree.  Unknown stored value → "off".
        self._repeat_mode = repeat_mode(cfg.get("playback.loop_file"),
                                        cfg.get("playback.loop_playlist"))
        self.state = ChromeState(float(cfg.get("ui.chrome_timeout_ms", 3000)))
        self.osd_ms = float(cfg.get("ui.osd_ms", 1200))
        self._dragging = False
        self._osd_deadline = 0.0
        self._toast_deadline = 0.0
        self._cursor_blank = False
        self._last_playing = None
        self._bars_visible = True
        self._preview_frac = None
        self._menu_open = 0      # R5-A: open popovers block retraction

        # ---- bottom bar: R6-B final order (BOTTOM_ORDER) --------------------
        self.bottom = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL,
                              spacing=8)
        self.bottom.add_css_class("reelsee-chrome")
        # R6-B FIRST: play/pause — an ICON button whose icon/tooltip/a11y
        # label are painted from the OBSERVED snapshot in update_snapshot,
        # activating the SAME win.play-pause action that Space uses.
        self.btn_play = Gtk.Button()
        self.btn_play.set_name(BOTTOM_ORDER[0])
        self.btn_play.add_css_class("reelsee-chrome")
        self._play_icon = Gtk.Image.new_from_icon_name(playpause_icon(False))
        self.btn_play.set_child(self._play_icon)
        self.btn_play.set_action_name("win.play-pause")
        self._pp_state = None
        self._paint_playpause(False)
        self.pos_label = Gtk.Label(label="0:00")
        self.pos_label.set_name("position")
        self.pos_label.add_css_class("reelsee-chrome")
        # §5.4 + §7 T1: transport cluster and A/B live in the left
        # cluster.  R6-B: the ⏹ stop button is REMOVED from the widget
        # tree entirely (the win.stop action survives for Ctrl+S/palette).
        self.btn_prev = Gtk.Button(label="⏮")
        self.btn_next = Gtk.Button(label="⏭")
        for b, act, tip in ((self.btn_prev, "win.playlist-prev",
                             "Previous item  (b)"),
                            (self.btn_next, "win.playlist-next",
                             "Next item  (n)")):
            b.add_css_class("reelsee-chrome")
            b.set_action_name(act)
            b.set_tooltip_text(tip)
        self.btn_prev.set_name("prev")
        self.btn_next.set_name("next")
        self.ab_a_btn = Gtk.Button(label="A")
        self.ab_b_btn = Gtk.Button(label="B")
        for b, which, tip in ((self.ab_a_btn, "a", "Set A  [  (["),
                              (self.ab_b_btn, "b", "Set B  ]  (])")):
            b.add_css_class("reelsee-chrome")
            b.set_tooltip_text(tip)
            b.connect("clicked",
                      lambda _b, k=which: self.on_ab and self.on_ab(k))
        self.ab_a_btn.set_name("a")
        self.ab_b_btn.set_name("b")
        _adj = Gtk.Adjustment(value=0.0, lower=0.0, upper=1000.0,
                              step_increment=1.0)
        self.seek = _SeekScale(orientation=Gtk.Orientation.HORIZONTAL,
                               adjustment=_adj)
        self.seek.add_css_class("reelsee-chrome")
        self.seek.set_draw_value(False)
        self.seek.set_hexpand(True)
        self.seek.set_name("seek")
        self.dur_label = Gtk.Label(label="--:--")
        self.dur_label.add_css_class("reelsee-chrome")
        self.dur_label.set_name("duration")
        # R5-C: repeat toggle directly beside the progress bar — icon
        # cycles off -> all -> one -> off; "off" reuses the same glyph dimmed.
        self.btn_repeat = Gtk.Button()
        self.btn_repeat.add_css_class("reelsee-chrome")
        self._repeat_icon = Gtk.Image.new_from_icon_name(
            REPEAT_ICONS["off"])
        self.btn_repeat.set_child(self._repeat_icon)
        self.btn_repeat.set_name("repeat")
        self.btn_repeat.connect("clicked",
                                lambda *_a: self.on_repeat
                                and self.on_repeat())
        self._update_repeat()
        self.speed_btn = Gtk.Button(label="1.00\u00d7")
        self.speed_btn.add_css_class("reelsee-chrome")
        self.speed_btn.set_name("speed")
        self.speed_btn.connect("clicked", self._on_speed_click)
        self.vol = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL,
                                            0.0, 200.0, 1.0)
        self.vol.add_css_class("reelsee-chrome")
        self.vol.set_draw_value(False)
        self.vol.set_size_request(90, -1)
        self.vol.set_name("volume")
        # R6-B: mute directly beside the volume — the SAME
        # win.toggle-mute action as the "m" key (writes audio.muted and
        # clears the transient focus-mute through _act_mute).
        self.btn_mute = Gtk.Button()
        self.btn_mute.set_name("mute")
        self.btn_mute.add_css_class("reelsee-chrome")
        self._mute_icon = Gtk.Image.new_from_icon_name(mute_icon(False))
        self.btn_mute.set_child(self._mute_icon)
        self.btn_mute.set_action_name("win.toggle-mute")
        self._mute_state = None
        self._paint_mute(False)
        # R7 LAST (rightmost end of the bottom bar, after mute): the decode
        # indicator + per-video GPU/CPU toggle.  The label/tooltip/a11y are
        # painted from the OBSERVED snapshot (decode_state); activation goes
        # through the on_decode callback the window wires (one action for
        # both roles — indicator and toggle).
        self.btn_decode = Gtk.Button(label=DECODE_LABELS["idle"])
        self.btn_decode.set_name(BOTTOM_ORDER[-1])
        self.btn_decode.add_css_class("reelsee-chrome")
        self.btn_decode.connect("clicked",
                                lambda *_a: self.on_decode
                                and self.on_decode())
        self._decode_state = None
        self._decode_flags = (False, False)   # (user_cpu, auto_cpu)
        self._paint_decode(decode_state(None), None)
        for _tag in BOTTOM_ORDER:
            self.bottom.append(getattr(self, {
                "play_pause": "btn_play", "prev": "btn_prev",
                "next": "btn_next", "a": "ab_a_btn", "b": "ab_b_btn",
                "position": "pos_label", "seek": "seek",
                "duration": "dur_label", "repeat": "btn_repeat",
                "speed": "speed_btn", "volume": "vol",
                "mute": "btn_mute",
                "decode": "btn_decode"}[_tag]))
        self.seek.connect("change-value", self._on_seek_change)
        self.vol.connect("change-value", self._on_vol_change)

        # hover preview for the seek bar (label floating above the bar)
        self.preview = Gtk.Label(label="")
        self.preview.add_css_class("reelsee-osd")
        self.preview.set_valign(Gtk.Align.END)
        self.preview.set_margin_bottom(56)
        self.preview.set_visible(False)

        # R8-A: the in-video TOP MENU BAR is gone — entirely.  No bar, no
        # filename label, no audio/subs/playlist/chapters/tracks buttons
        # exist for ANY config value; every feature lives in the right-click
        # context menu (ctxmenu.build) and the window title keeps the name.

        # ---- OSD (transient, keyboard feedback) + toast ---------------------
        self.osd = Gtk.Label(label="")
        self.osd.add_css_class("reelsee-osd")
        self.osd.set_valign(Gtk.Align.CENTER)
        self.osd.set_halign(Gtk.Align.START)
        self.osd.set_margin_start(16)
        self.osd.set_visible(False)
        self.toast = Gtk.Label(label="")
        self.toast.add_css_class("reelsee-osd")
        self.toast.set_valign(Gtk.Align.START)
        self.toast.set_halign(Gtk.Align.CENTER)
        self.toast.set_margin_top(16)
        self.toast.set_visible(False)

        # motion over the seek scale drives the hover preview
        mc = Gtk.EventControllerMotion()
        mc.connect("motion", self._on_seek_motion)
        mc.connect("leave", lambda *_a: self.preview.set_visible(False))
        self.seek.add_controller(mc)

    # ------------------------------------------------------------- overlays
    def overlay_children(self):
        """(preview, osd, toast, bottom) — window adds them to its overlay.

        R8-A: the deleted top menu bar is NOT in this list for ANY config
        value — the bar and every child of it simply do not exist."""
        self.bottom.set_valign(Gtk.Align.END)
        self.osd.set_valign(Gtk.Align.CENTER)
        return (self.preview, self.osd, self.toast, self.bottom)

    # ---------------------------------------------------------------- motion
    def motion(self, x: float, y: float, width: float, height: float) -> None:
        now = self.state.clock()
        self.state.on_pointer_motion(now)
        self.state.set_bottom_hover(height - y <= BOTTOM_HOVER_PX, now)

    def click(self) -> None:
        self.state.on_pointer_click(self.state.clock())

    def leave(self) -> None:
        self.state.on_pointer_leave(self.state.clock())

    def key(self) -> None:
        self.state.on_key(self.state.clock())

    def wake(self) -> None:
        """Re-arm the countdown fresh and reveal (state edges such as
        entering/leaving fullscreen use this — same decision path as motion)."""
        self.state.on_pointer_motion(self.state.clock())

    # ------------------------------------------------------------ R5-A menus
    def menu_opened(self) -> None:
        """A popover opened: hold the chrome revealed (stranding a menu by
        retracting its anchor bar is worse than a late hide) and re-arm."""
        self._menu_open += 1
        self.state.on_pointer_motion(self.state.clock())

    def menu_closed(self) -> None:
        self._menu_open = max(0, self._menu_open - 1)
        if self._menu_open == 0:
            # re-arm the idle timer on close (R5-A): the countdown restarts
            self.state.on_pointer_motion(self.state.clock())

    @property
    def menu_open(self) -> bool:
        return self._menu_open > 0

    @property
    def bars_visible(self) -> bool:
        """Current applied chrome visibility (bottom bar + reveal mirror)."""
        return self._bars_visible

    # ------------------------------------------------------------ visibility
    def tick(self) -> bool:
        """Apply ChromeState to the widgets. Returns current visibility.

        R5-A: the returned visibility is also THE decision mirrored onto the
        window's Adw.ToolbarView (set_reveal_top_bars) — one decision, one
        animation source.  An open popover holds it revealed (menu must
        never be stranded by a retracting anchor bar)."""
        visible, _reason = self.state.tick()
        if self._menu_open:
            visible = True
        changed = visible is not self._bars_visible
        for bar in (self.bottom,):
            bar.set_child_visible(visible)
            bar.set_opacity(1.0 if visible else 0.0)
            # GTK overlay damage bug (P5 finding): a queue_draw() landing
            # while a bar is child-invisible (seek.value follows time-pos
            # the whole time playback runs) leaves the bar UNPAINTED when
            # it is re-shown — neither the child-visible flip nor any
            # later queue_draw repairs it.  A same-frame visible False/
            # True nudge on the overlay child forces the subtree to
            # revalidate+repaint (verified minimal repro, GTK 4.22).
            if changed and visible:
                bar.set_visible(False)
                bar.set_visible(True)
        self._bars_visible = visible
        self._blank_cursor(visible is False)
        # OSD auto-hide (transient, independent of the bars)
        now = self.state.clock()
        if self._osd_deadline and now >= self._osd_deadline:
            self._osd_deadline = 0.0
            self.osd.set_visible(False)
        if self._toast_deadline and now >= self._toast_deadline:
            self._toast_deadline = 0.0
            self.toast.set_visible(False)
        return visible

    def _blank_cursor(self, blank: bool) -> None:
        if blank == self._cursor_blank:
            return
        self._cursor_blank = blank
        gw = self.win.get_surface()
        if gw is None:
            return
        if blank:
            try:
                cur = Gdk.Cursor.new_from_name(self.win.get_display(), "none")
                gw.set_cursor(cur)
            except Exception:
                pass
        else:
            gw.set_cursor(None)

    # --------------------------------------------------------------- repeat
    def set_repeat(self, mode: str) -> None:
        """Apply a repeat mode to the button (idempotent; snapshot path
        calls this every frame with the DERIVED mode)."""
        if mode not in REPEAT_MODES:
            mode = "off"
        if mode == self._repeat_mode and \
                self._repeat_icon.get_icon_name() == REPEAT_ICONS[mode]:
            return
        self._repeat_mode = mode
        self._update_repeat()

    def _update_repeat(self) -> None:
        mode = self._repeat_mode
        self._repeat_icon.set_from_icon_name(REPEAT_ICONS[mode])
        label = "Repeat: " + mode          # tooltip + a11y name
        self.btn_repeat.set_tooltip_text(label)
        self.btn_repeat.update_property(
            [Gtk.AccessibleProperty.LABEL], [label])
        if mode == "off":
            self.btn_repeat.add_css_class("reelsee-repeat-off")
        else:
            self.btn_repeat.remove_css_class("reelsee-repeat-off")

    # ------------------------------------------------------------------ OSD
    def show_osd(self, text: str) -> None:
        self.osd.set_label(text)
        self.osd.set_visible(True)
        self._osd_deadline = self.state.clock() + self.osd_ms

    def hide_osd(self) -> None:
        """Explicit OSD dismissal (R2-P3 cancel path — "Resolving URL…"
        must disappear the moment the resolve ends or is cancelled)."""
        self.osd.set_visible(False)
        self._osd_deadline = 0.0

    def set_ab(self, a, b) -> None:
        """Push A/B endpoints (seconds or None) to bar markers + buttons."""
        self.seek.set_ab(a, b)
        for btn, val in ((self.ab_a_btn, a), (self.ab_b_btn, b)):
            if val is not None:
                btn.add_css_class("reelsee-ab-on")
            else:
                btn.remove_css_class("reelsee-ab-on")

    def show_toast(self, text: str, ms: float = 4000.0) -> None:
        self.toast.set_label(text)
        self.toast.set_visible(True)
        self._toast_deadline = self.state.clock() + ms

    def keyboard_seek_osd(self, snap: dict) -> None:
        """§4.6: keyboard seek while hidden → transient OSD, bars stay hidden."""
        pos = snap.get("time-pos")
        dur = snap.get("duration")
        speed = snap.get("speed", 1.0)
        try:
            sp = f"{float(speed):.2f}\u00d7"
        except (TypeError, ValueError):
            sp = "?"
        self.show_osd(f"{fmt_time(pos)} / {fmt_time(dur)}  {sp}")

    # -------------------------------------------------------------- snapshot
    def _paint_playpause(self, playing: bool) -> None:
        """Icon, tooltip and accessible name all track the OBSERVED state
        (R6-B); repaint only on an actual flip so snapshot ticks do not
        churn the a11y tree."""
        if self._pp_state is playing:
            return
        self._pp_state = playing
        self._play_icon.set_from_icon_name(playpause_icon(playing))
        label = "Pause" if playing else "Play"
        self.btn_play.set_tooltip_text(f"{label}  (Space)")
        try:
            self.btn_play.update_property(
                [Gtk.AccessibleProperty.LABEL], [label])
        except Exception:                           # noqa: BLE001
            pass                                    # a11y is best-effort

    def _paint_mute(self, muted: bool) -> None:
        if self._mute_state is muted:
            return
        self._mute_state = muted
        self._mute_icon.set_from_icon_name(mute_icon(muted))
        label = "Unmute" if muted else "Mute"
        self.btn_mute.set_tooltip_text(f"{label}  (m)")
        try:
            self.btn_mute.update_property(
                [Gtk.AccessibleProperty.LABEL], [label])
        except Exception:                           # noqa: BLE001
            pass

    # ---------------------------------------------------------------- decode
    def set_decode_flags(self, user_cpu: bool, auto_cpu: bool) -> None:
        """Push WHO chose CPU (R7-B): the window owns the lifecycle —
        user override (per-video toggle) and the automatic fallback marker.
        Repaints immediately using the LAST OBSERVED value (labels never
        run ahead of the snapshot; the snapshot path repaints anyway)."""
        if self._decode_flags == (bool(user_cpu), bool(auto_cpu)):
            return
        self._decode_flags = (bool(user_cpu), bool(auto_cpu))
        self._paint_decode(decode_state(self._decode_observed,
                                        *self._decode_flags),
                           self._decode_observed)

    def _paint_decode(self, state: str, hwdec_current) -> None:
        """Label/tooltip/a11y from the OBSERVED value only; dimmed while
        on CPU (chrome skin), normal on GPU."""
        key = (state, str(hwdec_current))
        if self._decode_state == key:
            return
        self._decode_state = key
        self._decode_observed = hwdec_current
        self.btn_decode.set_label(DECODE_LABELS.get(state, "…"))
        self.btn_decode.set_tooltip_text(decode_tooltip(state,
                                                        hwdec_current))
        if state in ("cpu", "cpu_auto"):
            self.btn_decode.add_css_class("reelsee-decode-cpu")
        else:
            self.btn_decode.remove_css_class("reelsee-decode-cpu")
        a11y = {"gpu": "Hardware decode: GPU (click for CPU this video)",
                "cpu": "CPU decode, user choice (click to try GPU again)",
                "cpu_auto":
                    "CPU decode, automatic fallback "
                    "(click to try GPU again)",
                "idle": "Decode mode unknown yet "
                        "(click to attempt the configured engine)"}[state]
        try:
            self.btn_decode.update_property(
                [Gtk.AccessibleProperty.LABEL], [a11y])
        except Exception:                           # noqa: BLE001
            pass                                    # a11y is best-effort

    def update_snapshot(self, snap: dict) -> None:
        playing = bool(snap.get("pause")) is False and \
            bool(snap.get("idle-active")) is False and \
            bool(snap.get("eof-reached")) is False
        self._paint_playpause(playing)
        self._paint_mute(bool(snap.get("mute")))
        # R7: decode indicator — derived EVERY tick from the observed
        # hwdec-current through the ONE classifier (never the config).
        _obs = snap.get("hwdec-current")
        self._paint_decode(decode_state(_obs, *self._decode_flags), _obs)
        if self._last_playing is not playing:
            self._last_playing = playing
            self.state.on_playing_changed(playing)
        dur = snap.get("duration")
        pos = snap.get("time-pos")
        try:
            durf = float(dur) if dur else 0.0
        except (TypeError, ValueError):
            durf = 0.0
        self.dur_label.set_label(fmt_time(durf))
        if durf > 0:
            self.seek.set_dur(durf)
        if pos is not None and not self._dragging:
            self.pos_label.set_label(fmt_time(pos))
            if durf > 0:
                self.seek.set_value(min(1000.0, float(pos) / durf * 1000.0))
        vol = snap.get("volume")
        if vol is not None:
            self.vol.set_value(float(vol))
        self.speed_btn.set_label(f"{float(snap.get('speed', 1.0) or 1.0):.2f}\u00d7")
        # R5-C: icon state DERIVED from the two mpv loop properties -
        # a raw "l"/"r" keypress repaints the button via this path.
        self.set_repeat(repeat_mode(snap.get("loop-file"),
                                    snap.get("loop-playlist")))
        # R8-A: the old top-bar filename label is gone with the bar; the
        # window title + headerbar title_widget (window._update_title) are
        # the ONE place the filename shows.

    # ------------------------------------------------------------- handlers
    def _on_seek_change(self, _scale, _scroll, value) -> bool:
        self._dragging = True
        dur = self.win.core.snapshot.get("duration") or 0.0
        target = float(value) / 1000.0 * float(dur)
        if self.on_seek is not None:
            self.on_seek(target)
        GLib.timeout_add(120, self._end_drag)
        return False

    def _end_drag(self) -> bool:
        self._dragging = False
        return False

    def _on_vol_change(self, _scale, _scroll, value) -> bool:
        v = float(value)
        self.vol.set_value(v)
        if self.on_volume is not None:
            self.on_volume(v)
        return True

    def _on_speed_click(self, _btn) -> None:
        cur = float(self.win.core.snapshot.get("speed", 1.0) or 1.0)
        idx = 0
        for i, p in enumerate(self.SPEED_PRESETS):
            if abs(p - cur) < 0.01:
                idx = i
                break
        nxt = self.SPEED_PRESETS[(idx + 1) % len(self.SPEED_PRESETS)]
        if self.on_speed is not None:
            self.on_speed(nxt)

    def _on_seek_motion(self, _ctrl, x, _y) -> None:
        dur = self.win.core.snapshot.get("duration") or 0.0
        if not dur:
            return
        adj = self.seek.get_adjustment()
        frac = (x / max(1, self.seek.get_width())) * \
            (adj.get_upper() - adj.get_lower()) + adj.get_lower()
        frac = min(1.0, max(0.0, frac / 1000.0))
        self.preview.set_label(fmt_time(frac * float(dur)))
        self.preview.set_visible(True)
        self.preview.set_halign(Gtk.Align.START)
        self.preview.set_margin_start(int(x))
