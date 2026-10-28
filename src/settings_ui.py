"""Settings (PLAN §8 P4): Adw preferences window — Playback / Audio /
Decode & GPU / Subtitles / Interface / Advanced mpv.

The page content is generated from the pure BINDINGS registry below: exactly
ONE widget per schema leaf key (tests/test_settings_bind.py lints this against
config.schema_leaf_keys()).  Changes go through cfg.set (debounced + atomic,
immediate for R-4 keys) and are live-applied to every open window.

The GTK part builds only; all semantics live in the registry + apply_live().
"""
from __future__ import annotations

import config as C

PAGES: tuple[tuple[str, str], ...] = (
    ("playback", "Playback"),
    ("audio", "Audio"),
    ("video", "Video"),
    ("decode", "Decode & GPU"),
    ("subtitles", "Subtitles"),
    ("interface", "Interface"),
    ("advanced", "Advanced mpv"),
)

# One entry per schema leaf key.  kind: switch | scale | combo | entry |
# options (dict editor) | fallback (read-only status row).
# P6 swaps engine.decoder/renderer_override kinds to live GPU selectors — the
# one-binding-per-key invariant is asserted in tests/test_settings_bind.py.
BINDINGS: tuple[dict, ...] = (
    # ---- Playback ----
    dict(key="playback.speed", page="playback", kind="scale",
         label="Speed", minimum=0.1, maximum=4.0, step=0.05, digits=2),
    dict(key="playback.loop_file", page="playback", kind="entry",
         label="Loop file (no | inf | N)"),
    dict(key="playback.loop_playlist", page="playback", kind="entry",
         label="Loop playlist (no | inf | N)"),
    dict(key="playback.autoplay", page="playback", kind="switch",
         label="Autoplay next item"),
    dict(key="playback.resume", page="playback", kind="switch",
         label="Resume playback"),
    dict(key="playback.resume_min_s", page="playback", kind="scale",
         label="Resume only past (s)", minimum=0, maximum=3600, step=10,
         digits=0),
    dict(key="playback.preload_next", page="playback", kind="switch",
         label="Preload next item"),
    dict(key="playback.keep_open", page="playback", kind="switch",
         label="Keep window open at EOF"),
    dict(key="ab.count", page="playback", kind="entry",
         label="A/B repeat count (inf | N)"),
    dict(key="playback.pitch", page="playback", kind="switch",
         label="Preserve pitch at other speeds"),
    dict(key="playback.video_off", page="playback", kind="switch",
         label="Audio-only (video off)"),
    dict(key="playback.cover_art", page="playback", kind="combo",
         label="Cover art for audio files",
         choices=("no", "exact", "albumart", "any", "all")),
    dict(key="playback.ytdlp", page="playback", kind="switch",
         label="Resolve URLs with yt-dlp (if installed)"),
    dict(key="playback.ytdlp_timeout", page="playback", kind="scale",
         label="URL resolve timeout (s)", minimum=1.0, maximum=30.0,
         step=0.5, digits=1),
    # ---- Audio ----
    dict(key="audio.muted", page="audio", kind="switch", label="Muted"),
    dict(key="audio.volume", page="audio", kind="scale", label="Volume",
         minimum=0, maximum=200, step=1, digits=0),
    dict(key="audio.focus_mute", page="audio", kind="switch",
         label="Mute unfocused windows"),
    dict(key="audio.exclusive", page="audio", kind="switch",
         label="Exclusive audio"),
    dict(key="audio.delay_s", page="audio", kind="scale",
         label="Audio delay (s)", minimum=-60, maximum=60, step=0.1,
         digits=1),
    dict(key="audio.device", page="audio", kind="entry",
         label="Audio device (auto | alsa/… | pipewire/…)"),
    # ---- Video ----
    dict(key="video.aspect", page="video", kind="entry",
         label="Aspect override (no | 16:9 | 1.7777)"),
    dict(key="video.zoom", page="video", kind="scale", label="Zoom",
         minimum=-1.0, maximum=4.0, step=0.01, digits=2),
    dict(key="video.pan_x", page="video", kind="scale", label="Pan X",
         minimum=-1.0, maximum=1.0, step=0.01, digits=2),
    dict(key="video.pan_y", page="video", kind="scale", label="Pan Y",
         minimum=-1.0, maximum=1.0, step=0.01, digits=2),
    dict(key="video.rotate", page="video", kind="scale",
         label="Rotate (degrees)", minimum=0.0, maximum=359.0, step=1.0,
         digits=0),
    dict(key="video.crop", page="video", kind="entry",
         label="Crop (WxH+X+Y, empty = off)"),
    dict(key="video.crop_to_window", page="video", kind="switch",
         label="Crop to window (fill, clip overflow)"),
    dict(key="video.brightness", page="video", kind="scale",
         label="Brightness", minimum=-100, maximum=100, step=1, digits=0),
    dict(key="video.contrast", page="video", kind="scale",
         label="Contrast", minimum=-100, maximum=100, step=1, digits=0),
    dict(key="video.saturation", page="video", kind="scale",
         label="Saturation", minimum=-100, maximum=100, step=1, digits=0),
    dict(key="video.gamma", page="video", kind="scale",
         label="Gamma", minimum=-100, maximum=100, step=1, digits=0),
    dict(key="video.hue", page="video", kind="scale",
         label="Hue", minimum=-100, maximum=100, step=1, digits=0),
    dict(key="video.deinterlace", page="video", kind="combo",
         label="Deinterlace", choices=("auto", "yes", "no")),
    dict(key="video.scale_preset", page="video", kind="combo",
         label="Scaler quality preset",
         choices=("default", "fast", "high", "higher")),
    dict(key="video.deband", page="video", kind="switch",
         label="Debanding filter"),
    dict(key="video.dither_depth", page="video", kind="entry",
         label="Dither depth (auto | 8 | 10 | …)"),
    dict(key="video.interpolation", page="video", kind="switch",
         label="Motion interpolation"),
    dict(key="video.tscale", page="video", kind="combo",
         label="Temporal scaler (tscale)",
         choices=("catmull", "lanczos3", "box", "nearest", "oversample")),
    # ---- Decode & GPU ----
    dict(key="engine.decoder", page="decode", kind="engine",
         label="Decoder engine"),
    dict(key="engine.hwdec_pref", page="decode", kind="combo",
         label="Hardware decode preference",
         choices=("vaapi", "vulkan")),
    dict(key="engine.zero_copy", page="decode", kind="switch",
         label="Zero-copy VAAPI decode (may fall back — engages only on "
               "some render paths; copy mode is the reliable default)"),
    dict(key="engine.renderer_override", page="decode", kind="renderer",
         label="Renderer GPU (applies after restart)"),
    # R16-E §E.1/§E.2 (ROUND18 corrected): decode GPU is independent of the
    # render GPU.  Empty = auto (the INTEGRATED GPU — display-bearing or not;
    # the iGPU may carry the monitors — else CPU; never a dGPU); picking a
    # node pins it, which is the documented escape hatch.
    dict(key="playback.vaapi_device", page="decode", kind="vanode",
         label="VAAPI decode node (new windows)",
         note="empty = integrated GPU or CPU"),
    dict(key="engine.cpu_threads", page="decode", kind="scale",
         label="CPU threads (0 = auto)", minimum=0, maximum=32, step=1,
         digits=0),
    # R16-D: how many streams the iGPU decodes at once; windows past it fall
    # back to CPU.  scripts/bench_multidecode.py measures the right value.
    dict(key="engine.decode_capacity", page="decode", kind="scale",
         label="Decode GPU stream capacity", minimum=0, maximum=8, step=1,
         digits=0),
    # R16-B: the performance switch belongs next to the decode controls.
    dict(key="playback.suppress_video_when_hidden", page="decode",
         kind="switch",
         label="Stop decoding video in minimized windows (audio keeps "
               "playing)"),
    dict(key="engine.watchdog_enabled", page="decode", kind="switch",
         label="Auto-fallback on overload"),
    dict(key="engine.watchdog_fps_floor_pct", page="decode", kind="scale",
         label="FPS floor (%)", minimum=50, maximum=100, step=1, digits=0),
    dict(key="engine.watchdog_drop_ceiling_pct", page="decode", kind="scale",
         label="Drop ceiling (%)", minimum=0, maximum=20, step=1, digits=0),
    dict(key="engine.retry_on_new_file", page="decode", kind="switch",
         label="Retry engine on new file"),
    dict(key="engine.last_fallback", page="decode", kind="fallback",
         label="Last fallback"),
    # ---- Subtitles ----
    dict(key="subtitles.visible", page="subtitles", kind="switch",
         label="Show subtitles"),
    dict(key="subtitles.scale", page="subtitles", kind="scale",
         label="Subtitle scale", minimum=0.25, maximum=4.0, step=0.05,
         digits=2),
    dict(key="subtitles.delay_s", page="subtitles", kind="scale",
         label="Subtitle delay (s)", minimum=-60, maximum=60, step=0.1,
         digits=1),
    dict(key="subtitles.auto", page="subtitles", kind="entry",
         label="Auto-load mode"),
    dict(key="subtitles.font", page="subtitles", kind="entry",
         label="Font family"),
    dict(key="subtitles.font_size", page="subtitles", kind="scale",
         label="Font size", minimum=10, maximum=200, step=1, digits=0),
    dict(key="subtitles.color", page="subtitles", kind="entry",
         label="Text colour (#rrggbb)"),
    dict(key="subtitles.border_size", page="subtitles", kind="scale",
         label="Border size", minimum=0, maximum=10, step=0.5, digits=1),
    dict(key="subtitles.margin_y", page="subtitles", kind="scale",
         label="Bottom margin (px)", minimum=0, maximum=300, step=1,
         digits=0),
    # ---- Interface (+ screenshots) ----
    dict(key="ui.chrome_timeout_ms", page="interface", kind="scale",
         label="Chrome hide delay (ms)", minimum=500, maximum=10000,
         step=100, digits=0),
    dict(key="ui.osd_ms", page="interface", kind="scale",
         label="OSD duration (ms)", minimum=300, maximum=5000, step=100,
         digits=0),
    dict(key="ui.seek_step_s", page="interface", kind="scale",
         label="Seek step (s)", minimum=1, maximum=60, step=1, digits=0),
    dict(key="ui.seek_step_long_s", page="interface", kind="scale",
         label="Long seek step (s)", minimum=5, maximum=300, step=5,
         digits=0),
    dict(key="ui.theme", page="interface", kind="combo", label="Theme",
         choices=("black",)),
    dict(key="ui.ontop", page="interface", kind="switch",
         label="Always on top"),
    # R8-A: ui.show_top_menu row DELETED with the bar itself — the setting
    # no longer exists; old configs carrying the key load silently and the
    # key is simply ignored (deep_merge keeps unknown keys, nothing reads
    # it).
    dict(key="ui.lock_aspect", page="interface", kind="switch",
         label="Keep window aspect on resize"),
    # R10: the remembered geometry itself is STATE (window.json), but
    # WHETHER to remember and how far to cascade is user policy.  Both are
    # read live when the NEXT window opens — no restart needed.
    dict(key="ui.remember_geometry", page="interface", kind="switch",
         label="Remember window size and position"),
    dict(key="ui.cascade_offset_px", page="interface", kind="scale",
         label="New-window cascade offset (px, 0 = exactly on top)",
         minimum=0, maximum=200, step=1, digits=0),
    dict(key="ui.title_format", page="interface", kind="entry",
         label="Window title (${media-title} placeholder)"),
    dict(key="ui.screenshot_clipboard", page="interface", kind="switch",
         label="Copy screenshots to clipboard"),
    dict(key="screenshot.directory", page="interface", kind="entry",
         label="Screenshot directory"),
    dict(key="screenshot.format", page="interface", kind="combo",
         label="Screenshot format", choices=("png", "jpeg")),
    dict(key="screenshot.jpeg_quality", page="interface", kind="scale",
         label="JPEG quality", minimum=1, maximum=100, step=1, digits=0),
    # ---- Advanced ----
    dict(key="mpv.options", page="advanced", kind="options",
         label="Raw mpv options (applied at init)"),
)

