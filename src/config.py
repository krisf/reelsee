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

import wingeom

VERSION = 1

DEFAULTS: dict = {
    "version": VERSION,
    # R15-B: one-time migration ledger — run_migrations() adds the flag
    # to a RAW config file when it first boots a stored file.  True in
    # DEFAULTS by design: a FRESH install (no file on disk) has nothing
    # to migrate, and a Config merged from DEFAULTS must read as
    # "already migrated".  Not a settings leaf — schema_leaf_keys skips
    # this section exactly like "version".
    "migrations": {"repeat_default_on": True},
    "audio": {"muted": True, "volume": 100.0, "focus_mute": True,
              "exclusive": False, "delay_s": 0.0, "device": "auto"},
    "engine": {
        "decoder": "auto",           # auto | cpu | <pci>[:vulkan]
        "hwdec_pref": "vaapi",       # vaapi | vulkan
        "zero_copy": False,          # R2-P1: opt into zero-copy vaapi (may
                                     # fall back — see engine.py mapping)
        "renderer_override": "auto", # auto | DRI_PRIME index (string)
        "cpu_threads": 0,            # 0 = auto
        # R16-D: how many streams the decode GPU holds at once.  Default 2;
        # refine it on the real desktop with scripts/bench_multidecode.py
        # (margin rule: highest N where every stream holds >= 24 fps with no
        # rising drop trend).  Windows beyond it decode on the CPU.
        "decode_capacity": 2,
        "watchdog_enabled": True,
        "watchdog_fps_floor_pct": 90,
        "watchdog_drop_ceiling_pct": 3,
        "retry_on_new_file": True,
        "last_fallback": None,       # {from,to,ts,fps_ratio,drop_ratio} (Appendix C)
    },
    # R15-B: repeat ships ON ("inf" = loop this file forever); the
    # one-time run_migrations() migrates pre-0.x stored "no" values.
    "playback": {"speed": 1.0, "loop_file": "inf", "loop_playlist": "no",
                 "autoplay": True, "resume": True, "resume_min_s": 300,
                 "preload_next": False, "keep_open": True,
                 "pitch": True, "video_off": False, "cover_art": "albumart",
                 # R16-B: a minimized (unmapped) window stops DECODING video
                 # (`vid=no`) while nobody can see it — audio keeps playing,
                 # position keeps advancing, nothing is ever auto-paused.
                 # Off = the pre-R16 behaviour (keep decoding invisibly).
                 "suppress_video_when_hidden": True,
                 # R16-E §E.1/§E.2 (ROUND18 corrected): which render node
                 # VAAPI decode uses.  "" = AUTO: the INTEGRATED
                 # (non-discrete) card — the iGPU, display-bearing or not;
                 # connectors are not part of the mandate — or CPU when no
                 # card qualifies; automatic selection may never name a
                 # discrete dGPU.  Any other value PINS that node explicitly
                 # (escape hatch: it may name a dGPU, because a human chose
                 # it).
                 "vaapi_device": "",
                 "ytdlp": True, "ytdlp_timeout": 5.0},
    "ab": {"count": "inf"},
    "ui": {"chrome_timeout_ms": 3000, "osd_ms": 1200, "seek_step_s": 5,
           "seek_step_long_s": 30, "theme": "black", "ontop": False,
           "title_format": "${media-title} — REELSEE",
           "screenshot_clipboard": False,
           # R8-B: a drag keeps the video's shape (observed dwidth/dheight).
           # GTK 4 has no geometry hints left, so the window CORRECTS its
           # size on Gdk.Surface::layout — math in src/aspect.py.
           "lock_aspect": True,
           # R10: remember the last WINDOWED rect (<state>/window.json, NOT
           # this file) and cascade new windows off the open one.  The
           # remembered GEOMETRY is state; these two are the user's policy.
           "remember_geometry": True, "cascade_offset_px": 50},
    "subtitles": {"visible": True, "scale": 1.0, "delay_s": 0.0,
                  "auto": "exact", "font": "sans-serif", "font_size": 55.0,
                  "color": "#ffffff", "border_size": 3.0, "margin_y": 50.0},
    "video": {"aspect": "no", "zoom": 0.0, "pan_x": 0.0, "pan_y": 0.0,
              "rotate": 0.0, "crop": "no", "brightness": 0.0, "contrast": 0.0,
              # R11: "Crop to window" — fill the window, clip the overflow
              # (cover semantics).  Persisted like mute/repeat (R9 lesson:
              # the ONE writer persists AND applies the live property).
              "crop_to_window": False,
              "saturation": 0.0, "gamma": 0.0, "hue": 0.0,
              "deinterlace": "auto", "scale_preset": "default",
              "deband": False, "dither_depth": "auto", "interpolation": False,
              "tscale": "catmull"},
    "screenshot": {"directory": "~/Pictures", "format": "png", "jpeg_quality": 90},
    "mpv": {"options": {}},           # T2 raw option overrides, applied at init
}

