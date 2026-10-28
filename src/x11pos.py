"""x11pos — the ONE Xlib seam: exact window placement on the X11 backend.

Why raw Xlib at all (operator-proven, references/ROUND10-BRIEF.md):
  * GTK 4 has NO window-move API — Gtk.Window exposes nothing for
    position and GdkX11's surface adds only get_xid() (measured on this
    build: 4.22.4, no get_origin on the surface either);
  * Xlib on top of the SAME surface (surface.get_xid() →
    create_resource_object("window", xid) → configure(x, y, w, h))
    positions a MAPPED toplevel EXACTLY ((0,0)→(420,260) measured, and
    re-verified by scripts/smoke_wingeom.py);
  * Wayland cannot position a toplevel at all — xdg-shell has no
    position request, the compositor decides.  There is no workaround.

So this module is deliberately a *degradation-first* seam: every entry
point returns None/False and logs ONE debug line when the platform has
no answer (no surface yet, no xid, non-X11 surface, Xlib missing, X
server unreachable, dead connection).  It must never raise, never block
the main loop, and never spam the log — the open path in window.py keeps
working size-only no matter what happens here.

The python-xlib Display connection is opened lazily and shared; window
coordinates are walked up the parent chain to the ROOT window, so a
window-manager reparent (frame window) reports the position the user
actually sees, not a frame-relative local origin.
"""
from __future__ import annotations

import logging
import os
import warnings

log = logging.getLogger("reelsee.x11pos")

_state: dict = {
    "display": None,          # shared python-xlib Display (or False = dead)
    "display_failed": False,
    "xid_probe": None,        # (module, surface-class-name) cache — debug only
}
_logged = set()               # one debug line per (kind) — never a per-tick log


def _debug_once(kind: str, msg: str, *args) -> None:
    if kind not in _logged:
        _logged.add(kind)
        log.debug(msg, *args)


def reset_logging_for_tests() -> None:
    """Smokes assert 'one debug log, no exception' on the Wayland path;
    they clear the once-latch first so the assertion is about THIS run."""
    _logged.clear()


# ------------------------------------------------------------------ surfaces
def surface_xid(win) -> int | None:
    """The X11 window id of a MAPPED toplevel, or None.

    None means "no X11 answer right now": not mapped yet, or a Wayland
    surface (get_xid simply does not exist there), or GDK without the
    GdkX11 introspection typelib.  Never raises.
    """
    try:
        surf = win.get_surface()
    except Exception:                               # noqa: BLE001
        return None
    if surf is None:
        return None
    get_xid = getattr(surf, "get_xid", None)
    if get_xid is None:                             # Wayland surfaces lack it
        _debug_once("no-xid", "surface %s has no xid: positioning is "
                    "X11-only; size-only from here (compositor owns "
                    "position)", type(surf).__name__)
        return None
    try:
        with warnings.catch_warnings():             # get_xid is deprecated
            warnings.simplefilter("ignore")
            xid = get_xid()
    except Exception:                               # noqa: BLE001
        _debug_once("xid-fail", "get_xid failed: %r", True)
        return None
    if not xid:
        return None
    return int(xid)


def is_x11(win) -> bool:
    return surface_xid(win) is not None


# ------------------------------------------------------------------- display
def _display():
    """Shared python-xlib connection, or None.  One failed attempt is
    remembered: no per-tick reconnect storm on a headless/Wayland box."""
    st = _state
    if st["display"] is False or (st["display"] is None
                                  and st["display_failed"]):
        return None
    if st["display"] is not None:
        return st["display"]
    try:
        from Xlib import display as xdisplay        # python3-xlib, system pkg
        disp = xdisplay.Display(os.environ.get("DISPLAY") or None)
    except Exception as exc:                        # noqa: BLE001
        st["display_failed"] = True
        _debug_once("xlib", "no Xlib display (%s): positioning degraded to "
                    "size-only", type(exc).__name__)
        return None
    st["display"] = disp
    return disp


def _root(disp):
    return disp.screen().root


def _window(win):
    """(display, xlib window object) for a mapped X11 toplevel, or None."""
    xid = surface_xid(win)
    if xid is None:
        return None
    disp = _display()
    if disp is None:
        return None
    try:
        return disp, disp.create_resource_object("window", xid)
    except Exception:                               # noqa: BLE001
        return None


