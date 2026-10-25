"""Key bindings (PLAN §4.7, §5.4).  Pure module — parseable + unit-testable
without GTK; window.py supplies Gdk event facts (keyval name, state flags).

Canonical event string: modifiers lowercase joined with '+' plus the
lowercased Gdk keyval name, e.g. "shift+left", "f11", "bracketleft", "space".
Single-character keys keep uppercase as their Gdk name ("N" etc.); the window
therefore also tries the lowercase event string, which is how the plain-char
entries in DEFAULT_BINDINGS below resolve (and why "l" vs "shift+l" cannot be
distorted into two bindings).
"""
from __future__ import annotations

MODIFIER_ORDER = ("ctrl", "alt", "super", "shift")

#: canonical event string → action id (int/str args resolved by window.py)
DEFAULT_BINDINGS: dict[str, str] = {
    "space": "play-pause",
    "p": "play-pause",
    "left": "seek-small",
    "right": "seek-small",
    "shift+left": "seek-long",
    "shift+right": "seek-long",
    "up": "volume-up",
    "down": "volume-down",
    "9": "volume-down",
    "0": "volume-up",
    "m": "toggle-mute",
    "f": "fullscreen",
    "f11": "fullscreen",
    "escape": "exit",
    "q": "exit",
    "backslash": "fullscreen-exit",
    "s": "screenshot",
    "a": "cycle-audio",
    "v": "cycle-sub",
    "t": "cycle-sub-visible",
    "bracketleft": "set-a",
    "bracketright": "set-b",
    "shift+b": "clear-ab",
    "backspace": "clear-ab",
    "comma": "frame-back-step",
    "period": "frame-step",
    "l": "loop-file",
    "ctrl+comma": "settings",
    "braceleft": "speed-down",
    "braceright": "speed-up",
    "shift+comma": "speed-down",
    "shift+period": "speed-up",
    # §7 T1 (P11): playlist / chapters / loop-playlist / stop / quit.
    # NOTE: single-char events canonicalise UPPERCASE ("m" press → "M"),
    # so window.py falls back to ev.lower(); distinct shift+letter bindings
    # are therefore impossible — playlist-prev is "b", loop-playlist "r".
    "n": "playlist-next",
    "b": "playlist-prev",
    "r": "loop-playlist",
    "page_up": "chapter-prev",
    "page_down": "chapter-next",
    "prior": "chapter-prev",
    "next": "chapter-next",
    "ctrl+s": "stop",
    "ctrl+q": "quit",
}

_MOD_ALIASES = {"ctrl": "ctrl", "control": "ctrl", "shift": "shift",
                "alt": "alt", "meta": "alt", "super": "super",
                "hyper": "super"}


def parse_binding(raw: str) -> str | None:
    """'Shift+Left' → 'shift+left'; None for empty/garbage."""
    raw = (raw or "").strip()
    if not raw:
        return None
    parts = [p for p in raw.split("+") if p]
    if not parts:
        return None
    key = parts[-1]
    mods = []
    for p in parts[:-1]:
        m = _MOD_ALIASES.get(p.lower())
        if m and m not in mods:
            mods.append(m)
    if len(key) == 1:
        key = key.upper()
    else:
        key = key.lower()
    mods = [m for m in MODIFIER_ORDER if m in mods]
    return "+".join(mods + [key])


def event_string(keyval_name: str | None, shift: bool, ctrl: bool, alt: bool,
                 super_: bool = False) -> str | None:
    """Normalize a GDK key press into the canonical event string."""
    if not keyval_name:
        return None
    key = keyval_name.lower()
    # Gdk gives ISO keysyms like "minus"/"plus" — we prefer printable char
    if len(keyval_name) == 1:
        key = keyval_name.upper() if keyval_name.isalpha() else keyval_name
    mods = []
    if ctrl:
        mods.append("ctrl")
    if alt:
        mods.append("alt")
    if super_:
        mods.append("super")
    if shift:
        mods.append("shift")
    mods = [m for m in MODIFIER_ORDER if m in mods]
    return "+".join(mods + [key])


def load_input_conf(path, base: dict[str, str | tuple] | None = None  # SYNC-FS-OK
                    ) -> dict[str, str | tuple]:
    """Merge an mpv-style input.conf.  Lines: 'KEY action'.  Values that are
    REELSEE action ids stay strings; anything else becomes
    ("mpv-command", [argv…]) executed via core.command(*argv) (no blocklist
    here — input.conf is an explicit trusted file, the palette is the
    guarded path, R12)."""
    out: dict[str, str | tuple] = dict(base if base is not None
                                       else DEFAULT_BINDINGS)
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                key, _, rest = line.partition(" ")
                rest = rest.strip()
                if not rest:
                    continue
                canon = parse_binding(key)
                if not canon:
                    continue
                if rest in DEFAULT_BINDINGS.values():
                    out[canon] = rest
                else:
                    out[canon] = ("mpv-command", rest.split())
    except OSError:
        pass
    return out
