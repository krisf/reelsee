"""R6-A: the video right-click context menu — the model half (pure Gio).

ONE model, ONE truth: this module turns the live mpv snapshot into a
``Gio.Menu``.  It never touches mpv, GTK widgets or config.  src/window.py
registers the ``ctx.*`` actions the model references and routes them to
the SAME entry points the keymap and the playlist/chapters/tracks popovers
already use (identical ``core.command("set", ...)`` calls, identical
repeat cycle via ``chrome.repeat_props``) — so the context menu can never
disagree with the rest of the player.

R8-A: with the in-video top menu bar DELETED outright, this menu is the
home of every feature the bar had — it is the only menu, always, for
every config.

Rules implemented here (round 6 brief §A):
  * a disabled filename header is the first section;
  * body order: Audio, Subtitles, Repeat, Crop to window, Playlist,
    Chapters, Tracks (R11 inserted the checkable crop toggle);
  * an entry whose data is missing in the snapshot stays PRESENT but
    insensitive — modelled as an item bound to the always-disabled
    ``ctx.noop`` action (GTK renders action-bound items insensitive when
    the action is disabled; an action-less item would be too);
  * Repeat is a radio submenu bound to the stateful ``ctx.repeat`` action
    — the checked entry comes from the action state, which window.py
    keeps in sync with the mpv ``loop-file``/``loop-playlist`` truth.
"""
from __future__ import annotations

import os

from gi.repository import Gio, GLib

APP = "ctx"                 # action group name registered by the window
NOOP = APP + ".noop"        # always-disabled action: renders items out


def track_label(t: dict) -> str:
    """ONE label formatting shared by the tracks popover rows (§7 T1)
    and the context menu — a single formatting source for both menus."""
    bits = [f"[{t.get('type') or '?'}]",
            t.get("title") or t.get("codec") or "",
            f"[{t.get('lang')}]" if t.get("lang") else "",
            f"#{t.get('id')}"]
    return ("\u25cf " if t.get("selected") else "\u25cb ") + \
        " ".join(b for b in bits if b)


def _disabled(section: Gio.Menu, label: str) -> None:
    section.append_item(Gio.MenuItem.new(label[:90], NOOP))


def _add(menu: Gio.Menu, label: str, action: str, target=None) -> None:
    """append with a TYPED target (Gio's detailed-action parser would
    otherwise hand the action a string instead of the int32/s variant)."""
    item = Gio.MenuItem.new(label, action)
    if target is not None:
        item.set_attribute_value("target", target)
    menu.append_item(item)


def _submenu_item(section: Gio.Menu, label: str, sub: Gio.Menu) -> None:
    item = Gio.MenuItem.new(label[:90], None)
    item.set_submenu(sub)
    section.append_item(item)


def _tracks(snap) -> list:
    tl = snap.get("track-list")
    return [t for t in tl if isinstance(t, dict)] \
        if isinstance(tl, list) else []


def _kind_tracks(tracks, kind):
    """EVERY selectable track of a kind, selected included — the
    selected row renders disabled (it is the current choice), exactly
    like the tracks popover; a kind that EXISTS yields a live submenu
    (with an Off entry), only a MISSING kind is the disabled entry."""
    return [t for t in tracks
            if t.get("type") == kind and not t.get("image")]