# ------------------------------------------------------------------ geometry
def get_rect(win) -> tuple[int, int, int, int] | None:
    """The window's LIVE root-relative rect (x, y, w, h), or None.

    get_geometry() is parent-relative, so on a WM-managed (reparented)
    window the offsets are summed up the parent chain to the root — the
    position the user sees.  Under a WM-less Xvfb the walk is a single
    step and already root-relative.
    """
    got = _window(win)
    if got is None:
        return None
    disp, xw = got
    try:
        root_id = _root(disp).id
        node = xw
        x = y = 0
        for _ in range(16):                        # frames nest; never loop
            g = node.get_geometry()
            x += g.x
            y += g.y
            tree = node.query_tree()
            if tree.parent.id == root_id or tree.parent.id == node.id:
                return (x, y, g.width, g.height)
            node = tree.parent
        return (x, y, xw.get_geometry().width, xw.get_geometry().height)
    except Exception as exc:                        # noqa: BLE001
        _drop_display()
        _debug_once("get-rect", "Xlib geometry failed: %s", type(exc).__name__)
        return None


def set_rect(win, rect) -> bool:
    """configure(x=, y=, width=, height=) on the live X11 surface.

    True = the request went through.  False = no-op degradation (no xid,
    no Xlib, dead server) — the caller keeps the size-only path and the
    user sees nothing but a window the compositor placed.  NEVER raises.
    """
    from wingeom import norm_rect
    r = norm_rect(rect)
    if r is None:
        return False
    got = _window(win)
    if got is None:
        return False
    disp, xw = got
    x, y, w, h = r
    try:
        xw.configure(x=x, y=y, width=w, height=h)
        disp.sync()
        return True
    except Exception as exc:                        # noqa: BLE001
        _drop_display()
        _debug_once("set-rect", "Xlib configure failed: %s",
                    type(exc).__name__)
        return False


def _drop_display() -> None:
    """A failed protocol round-trip poisons the connection: close, forget,
    let the next call try once more with a fresh one."""
    d = _state.get("display")
    _state["display"] = None
    _state["display_failed"] = False
    _state.pop("hints_atom", None)                  # atoms are per-display
    try:
        if d is not None and d is not False:
            d.close()
    except Exception:                               # noqa: BLE001
        pass


# ------------------------------------------------------------- size hints
# R14-B: the ONLY way to make a resize obey an aspect on X11 is to SAY SO
# in WM_NORMAL_HINTS (the PAspect bit + min/max aspect words) — GTK 4 has
# no API, and post-hoc correction from Gdk.Surface::layout is a fight the
# compositor always wins mid-drag.  The property is the ICCCM WM_SIZE_HINTS
# array of 18 CARD32 words:
#   [0]flags [1]x [2]y [3]w [4]h [5..8]min/max w,h [9,10]resize inc
#   [11,12]min aspect num,den [13,14]max aspect num,den [15,16]base w,h
#   [17]win_gravity.   PAspect is bit 5 of the flags.
# Everything is raw protocol constants (no Xlib imports at module scope —
# the module must stay importable with no Xlib and no display).
_HINTS_WORDS = 18
_SH_FLAGS = 0
_SH_MIN_ASPECT = 11
_SH_MAX_ASPECT = 13
_PASPECT = 1 << 5
_PROP_MODE_REPLACE = 0


def _read_hints(xw, disp) -> list[int] | None:
    """Live WM_NORMAL_HINTS as 18 ints, or None (absent / wrong type —
    ICCCM says old-typed hints are simply ignored)."""
    try:
        prop = xw.get_full_property(disp.intern_atom("WM_NORMAL_HINTS"),
                                    disp.intern_atom("WM_SIZE_HINTS"))
    except Exception:                               # noqa: BLE001
        _drop_display()
        return None
    if prop is None or prop.format != 32 or prop.value is None:
        return None
    arr = [int(v) & 0xFFFFFFFF for v in prop.value]
    if len(arr) < _HINTS_WORDS:                     # ancient 14-word form
        arr += [0] * (_HINTS_WORDS - len(arr))
    return arr[:_HINTS_WORDS]


def _write_hints(xw, disp, arr) -> bool:
    try:
        xw.change_property(disp.intern_atom("WM_NORMAL_HINTS"),
                           disp.intern_atom("WM_SIZE_HINTS"), 32,
                           list(arr), _PROP_MODE_REPLACE)
        disp.sync()                                 # property BEFORE any
        return True                                 # later map/read-back
    except Exception as exc:                        # noqa: BLE001
        _drop_display()
        _debug_once("hints-write", "Xlib size hints failed: %s",
                    type(exc).__name__)
        return False