KINDS = {"switch", "scale", "combo", "entry", "options", "fallback",
         "engine", "renderer",
         # R16-E §E.2: /dev/dri render-node picker (widget swap, count 1)
         "vanode"}


def binding_keys() -> list[str]:
    return [b["key"] for b in BINDINGS]


def validate_bindings(defaults=None) -> list[str]:
    """[] when every schema leaf key has EXACTLY one binding (lint helper)."""
    errs: list[str] = []
    schema = set(C.schema_leaf_keys(defaults))
    bound = binding_keys()
    for k in sorted(schema - set(bound)):
        errs.append(f"unbound: {k}")
    for k in sorted(set(bound) - schema):
        errs.append(f"not-in-schema: {k}")
    seen: set[str] = set()
    for b in BINDINGS:
        if b["key"] in seen:
            errs.append(f"duplicate: {b['key']}")
        seen.add(b["key"])
        pages = {p for p, _t in PAGES}
        if b["page"] not in pages:
            errs.append(f"bad page: {b['key']}")
        if b["kind"] not in KINDS:
            errs.append(f"bad kind: {b['key']}")
        if not b.get("label"):
            errs.append(f"no label: {b['key']}")
        if b["kind"] == "combo" and not b.get("choices"):
            errs.append(f"combo without choices: {b['key']}")
        if b["kind"] == "scale":
            if not (b["minimum"] < b["maximum"]) or b["step"] <= 0:
                errs.append(f"bad scale range: {b['key']}")
    return errs


