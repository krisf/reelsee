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
server unreachable, dead connection) — EXCEPT the no-xid notice, which is
INFO since round 24 because it is the one degradation the operator has to
be able to see.  It must never raise, never block the main loop, and never
spam the log — the open path in window.py keeps working size-only no
matter what happens here.

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
_logged = set()               # one line per (kind) — never a per-tick log


def _debug_once(kind: str, msg: str, *args) -> None:
    if kind not in _logged:
        _logged.add(kind)
        log.debug(msg, *args)


def _info_once(kind: str, msg: str, *args) -> None:
    """Like _debug_once, for the ONE degradation the operator needs to see
    without turning debug on (R24: the no-xid notice was the only thing in
    the log explaining why a remembered position was ignored, and it was
    invisible at the default level)."""
    if kind not in _logged:
        _logged.add(kind)
        log.info(msg, *args)


def reset_logging_for_tests() -> None:
    """Smokes assert 'one log line, no exception' on the Wayland path; they
    clear the once-latch first so the assertion is about THIS run."""
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
        # R24: INFO, not DEBUG.  This is the line that explains to the
        # operator why a remembered position was ignored, and it was
        # invisible at the default level — which is exactly how "it still
        # does not resume position" survived a release.  Still ONE line per
        # process (the once-latch), so a 1 s sampler cannot spam the log.
        _info_once("no-xid", "surface %s has no xid: positioning is "
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
#   [17]win_gravity.
# R28 — the round this whole seam was DEAD on a real desktop: the flag bit
# table below had PAspect at `1 << 5`.  ICCCM §4.1.2.4 (and <X11/Xutil.h>):
# USPosition 1, USSize 2, PPosition 4, PSize 8, PMinSize 16, PMaxSize 32,
# PResizeInc 64, PAspect 128, PBaseSize 256, PWinGravity 512.  `1 << 5` is
# PMaxSize; PAspect is 1 << 7.  Measured on the operator's Mutter: the old
# bit advertised PMaxSize over max words that are 0,0 — a WM reads "never
# larger than 0x0" and clamps the window to its content minimum (102x102
# window.json = the user's "missing window"), while clear_aspect_ratio
# stripped GDK's own PMaxSize flag.  The table is now spelled OUT word by
# word and audited against Xutil.h; the arithmetic lives in the PURE
# hints_merge_aspect/hints_clear_aspect functions below (unit-gated with no
# display by tests/test_x11pos_hints.py), and NOTHING publishes without
# (b) dropping a PMaxSize whose max words are 0,0 and (c) skipping a write
# whose array would be byte-identical to the server's.
# Everything is raw protocol constants (no Xlib imports at module scope —
# the module must stay importable with no Xlib and no display).
_HINTS_WORDS = 18
_SH_FLAGS = 0
_SH_X, _SH_Y, _SH_W, _SH_H = 1, 2, 3, 4
_SH_MIN_W, _SH_MIN_H = 5, 6
_SH_MAX_W, _SH_MAX_H = 7, 8
_SH_WINC, _SH_HINC = 9, 10
_SH_MIN_ASPECT = 11
_SH_MAX_ASPECT = 13
_SH_BASE_W, _SH_BASE_H = 15, 16
_SH_GRAVITY = 17
# ICCCM §4.1.2.4 / <X11/Xutil.h> WM_SIZE_HINTS flag bits — R28-corrected.
_US_POSITION = 1 << 0        #   1
_US_SIZE = 1 << 1            #   2
_PPOSITION = 1 << 2          #   4
_PSIZE = 1 << 3              #   8
_PMIN_SIZE = 1 << 4          #  16
_PMAX_SIZE = 1 << 5          #  32  <- NOT PAspect.  This mix-up is R28.
_PRESIZE_INC = 1 << 6        #  64
_PASPECT = 1 << 7            # 128  <- the aspect-ratio flag (R28 fix)
_PBASE_SIZE = 1 << 8         # 256
_PWIN_GRAVITY = 1 << 9       # 512
_PROP_MODE_REPLACE = 0


# --------------------------------------------------- pure hints arithmetic
# R28 §4: every decision about the array's WORDS is a pure function here —
# no X calls, no display — so tests/test_x11pos_hints.py can audit the
# bit arithmetic headless, including the negative control (the pre-R28
# constants fail that gate by construction).


def _hints_pad(arr) -> list[int] | None:
    """Anything shaped like the hints array → exactly 18 masked ints, or
    None for garbage (non-numeric words).  Short arrays are the ancient
    14-word form, zero-padded like _read_hints does; longer input is a
    sign of a broken caller and is refused."""
    try:
        vals = [int(v) & 0xFFFFFFFF for v in arr]
    except (TypeError, ValueError):
        return None
    if len(vals) > _HINTS_WORDS:
        return None
    if len(vals) < _HINTS_WORDS:
        vals += [0] * (_HINTS_WORDS - len(vals))
    return vals


def _hints_drop_dead_max(arr: list[int]) -> bool:
    """In-place R28 §3-B: a PMaxSize whose max_width AND max_height are 0
    is self-defeating — ICCCM treats an unset flag as "no maximum", while
    a SET flag with zero words means "never larger than 0x0", and a real
    WM answers that by clamping the window to its content minimum.  Drop
    the FLAG (the words are inert once the flag is gone, so foreign word
    bytes stay untouched).  Returns True when it changed anything.
    PMaxSize with any nonzero max word is a legitimate client maximum and
    is NEVER touched here."""
    if (arr[_SH_FLAGS] & _PMAX_SIZE
            and arr[_SH_MAX_W] == 0 and arr[_SH_MAX_H] == 0):
        arr[_SH_FLAGS] &= ~_PMAX_SIZE & 0xFFFFFFFF
        return True
    return False


def hints_merge_aspect(arr, num, den) -> list[int] | None:
    """Pure half of set_aspect_ratio: `arr` (the server's current words)
    with PAspect ``num:den`` pinned (min-aspect == max-aspect) — a NEW
    list, or None when the server ALREADY holds exactly that, i.e. the
    caller must write NOTHING (R28 §3-C: no property announcement, no WM
    round-trip, per drawn frame).  Foreign words (GDK's min/base/inc/
    gravity) survive byte-exactly; a dead PMaxSize-0,0 flag is dropped
    (§3-B).  Garbage input (non-numeric, > 18 words, bad ratio) → None."""
    try:
        num, den = int(num), int(den)
    except (TypeError, ValueError):
        return None
    if num <= 0 or den <= 0:
        return None
    out = _hints_pad(arr)
    if out is None:
        return None
    before = list(out)
    out[_SH_FLAGS] |= _PASPECT
    out[_SH_MIN_ASPECT], out[_SH_MIN_ASPECT + 1] = num, den
    out[_SH_MAX_ASPECT], out[_SH_MAX_ASPECT + 1] = num, den
    _hints_drop_dead_max(out)
    return out if out != before else None


def hints_clear_aspect(arr) -> list[int] | None:
    """Pure half of clear_aspect_ratio: clear the PAspect BIT ONLY —
    never PMaxSize (R28 §3-A: the old constant cleared PMaxSize and had
    been stripping GDK's own maximum), never any other foreign word; the
    aspect words themselves are inert without the flag and stay put — or
    None when the server already has the bit clear, i.e. write nothing.
    The one exception is the §3-B dead-max drop: a PMaxSize-0,0 left by
    the PRE-R28 code still poisons the window, so clearing the lock must
    heal it too.  Garbage input → None."""
    out = _hints_pad(arr)
    if out is None:
        return None
    before = list(out)
    out[_SH_FLAGS] &= ~_PASPECT & 0xFFFFFFFF
    _hints_drop_dead_max(out)
    return out if out != before else None


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
    live in the SAME property) are read back first and handed to the pure
    `hints_merge_aspect`, which returns None when the server ALREADY
    holds exactly the array we want — then NOTHING is written (R28 §3-C:
    the clobber watch answers GDK's per-frame PropertyNotify by re-
    applying, and every identical re-write was another property
    announcement, another WM round-trip, and another re-constrain of the
    live window on a real WM).  A write that DOES happen has already had
    any dead PMaxSize-0,0 flag dropped (§3-B), so a self-defeating array
    can never leave this function.  min-aspect == max-aspect pins the
    ratio exactly.  True = the property is now on the server (sync()
    proves it, or proves it was already right); False = degraded (no
    xid/Xlib/dead server) and the caller keeps the §R8-B post-hoc
    correction path.  Never raises."""
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
    merged = hints_merge_aspect(arr, num, den)
    if merged is None:
        return True          # the server already holds exactly this array
    return _write_hints(xw, disp, merged)


def clear_aspect_ratio(win) -> bool:
    """Drop PAspect so the window resizes freely again (lock off, crop on,
    no video, fullscreen).  A missing property, or one without the bit, is
    ALREADY clear: return True without touching the server (no property
    churn on every state ping).  Merge-write, so GDK's other fields live
    on untouched — and "untouched" now means the RIGHT bit: the pre-R28
    constant cleared PMaxSize (32) instead of PAspect (128), i.e. this
    call had been silently stripping GDK's own maximum-size flag since
    the seam shipped.  A dead PMaxSize-0,0 (left by that old code) is the
    one thing healed here, per §3-B."""
    got = _window(win)
    if got is None:
        return False
    disp, xw = got
    arr = _read_hints(xw, disp)
    if arr is None:
        return True                     # no property: already clear
    cleared = hints_clear_aspect(arr)
    if cleared is None:
        return True                     # bit already clear: write nothing
    return _write_hints(xw, disp, cleared)


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