# Keys whose user changes must hit the disk immediately, not debounced (R-4).
# R9-A: the two repeat keys join mute/volume — the bar is "survives a
# restart, LIKE MUTE DOES": with a 250 ms debounce and no shutdown flush,
# a toggle followed by a quick quit would still be lost.
PERSIST_NOW_KEYS = {("audio", "muted"), ("audio", "volume"),
                    ("playback", "loop_file"),
                    ("playback", "loop_playlist"),
                    ("video", "crop_to_window")}

DEBOUNCE_S = 0.25

# ------------------------------------------------------- live property map --
# §7 T1: every config leaf that maps 1:1 onto a *runtime-settable* mpv
# property.  Value kinds: "bool" | "boolnum" (bool → 1.0/0.0) | "float" |
# "str" | "strno" (empty/None → "no").  Keys NOT here (engine.*, ui.chrome_timeout_ms, ab.count,
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
    # R11: crop-to-window (cover/fill) rides mpv's pan-scan: panscan=1.0
    # with keepaspect=yes (the default) zooms to FULL fill and clips the
    # overflow — exactly the brief's cover semantics, and MEASURED (round
    # 11 probe) to have a visible effect through the embedded vo=libmpv
    # render API.  boolnum because panscan is numeric, not a flag.
    "video.crop_to_window": ("panscan", "boolnum"),
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
        if kind == "boolnum":
            return 1.0 if value else 0.0
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
            if k in ("version", "migrations"):
                # internal bookkeeping sections, never settings leaves
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


def run_migrations(path: Path | None = None) -> None:  # SYNC-FS-OK
    """R15-B: one-time config migrations, run at boot BEFORE Config().

    Repeat shipped with the default loop_file "no" until round 15 made
    it ON ("inf") — but a STORED value always beats the default, so
    every existing install would keep repeating "off" forever.  This
    migration runs exactly once per on-disk config: a raw file without
    the `migrations.repeat_default_on` flag gets that flag, and a
    literal stored playback.loop_file of "no" flips to "inf" — nothing
    else in the file is touched (the RAW dict is edited, not the merged
    one, and written back with save_atomic: temp + rename).

    Never re-fires: once the flag is there the function returns without
    reading further, so a user who later turns repeat OFF writes a new
    "no" BEHIND the flag and it survives every restart.  No file yet
    (fresh install — DEFAULTS already say "inf") or an unreadable/corrupt
    file: nothing to migrate, nothing written.  A non-literal value
    ("3", "all", absent) is left exactly as found.
    """
    path = Path(path) if path else config_path()
    raw = load_raw(path)                       # None: missing OR corrupt
    if raw is None:
        return
    mig = raw.get("migrations")
    if isinstance(mig, dict) and mig.get("repeat_default_on") is True:
        return                                 # migrated once — never again
    if not isinstance(mig, dict):
        mig = {}
    pb = raw.get("playback")
    if isinstance(pb, dict) and pb.get("loop_file") == "no":
        pb["loop_file"] = "inf"                # the ONE literal we flip
    mig["repeat_default_on"] = True
    raw["migrations"] = mig
    save_atomic(path, raw)


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


