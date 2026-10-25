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
    try:
        if d is not None and d is not False:
            d.close()
    except Exception:                               # noqa: BLE001
        pass