def build(snap) -> Gio.Menu:
    """Snapshot in, Gio.Menu out (fresh model on every call — the window
    swaps it into the popover before each popup)."""
    snap = snap if isinstance(snap, dict) else {}
    name = snap.get("media-title") or snap.get("filename") or "REELSEE"
    label = os.path.basename(str(name))

    menu = Gio.Menu()
    head = Gio.Menu()
    _disabled(head, label)                 # filename header: never live
    menu.append_section(None, head)

    body = Gio.Menu()
    tracks = _tracks(snap)

    # ---- Audio / Subtitles ----------------------------------------------
    for label, kind, prop, off in (("Audio", "audio", "pick-audio",
                                    "audio-off"),
                                   ("Subtitles", "sub", "pick-sub",
                                    "sub-off")):
        rows = _kind_tracks(tracks, kind)
        if not rows:
            _disabled(body, label)
            continue
        sub = Gio.Menu()
        for t in rows:
            tid = t.get("id")
            if t.get("selected") or not isinstance(tid, int):
                _disabled(sub, track_label(t)[2:])   # current choice
            else:
                _add(sub, track_label(t)[2:], f"{APP}.{prop}",
                     GLib.Variant("i", tid))
        _add(sub, f"\u25cb no {label.lower()}", f"{APP}.{off}")
        _submenu_item(body, label, sub)

    # ---- Repeat (always live; radio state = mpv loop-file/loop-playlist)
    sub = Gio.Menu()
    for mode in ("off", "all", "one"):
        _add(sub, mode.capitalize(), f"{APP}.repeat",
             GLib.Variant("s", mode))
    _submenu_item(body, "Repeat", sub)

    # ---- Crop to window (R11: stateful checkable toggle directly after
    # Repeat).  The CHECKMARK is rendered by Gtk from the ctx.crop-to-window
    # BOOLEAN ACTION STATE — the model only points at the action, because a
    # fresh model per popup must never bake a stale checkmark into a label.
    # Always live: it needs no file data, exactly like Repeat.
    _add(body, "Crop to window", f"{APP}.crop-to-window")

    # ---- Playlist -------------------------------------------------------
    pl = snap.get("playlist")
    pl = [r for r in pl if isinstance(r, dict)] if isinstance(pl, list) \
        else []
    if pl:
        sub = Gio.Menu()
        for i, r in enumerate(pl):
            mark = "\u25b6" if r.get("current") else " "
            name = (r.get("title") or r.get("filename") or "?")
            _add(sub, f"{mark} {i:3d}  {str(name)[:80]}",
                 f"{APP}.goto-playlist", GLib.Variant("i", i))
        _submenu_item(body, "Playlist", sub)
    else:
        _disabled(body, "Playlist")

    # ---- Chapters -------------------------------------------------------
    chs = snap.get("chapter-list")
    chs = [c for c in chs if isinstance(c, dict)] \
        if isinstance(chs, list) else []
    if chs:
        sub = Gio.Menu()
        for i, ch in enumerate(chs):
            title = ch.get("title") or f"Chapter {i + 1}"
            _add(sub, f"{i + 1:3d}  {str(title)[:80]}",
                 f"{APP}.goto-chapter", GLib.Variant("i", i))
        _submenu_item(body, "Chapters", sub)
    else:
        _disabled(body, "Chapters")

    # ---- Tracks (the full tracks-popover mirror) ------------------------
    if tracks:
        sub = Gio.Menu()
        for t in tracks:
            kind = t.get("type") or "?"
            prop = {"video": "pick-video", "audio": "pick-audio",
                    "sub": "pick-sub"}.get(kind)
            tid = t.get("id")
            if prop and isinstance(tid, int) and not t.get("selected"):
                _add(sub, track_label(t), f"{APP}.{prop}",
                     GLib.Variant("i", tid))
            else:
                _disabled(sub, track_label(t))
        _add(sub, "\u25cb no audio", f"{APP}.audio-off")
        _add(sub, "\u25cb no subtitles", f"{APP}.sub-off")
        _submenu_item(body, "Tracks", sub)
    else:
        _disabled(body, "Tracks")

    menu.append_section(None, body)
    return menu


def _item(menu, i):
    lv = menu.get_item_attribute_value(i, "label", None)
    av = menu.get_item_attribute_value(i, "action", None)
    tv = menu.get_item_attribute_value(i, "target", None)
    label = lv.unpack() if lv is not None else None
    act = av.unpack() if av is not None else None
    target = tv.unpack() if tv is not None else None
    sub = menu.get_item_link(i, "submenu")
    return (label, act, target, None if sub is None else model_dump(sub))


def model_dump(menu):
    """[(label, action_or_None, target_or_None, submenu_dump_or_None)] in
    paint order (sections flattened) — the SAME walk unit tests and
    smokes assert against (Gio.Menu introspection only; works with no
    display).  A disabled entry is exactly one whose action is
    ``ctx.noop``."""
    out = []
    for i in range(menu.get_n_items()):
        sec = menu.get_item_link(i, "section")
        if sec is not None:                 # top-level: sections of items
            for j in range(sec.get_n_items()):
                out.append(_item(sec, j))
        else:                               # submenu: flat items
            out.append(_item(menu, i))
    return out