class GeometryStore:
    """R10-A: the last WINDOWED window rect — runtime STATE, not config.

    Lives in `<state>/window.json` (next to resume.json), deliberately not
    in the user config: it is where the last window happened to be, a
    machine fact nobody edits.  Discipline, all of it pinned by
    tests/test_wingeom.py:

      * missing / corrupt / nonsense file ⇒ last() is None and NOTHING
        raises — the app opens with the default behaviour (§A.3);
      * only windowed rects are remembered: remember(..., windowed=False)
        (closing while fullscreen or maximized) keeps the previously
        remembered rect byte-for-byte (§A.2);
      * writes are debounced (DEBOUNCE_S, like the config) and flush()
        fires on close/quit even when the debounce never got its turn —
        "save on quit" is a promise, not a race (§A.1).
    """

    def __init__(self, path: Path | None = None,
                 debounce_s: float = DEBOUNCE_S):
        self.path = Path(path) if path else state_dir() / "window.json"
        self.debounce_s = debounce_s
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None
        self._dirty = False
        raw = load_raw(self.path)
        rect = wingeom.valid_rect(raw.get("rect") if raw else None)
        self._rect: tuple | None = rect      # the remembered windowed rect
        self._monitor = wingeom.valid_monitor(
            raw.get("monitor") if raw else None)

    # -- read ----------------------------------------------------------------
    def last(self) -> tuple[int, int, int, int] | None:
        """The remembered rect (x, y, w, h), or None ("never saved / corrupt")."""
        with self._lock:
            return self._rect

    def last_monitor(self) -> int | None:
        """Monitor index the rect was remembered on, or None (unknown)."""
        with self._lock:
            return self._monitor

    # -- write ---------------------------------------------------------------
    def remember(self, rect, monitor: int | None = None, *,
                 windowed: bool = True, persist: bool = True) -> bool:
        """Update the remembered rect and schedule the debounced write.

        windowed=False is a NO-OP on the value (§A.2: a fullscreen close
        must leave the remembered size intact) — it still reports False so
        the caller can see that nothing changed.  An invalid rect is
        dropped silently: garbage from a buggy caller is treated exactly
        like a corrupt file.
        """
        if not windowed:
            return False
        r = wingeom.valid_rect(rect)
        if r is None:
            return False
        mon = wingeom.valid_monitor(monitor)
        with self._lock:
            changed = (r != self._rect or mon != self._monitor)
            self._rect, self._monitor = r, mon
            if not changed and not self._dirty:
                return True                  # same value on disk already
            self._dirty = True
            if not persist:
                return True
            self._schedule_flush()
        return True

    def _schedule_flush(self) -> None:
        """Debounced write; caller must hold _lock (Config's pattern)."""
        if self._timer is not None:
            self._timer.cancel()
        self._timer = threading.Timer(self.debounce_s, self.flush)
        self._timer.daemon = True
        self._timer.start()

    def flush(self) -> None:
        """Persist NOW — the close/quit guarantee: even if the debounce
        has not fired, the last windowed rect lands on disk."""
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            if not self._dirty:
                return
            payload = {"version": VERSION, "rect": None, "monitor": None}
            if self._rect is not None:
                x, y, w, h = self._rect
                payload["rect"] = {"x": x, "y": y, "w": w, "h": h}
                payload["monitor"] = self._monitor
            self._dirty = False
        try:
            save_atomic(self.path, payload)  # SYNC-FS-OK (atomic, R2)
        except OSError as exc:               # best-effort, like StateStore
            import logging
            logging.getLogger("reelsee.config").warning(
                "window geometry write failed (%s): %s", self.path, exc)