# Keys that additionally hit every LIVE mpv core immediately (§4 live-apply).
# config.PROPERTY_MAP is the single source of truth (§7 T1 surface); this
# view exists for the P4 lint and diagnostics.
LIVE_PROPERTIES = {k: prop for k, (prop, _kind) in C.PROPERTY_MAP.items()}


def _app_config(app):
    for w in list(getattr(app, "windows", [])):
        cfg = getattr(w, "cfg", None)
        if cfg is not None:
            return cfg
    return getattr(app, "config", None)


def _apply_engine_setting(app, key: str, value) -> None:
    """P6 live-apply for the GPU selectors.  engine.decoder re-runs the R20
    engagement mechanism on every open core; engine.renderer_override goes
    through apply_renderer_override, which re-execs once when the env must
    change (the DRI_PRIME env must predate GL init — §4.1)."""
    import engine as E
    if key == "engine.renderer_override":
        cfg = _app_config(app)
        if cfg is not None:
            E.apply_renderer_override(cfg)
        return
    cfg = _app_config(app)
    if key == "engine.zero_copy":
        # R2-P1: toggling zero-copy re-applies the CURRENT decoder so the
        # live cores switch between vaapi-copy (default) and vaapi at once.
        if cfg is None:
            return
        engines = E.enumerate_engines()
        sel = E.select_engine(cfg.get("engine.decoder", "auto"), engines)
    else:
        engines = E.enumerate_engines()
        sel = E.select_engine(value, engines)
    for w in list(getattr(app, "windows", [])):
        core = getattr(w, "core", None)
        if core is not None:
            E.apply_engine(core, sel, cfg)


