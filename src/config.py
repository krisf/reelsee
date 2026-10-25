"""Config schema, atomic load/save, and state stores (PLAN §5.1/§5.2).

Rules (Looksee-parity): missing/corrupt file → per-key defaults, never crash;
unknown keys preserved on write; writes debounced 250 ms and atomic
(tmp + os.replace); mute and volume persist immediately on every user change
(R-4).  Path roots are overridable through REELSEE_CONFIG_DIR /
REELSEE_STATE_DIR (test hook); the runtime default is the real user paths
allowed by R2.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from pathlib import Path

VERSION = 1

DEFAULTS: dict = {
    "version": VERSION,
    "audio": {"muted": True, "volume": 100.0, "focus_mute": True,
              "exclusive": False, "delay_s": 0.0, "device": "auto"},
    "engine": {
        "decoder": "auto",           # auto | cpu | <pci>[:vulkan]
        "hwdec_pref": "vaapi",       # vaapi | vulkan
        "zero_copy": False,          # R2-P1: opt into zero-copy vaapi (may
                                     # fall back — see engine.py mapping)
        "renderer_override": "auto", # auto | DRI_PRIME index (string)
        "cpu_threads": 0,            # 0 = auto
        "watchdog_enabled": True,
        "watchdog_fps_floor_pct": 90,
        "watchdog_drop_ceiling_pct": 3,
        "retry_on_new_file": True,
        "last_fallback": None,       # {from,to,ts,fps_ratio,drop_ratio} (Appendix C)
    },
    "playback": {"speed": 1.0, "loop_file": "no", "loop_playlist": "no",
                 "autoplay": True, "resume": True, "resume_min_s": 300,
                 "preload_next": False, "keep_open": True,
                 "pitch": True, "video_off": False, "cover_art": "albumart",
                 "ytdlp": True, "ytdlp_timeout": 5.0},
    "ab": {"count": "inf"},
    "ui": {"chrome_timeout_ms": 3000, "osd_ms": 1200, "seek_step_s": 5,
           "seek_step_long_s": 30, "theme": "black", "ontop": False,
           "title_format": "${media-title} — REELSEE",
           "screenshot_clipboard": False,
           # R5-B: the in-video top menu bar (playlist/chapters/tracks +
           # filename label).  Off = REMOVED from the widget tree; the
           # headerbar keeps the filename and every action keeps its key.
           "show_top_menu": True},
    "subtitles": {"visible": True, "scale": 1.0, "delay_s": 0.0,
                  "auto": "exact", "font": "sans-serif", "font_size": 55.0,
                  "color": "#ffffff", "border_size": 3.0, "margin_y": 50.0},
    "video": {"aspect": "no", "zoom": 0.0, "pan_x": 0.0, "pan_y": 0.0,
              "rotate": 0.0, "crop": "no", "brightness": 0.0, "contrast": 0.0,
              "saturation": 0.0, "gamma": 0.0, "hue": 0.0,
              "deinterlace": "auto", "scale_preset": "default",
              "deband": False, "dither_depth": "auto", "interpolation": False,
              "tscale": "catmull"},
    "screenshot": {"directory": "~/Pictures", "format": "png", "jpeg_quality": 90},
    "mpv": {"options": {}},           # T2 raw option overrides, applied at init
}

# Keys whose user changes must hit the disk immediately, not debounced (R-4).
PERSIST_NOW_KEYS = {("audio", "muted"), ("audio", "volume")}

DEBOUNCE_S = 0.25

# ------------------------------------------------------- live property map --
# §7 T1: every config leaf that maps 1:1 onto a *runtime-settable* mpv
# property.  Value kinds: "bool" | "float" | "str" | "strno" (empty/None →
# "no").  Keys NOT here (engine.*, ui.chrome_timeout_ms, ab.count,
# video.scale_preset, playback.video_off) are handled by window hooks —
# settings_ui.apply_live pushes everything else to every open core.
PROPERTY_MAP: dict[str, tuple[str, str]] = {
    "audio.muted": ("mute", "bool"),
    "audio.volume": ("volume", "float"),
    "audio.exclusive": ("audio-exclusive", "bool"),
    "audio.delay_s": ("audio-delay", "float"),
    "audio.device": ("audio-device", "str"),
    "playback.speed": ("speed", "float"),
    "playback.pitch": ("audio-pitch-correction", "bool"),
    "playback.loop_file": ("loop-file", "strno"),
    "playback.loop_playlist": ("loop-playlist", "strno"),
    "playback.keep_open": ("keep-open", "bool"),
    "playback.cover_art": ("cover-art-auto", "str"),
    "subtitles.visible": ("sub-visibility", "bool"),
    "subtitles.scale": ("sub-scale", "float"),
    "subtitles.delay_s": ("sub-delay", "float"),
    "subtitles.font": ("sub-font", "str"),
    "subtitles.font_size": ("sub-font-size", "float"),
    "subtitles.color": ("sub-color", "str"),
    "subtitles.border_size": ("sub-border-size", "float"),
    "subtitles.margin_y": ("sub-margin-y", "float"),
    "video.aspect": ("video-aspect-override", "strno"),
    "video.zoom": ("video-zoom", "float"),
    "video.pan_x": ("video-pan-x", "float"),
    "video.pan_y": ("video-pan-y", "float"),
    "video.rotate": ("video-rotate", "float"),
    "video.crop": ("video-crop", "strno"),
    "video.brightness": ("brightness", "float"),
    "video.contrast": ("contrast", "float"),
    "video.saturation": ("saturation", "float"),
    "video.gamma": ("gamma", "float"),
    "video.hue": ("hue", "float"),
    "video.deinterlace": ("deinterlace", "str"),
    "video.deband": ("deband", "bool"),
    "video.dither_depth": ("dither-depth", "str"),
    "video.interpolation": ("interpolation", "bool"),
    "video.tscale": ("tscale", "str"),
    "ui.ontop": ("ontop", "bool"),
    "ui.title_format": ("title", "str"),
    "screenshot.directory": ("screenshot-directory", "str"),
    "screenshot.format": ("screenshot-format", "str"),
    "screenshot.jpeg_quality": ("screenshot-jpeg-quality", "float"),
}


def coerce(kind: str, value):
    """Config value → mpv property value; None means 'skip this push'."""
    try:
        if kind == "bool":
            return bool(value)
        if kind == "float":
            return float(value)
        if kind == "strno":
            s = "" if value is None else str(value).strip()
            return s if s else "no"
        s = "" if value is None else str(value)
        return s if s else None
    except (TypeError, ValueError):
        return None


# §7 T1 "scale quality presets": one dropdown, several mpv scalers at once
# (algorithm names all valid mpv scalers; tscale stays a separate setting).
SCALE_PRESETS: dict[str, dict[str, str]] = {
    "default": {"scale": "bilinear", "cscale": "bilinear",
                "dscale": "bilinear"},
    "fast": {"scale": "bilinear", "cscale": "nearest", "dscale": "bilinear"},
    "high": {"scale": "ewa_lanczossharp", "cscale": "ewa_lanczossoft",
             "dscale": "catmull-rom"},
    "higher": {"scale": "ewa_lanczossharp", "cscale": "ewa_lanczossharp",
               "dscale": "mitchell"},
}


def config_dir() -> Path:
    return Path(os.environ.get("REELSEE_CONFIG_DIR")
                or Path.home() / ".config" / "reelsee")


def state_dir() -> Path:
    return Path(os.environ.get("REELSEE_STATE_DIR")
                or Path.home() / ".local" / "state" / "reelsee")


def config_path() -> Path:
    return config_dir() / "config.json"


# ------------------------------------------------------------------ merge --
def deep_merge(base: dict, override: dict) -> dict:
    """defaults ← user data; unknown user keys survive; base untouched.

    Nested dicts of BOTH inputs are copied (the result is fully independent of
    DEFAULTS — a mutated config must never corrupt the module-level defaults).
    """
    out = {}
    for k, v in base.items():
        out[k] = deep_merge(v, {}) if isinstance(v, dict) else v
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        elif isinstance(v, dict):
            out[k] = deep_merge({}, v)
        else:
            out[k] = v
    return out


def schema_leaf_keys(defaults: dict | None = None) -> list[str]:
    """Dotted leaf paths of the schema (settings-binding lint uses this)."""
    defaults = DEFAULTS if defaults is None else defaults
    keys: list[str] = []

    def walk(prefix: str, d: dict) -> None:
        for k, v in d.items():
            if k == "version":
                continue
            path = f"{prefix}{k}"
            if isinstance(v, dict) and v:
                walk(f"{path}.", v)
            else:
                keys.append(path)   # empty dict (e.g. mpv.options) = one widget

    walk("", defaults)
    return sorted(keys)


# -------------------------------------------------------------------- I/O --
def load_raw(path: Path) -> dict | None:  # SYNC-FS-OK
    """Read a JSON object; None if missing/corrupt/not-a-dict (never raise)."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def save_atomic(path: Path, data: dict) -> None:  # SYNC-FS-OK
    """Atomic JSON write: temp file in the same directory + os.replace (R2)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".",
                               suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class Config:
    """In-memory config with debounced atomic persistence."""

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else config_path()
        self.data = deep_merge(DEFAULTS, load_raw(self.path) or {})
        self.data.setdefault("version", VERSION)
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None
        self._dirty = False

    # -- access ------------------------------------------------------------
    def get(self, dotted: str, default=None):
        node = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def set(self, dotted: str, value, *, immediate: bool | None = None) -> None:
        parts = dotted.split(".")
        with self._lock:
            node = self.data
            for part in parts[:-1]:
                node = node.setdefault(part, {})
            node[parts[-1]] = value
            self._dirty = True
        if immediate is None:
            immediate = tuple(parts) in PERSIST_NOW_KEYS
        if immediate:
            self.flush_now()
        else:
            self._schedule_flush()

    # -- persistence ---------------------------------------------------------
    def _schedule_flush(self) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(DEBOUNCE_S, self.flush_now)
            self._timer.daemon = True
            self._timer.start()

    def flush_now(self) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            if not self._dirty:
                return
            snapshot = json.loads(json.dumps(self.data))  # deep copy, no refs
            self._dirty = False
        save_atomic(self.path, snapshot)


# ------------------------------------------------------- state stores ------
class StateStore:
    """LRU-capped JSON dict store ({key: {..., "ts": int}}) with the same
    corrupt-tolerance and atomic-write discipline as the config."""

    def __init__(self, path: Path, cap: int):
        self.path = Path(path)
        self.cap = cap
        raw = load_raw(self.path) or {}
        self._data = {k: v for k, v in raw.items() if isinstance(v, dict)}

    def get(self, key: str) -> dict | None:
        return self._data.get(key)

    def set(self, key: str, value: dict, *, persist: bool = True) -> None:
        entry = dict(value)
        entry["ts"] = int(time.time())
        self._data[key] = entry
        if len(self._data) > self.cap:  # LRU trim by ts
            keep = sorted(self._data.items(), key=lambda kv: kv[1].get("ts", 0))
            self._data = dict(keep[-self.cap:])
        if persist:
            self.persist()

    def remove(self, key: str, *, persist: bool = True) -> None:
        self._data.pop(key, None)
        if persist:
            self.persist()

    def items(self):
        return self._data.items()

    def persist(self) -> None:
        # A failing write (read-only state dir, full disk) must never break
        # the app: state stores are best-effort caches, the log gets the why.
        try:
            save_atomic(self.path, self._data)
        except OSError as exc:
            import logging
            logging.getLogger("reelsee.config").warning(
                "state store write failed (%s): %s", self.path, exc)


class ResumeStore(StateStore):
    def __init__(self, path: Path | None = None):
        super().__init__(path or state_dir() / "resume.json", cap=500)


class AbStore(StateStore):
    def __init__(self, path: Path | None = None):
        super().__init__(path or state_dir() / "ab.json", cap=100)


class EnginesStore(StateStore):
    """Capability-probe cache keyed '<pci>|<hwdec>|<codec>' (7-day TTL)."""

    TTL_S = 7 * 24 * 3600

    def __init__(self, path: Path | None = None):
        super().__init__(path or state_dir() / "engines.json", cap=512)

    def fresh(self, key: str) -> dict | None:
        entry = self._data.get(key)
        if entry and time.time() - entry.get("ts", 0) < self.TTL_S:
            return entry
        return None