def set_aspect_ratio(win, num, den) -> bool:
    """Publish PAspect ``num:den`` in WM_NORMAL_HINTS — MERGE-WRITE.

    The window's other fields (GDK's own min-size/base-size/gravity words
    live in the SAME property) are read back first and written untouched;
    only flags |= PAspect and the four aspect words change.  min-aspect ==
    max-aspect pins the ratio exactly.  True = the property is now on the
    server (sync() proves it); False = degraded (no xid/Xlib/dead server)
    and the caller keeps the §R8-B post-hoc correction path.  Never raises.
    """
    try:
        num, den = int(num), int(den)
    except (TypeError, ValueError):
        return False
    if num <= 0 or den <= 0:
        return False
    got = _window(win)
    if got is None:
        return False
    disp, xw = got
    arr = _read_hints(xw, disp)
    if arr is None:
        arr = [0] * _HINTS_WORDS
    if (arr[_SH_FLAGS] & _PASPECT
            and arr[_SH_MIN_ASPECT] == num and arr[_SH_MIN_ASPECT + 1] == den
            and arr[_SH_MAX_ASPECT] == num and arr[_SH_MAX_ASPECT + 1] == den):
        return True                                 # already exactly right
    arr[_SH_FLAGS] |= _PASPECT
    arr[_SH_MIN_ASPECT], arr[_SH_MIN_ASPECT + 1] = num, den
    arr[_SH_MAX_ASPECT], arr[_SH_MAX_ASPECT + 1] = num, den
    return _write_hints(xw, disp, arr)


def clear_aspect_ratio(win) -> bool:
    """Drop PAspect so the window resizes freely again (lock off, crop on,
    no video, fullscreen).  A missing property, or one without the bit, is
    ALREADY clear: return True without touching the server (no property
    churn on every state ping).  Merge-write, so GDK's other fields live
    on untouched."""
    got = _window(win)
    if got is None:
        return False
    disp, xw = got
    arr = _read_hints(xw, disp)
    if arr is None or not (arr[_SH_FLAGS] & _PASPECT):
        return True
    arr[_SH_FLAGS] &= ~_PASPECT & 0xFFFFFFFF
    return _write_hints(xw, disp, arr)


def get_aspect_ratio(win) -> tuple[int, int] | None:
    """The PAspect pair currently on the SERVER, or None.  Read-back, not
    a cache — the smoke gate asserts against what the X server actually
    holds.  Only for X11/debug; the app path never needs it."""
    got = _window(win)
    if got is None:
        return None
    disp, xw = got
    arr = _read_hints(xw, disp)
    if arr is None or not (arr[_SH_FLAGS] & _PASPECT):
        return None
    return (arr[_SH_MIN_ASPECT], arr[_SH_MIN_ASPECT + 1])


# ------------------------------------------------------------ clobber watch
# MEASURED (R14, scripts/smoke_aspecthints.py phase storm_*): GDK's X11
# backend rewrites WM_NORMAL_HINTS WHOLESALE — its own flags/min-size, no
# PAspect — on every drawn frame while a video plays (two PropertyNotify
# events per frame at 30 fps), and while paused it writes nothing at all.
# A one-shot publish therefore survives pauses and idling but is erased
# within one frame of playback, so the window keeps a PropertyChange
# subscription on its own X window (event selection is PER CLIENT: this
# costs GDK nothing) and re-declares PAspect whenever someone else wins
# the property.  Repair is a property write only — never a resize — so it
# cannot join the geometry storm §R8-B defuses.


def watch_hints(win) -> int | None:
    """Select PropertyChangeMask on our window; return the shared
    connection's fd to watch (GLib.IO_IN), or None = no X11 / no Xlib /
    dead connection (caller simply never watches)."""
    got = _window(win)
    if got is None:
        return None
    disp, xw = got
    try:
        from Xlib import X
        xw.change_attributes(event_mask=X.PropertyChangeMask)
        disp.sync()
        return disp.fileno()
    except Exception as exc:                        # noqa: BLE001
        _drop_display()
        _debug_once("hints-watch", "Xlib event mask failed: %s",
                    type(exc).__name__)
        return None


def unwatch_hints(win) -> None:
    """Drop the subscription (best effort — the connection may already be
    gone, and a dead watch on a closed fd is exactly the bug here)."""
    got = _window(win)
    if got is None:
        return
    disp, xw = got
    try:
        xw.change_attributes(event_mask=0)
        disp.sync()
    except Exception:                               # noqa: BLE001
        _drop_display()


def hint_notices(win) -> int:
    """Drain queued X events on the shared connection; return how many
    were PropertyNotify on WM_NORMAL_HINTS.  Includes the echo of OUR
    own writes — that is fine and makes this idempotent: the caller
    re-APPLIES (set_aspect_ratio early-returns when the words already
    match, writing nothing), so our echo provokes no further write and
    the loop closes itself after one round."""
    got = _window(win)
    if got is None:
        return 0
    disp, xw = got
    try:
        from Xlib import X
        atom = _state.get("hints_atom")
        if atom is None:
            atom = _state["hints_atom"] = disp.intern_atom("WM_NORMAL_HINTS")
        n = 0
        while disp.pending_events():
            ev = disp.next_event()
            if getattr(ev, "type", None) == X.PropertyNotify \
                    and getattr(ev, "atom", None) == atom:
                n += 1
        return n
    except Exception as exc:                        # noqa: BLE001
        _drop_display()
        _debug_once("hints-pump", "Xlib event drain failed: %s",
                    type(exc).__name__)
        return 0