def apply_live(app, key: str, value) -> None:
    """Push one changed setting into every open window's core (async set)."""
    if key in ("engine.decoder", "engine.renderer_override",
               "engine.zero_copy"):
        _apply_engine_setting(app, key, value)
    entry = C.PROPERTY_MAP.get(key)
    for w in list(getattr(app, "windows", [])):
        core = getattr(w, "core", None)
        if core is None:
            continue
        if entry is not None:
            prop, kind = entry
            val = C.coerce(kind, value)
            if val is None:
                continue
            if prop == "mute" and getattr(w, "_transient_muted", False):
                val = True          # focus mute (P8) outranks the setting
            core.set_property(prop, val)
        hook = getattr(w, "on_setting_changed", None)   # ui-side hook
        if hook is not None:
            try:
                hook(key, value)
            except Exception:
                pass


# =============================================================================
# GTK / Adw side
# =============================================================================
import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402


def _coerce(kind: str, value):
    if kind == "scale":
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0
    if kind == "switch":
        return bool(value)
    return "" if value is None else str(value)


class SettingsWindow:
    """Adw preferences window bound to cfg; commits on every widget change.

    `app` is the ReelseeApp (used for live-apply + ownership); `on_close`
    optional callback lets smoke tests assert closure.
    """

    def __init__(self, app, cfg, on_close=None):
        # R8-A: schema<->binding coherence is linted against DEFAULTS —
        # cfg.data legitimately PRESERVES unknown keys (deep_merge
        # contract, e.g. old configs carrying a deleted key); opening
        # settings must stay a clean no-op for those, never an assert.
        assert not validate_bindings(), validate_bindings()
        self.app = app
        self.cfg = cfg
        self.widgets: dict[str, object] = {}
        self.win = Adw.PreferencesWindow()
        self.win.set_title("REELSEE Settings")
        self.win.set_modal(False)
        if on_close is not None:
            self.win.connect("close-request",
                             lambda *_a: (on_close(), False)[1])
        groups: dict[str, Adw.PreferencesGroup] = {}
        self.pages: dict[str, Adw.PreferencesPage] = {}
        for page_id, title in PAGES:
            page = Adw.PreferencesPage()
            page.set_title(title)
            for b in BINDINGS:
                if b["page"] != page_id:
                    continue
                gkey = f"{page_id}:main" if b["kind"] != "options" \
                    else f"{page_id}:mpv"
                grp = groups.get(gkey)
                if grp is None:
                    grp = Adw.PreferencesGroup()
                    if gkey.endswith(":mpv"):
                        grp.set_title(b["label"])
                    groups[gkey] = grp
                    page.add(grp)
                self._make_row(b, grp)
            self.pages[page_id] = page
            self.win.add(page)
        # R10 live readout row (values from the observed Snapshot only)
        self._readout_timer = None
        self._readout_row = Adw.ActionRow(title="Live pipeline")
        self._readout_row.set_subtitle(self._readout_text())
        groups["decode:main"].add(self._readout_row)
        self.win.connect("close-request", self._stop_readout)

    def _commit(self, key: str, value) -> None:
        self.cfg.set(key, value)
        apply_live(self.app, key, value)

    # ---------------------------------------------------------------- rows
    def _make_row(self, b: dict, group: Adw.PreferencesGroup) -> None:
        key, kind = b["key"], b["kind"]
        cur = self.cfg.get(key)
        if kind == "switch":
            row = Adw.SwitchRow(title=b["label"], active=bool(cur))
            row.connect("notify::active",
                        lambda r, _ps, k=key: self._commit(k, r.get_active()))
        elif kind == "scale":
            row = Adw.SpinRow.new_with_range(b["minimum"], b["maximum"],
                                             b["step"])
            row.set_title(b["label"])
            digits = b.get("digits", 0)
            if digits and hasattr(row, "set_digits"):
                row.set_digits(digits)
            try:
                row.set_value(float(cur if cur is not None else 0))
            except (TypeError, ValueError):
                pass
            row.connect("notify::value",
                        lambda r, _ps, k=key: self._commit(k, r.get_value()))
        elif kind == "combo":
            choices = list(b["choices"])
            if str(cur) not in choices:
                choices.append(str(cur))
            row = Adw.ComboRow(title=b["label"],
                               model=Gtk.StringList.new(choices))
            row.set_selected(max(0, choices.index(str(cur))))
            row.connect("notify::selected",
                        lambda r, _ps, k=key, ch=choices: self._commit(
                            k, ch[r.get_selected()]))
        elif kind in ("engine", "renderer", "vanode"):
            values, labels = self._selector_choices(kind, str(cur or ""))
            row = Adw.ComboRow(title=b["label"],
                               model=Gtk.StringList.new(labels))
            row.set_selected(values.index(str(cur or "")))
            row.connect("notify::selected",
                        lambda r, _ps, k=key, vs=values: self._commit(
                            k, vs[r.get_selected()]))
        elif kind == "entry":
            title = b["label"]
            if b.get("note"):
                title += f" — {b['note']}"
            row = Adw.EntryRow(title=title, text=str(cur or ""))
            row.connect("apply", lambda r, k=key: self._commit(k, r.get_text()))
            row.connect("notify::text",
                        lambda r, _ps, k=key: self._commit(k, r.get_text()))
        elif kind == "options":
            self._make_options(b, group)
            return
        elif kind == "fallback":
            row = Adw.ActionRow(title=b["label"], subtitle=self._fallback_text())
            self.widgets[key] = row
            group.add(row)
            return
        else:
            raise ValueError(kind)
        self.widgets[key] = row
        group.add(row)

    def _fallback_text(self) -> str:
        fb = self.cfg.get("engine.last_fallback")
        if not isinstance(fb, dict) or not fb:
            return "none"
        return (f"{fb.get('from')} → {fb.get('to')}  "
                f"fps {float(fb.get('fps_ratio') or 0):.0%}  "
                f"drop {float(fb.get('drop_ratio') or 0):.1%}")

    def _make_options(self, b: dict, group: Adw.PreferencesGroup) -> None:
        """mpv.options editor: key rows with value entry + remove, add row."""
        state = {"rows": {}, "extra": []}
        self.widgets[b["key"]] = state

        def rebuild():
            for w in list(state["rows"].values()) + list(state["extra"]):
                w.set_visible(False)
                w.unparent()
            state["rows"].clear()
            state["extra"].clear()
            for k, v in (self.cfg.get("mpv.options") or {}).items():
                row = self._opt_row(k, str(v), rebuild)
                state["rows"][k] = row
                group.add(row)
            add = Adw.EntryRow(title="Add mpv option (name=value)",
                               show_apply_button=True)
            add.connect("apply", self._opt_add)
            state["extra"].append(add)
            group.add(add)

        self._rebuild_opts = rebuild
        rebuild()

    def _opt_row(self, key, value, rebuild):
        row = Adw.ActionRow(title=key)
        vrow = Adw.EntryRow(text=value)

        def _changed(*_a):
            if key in (self.cfg.get("mpv.options") or {}):
                self._commit("mpv.options", {
                    k: (vrow.get_text() if k == key else v)
                    for k, v in (self.cfg.get("mpv.options") or {}).items()})

        vrow.connect("changed", _changed)
        rm = Gtk.Button(label="remove")
        rm.add_css_class("flat")

        def _rm(*_a):
            opts = dict(self.cfg.get("mpv.options") or {})
            opts.pop(key, None)
            self._commit("mpv.options", opts)
            rebuild()
        rm.connect("clicked", _rm)
        row.add_suffix(vrow)
        row.add_suffix(rm)
        self.widgets[key] = vrow          # smoke hook: value entry of key
        return row

    def _opt_add(self, row):
        text = row.get_text().strip()
        k, sep, v = text.partition("=")
        if not sep or not k.strip():
            return
        opts = dict(self.cfg.get("mpv.options") or {})
        opts[k.strip()] = v.strip()
        self._commit("mpv.options", opts)
        row.set_text("")
        self._rebuild_opts()

    # ------------------------------------------------- P6 selectors/readout
    def _engines(self):
        if getattr(self, "_engines_cached", None) is None:
            import engine as E
            self._engines_cached = E.enumerate_engines()
        return self._engines_cached

    def _selector_choices(self, kind: str, cur: str):
        import engine as E
        engines = self._engines()
        if kind == "engine":
            values = [e.id for e in engines]
            labels = [e.label for e in engines]
            # R2-P1 selector honesty: zero-copy vaapi may silently fall
            # back on render paths where it cannot engage — flag it.
            if bool(self.cfg.get("engine.zero_copy", False)):
                labels = [f"{lb} (zero-copy, may fall back)"
                          if e.kind in ("vaapi", "vulkan", "auto") else lb
                          for lb, e in zip(labels, engines)]
        elif kind == "vanode":
            # §E.2: every render node, labelled with its PCI slot and card,
            # plus "" = auto (§E.1).  Enumeration comes from _engines(), which
            # tests replace wholesale — the row must render nodes it did not
            # discover itself.  ROUND18: what keeps a node out of AUTO is
            # being a discrete board add-in, NOT driving a display — the
            # iGPU row must never imply it is skipped for carrying monitors.
            auto = E.pick_decode_node()
            values, labels = [""], ["Auto — integrated GPU, else CPU decode"]
            for e in sorted((x for x in engines if x.render_node),
                            key=lambda x: str(x.pci)):
                tag = ""
                if e.render_node == auto:
                    tag = " — auto picks this"
                elif e.is_discrete:
                    tag = " — discrete GPU (auto never picks it)"
                elif e.is_display_gpu:
                    tag = " — drives the display"
                values.append(e.render_node)
                labels.append(f"{e.render_node} · {e.pci} · "
                              f"{e.label}{tag}")
        else:
            pm = E.prime_index_map()
            gpus = [e for e in engines if e.render_node]
            gpus.sort(key=lambda e: pm.get(E._short(str(e.pci)), 99))
            values, labels = ["auto"], ["Auto (default renderer)"]
            for e in gpus:
                idx = pm.get(E._short(str(e.pci)))
                if idx is None:
                    continue
                values.append(str(idx))
                labels.append(f"GPU {idx} — {e.label}")
        if cur not in values:                 # saved value not enumerated now
            values.append(cur)
            labels.append(f"{cur} (saved)")
        return values, labels

    def _readout_text(self) -> str:
        import engine as E
        snap: dict = {}
        for w in list(getattr(self.app, "windows", [])):
            core = getattr(w, "core", None)
            snapshot = getattr(core, "snapshot", None)
            if snapshot is not None:
                snap = {"hwdec-current": snapshot.get("hwdec-current")}
                break
        return E.readout_text(self._engines(), self.cfg.get("engine.decoder"),
                              snap, self.cfg.get("engine.renderer_override"))

    def _tick_readout(self) -> bool:
        try:
            self._readout_row.set_subtitle(self._readout_text())
        except Exception:                     # noqa: BLE001
            pass
        return True

    def _stop_readout(self, *_a) -> bool:
        if self._readout_timer is not None:
            GLib.source_remove(self._readout_timer)
            self._readout_timer = None
        return False

    def present(self) -> None:
        if self._readout_timer is None:
            self._readout_timer = GLib.timeout_add(500, self._tick_readout)
        self.win.present()

    def close(self) -> None:
        self.win.close()
