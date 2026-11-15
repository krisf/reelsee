#!/usr/bin/python3
"""R28 diagnosis launcher: run the real app with the geometry/hints seam
fully instrumented, so one :0 launch answers WHO resizes the window and
WHAT the WM_NORMAL_HINTS array says at that exact moment.

Everything is monkey-patching from OUTSIDE src/ — the shipped code stays
clean; the log (env AR_DIAG_LOG, default /tmp/ar-diag.log) records:

  HINT-WRITE   array before (server) / wanted / after (server sync'd),
               live rect at the write, and the calling frame;
  APPLY        every _aspect_hints_apply entry: lock state + live rect;
  NOTICE       every property-watch wake-up (GDK clobber echo) with the
               server array and rect as seen at entry;
  LAYOUT       every Gdk.Surface::layout: new w/h vs live rect;
  APP-RESIZE   every Gtk.Window.set_default_size / x11pos.set_rect with
               the caller frames — a shrink with NO app-resize line is a
               WM clamp, that is the whole discriminator;
  TICK         a 250 ms independent Xlib probe of the live rect + hints
               flags (observes WM action even when no app code runs).

usage: same argv as reelsee.py (the clip).  Used via AR_LAUNCH=... in
scripts/aspect_ratchet.py.
"""
import os
import runpy
import sys
import time
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO / "src") not in sys.path:
    sys.path.insert(0, os.environ.get("AR_SRC", str(REPO / "src")))

LOG = open(os.environ.get("AR_DIAG_LOG", "/tmp/ar-diag.log"), "a", buffering=1)
T0 = time.monotonic()


def log(tag, msg):
    LOG.write(f"{time.monotonic() - T0:7.3f} {tag} {msg}\n")


def callers(depth=6):
    keep = [f for f in traceback.format_stack()
            if "/src/" in f and "x11pos.py" not in f]
    return " << ".join(ln.strip() for ln in "".join(keep[-2:]).splitlines()
                       if "->" in ln or "in " in ln)[:300]


import gi  # noqa: E402  (harness pins GDK_BACKEND=x11 already)

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, GLib  # noqa: E402

import x11pos  # noqa: E402


def _rect_of(win):
    try:
        return x11pos.get_rect(win)
    except Exception:
        return None


# ------------------------------------------------------------------ hints
_w_write = x11pos._write_hints


def _write_hints(xw, disp, arr):
    before = None
    try:
        before = x11pos._read_hints(xw, disp)
    except Exception:
        pass
    ok = _w_write(xw, disp, arr)
    try:
        after = x11pos._read_hints(xw, disp)
    except Exception:
        after = "ERR"
    log("HINT-WRITE", f"ok={ok} before={before} want={list(arr)} after={after}"
                      f" callers={callers()}")
    return ok


x11pos._write_hints = _write_hints

for _name in ("set_aspect_ratio", "clear_aspect_ratio"):
    def _mk(name):
        orig = getattr(x11pos, name)

        def patched(win, *a):
            log("HINT-CALL", f"{name}{a} rect={_rect_of(win)} "
                             f"callers={callers()}")
            r = orig(win, *a)
            return r
        return patched
    setattr(x11pos, _name, _mk(_name))

# ------------------------------------------------------------------ app resizes
_sds = Gtk.Window.set_default_size


def _set_default_size(win, w, h):
    log("APP-RESIZE", f"set_default_size({w},{h}) rect={_rect_of(win)} "
                      f"callers={callers()}")
    return _sds(win, w, h)


Gtk.Window.set_default_size = _set_default_size

_srect = x11pos.set_rect


def _set_rect(win, rect):
    log("APP-RESIZE", f"set_rect({rect}) rect={_rect_of(win)} "
                      f"callers={callers()}")
    return _srect(win, rect)


x11pos.set_rect = _set_rect

# ------------------------------------------------------------------ window seams
import window  # noqa: E402

RW = window.ReelseeWindow

_apply = RW._aspect_hints_apply


def _aspect_hints_apply(self):
    try:
        lock = self._aspect_locked()
    except Exception:
        lock = "?"
    log("APPLY", f"lock={lock} rect={_rect_of(self)} callers={callers(4)}")
    return _apply(self)


RW._aspect_hints_apply = _aspect_hints_apply

_io = RW._on_hints_io


def _on_hints_io(self, *a):
    n = 0
    try:
        n = len(x11pos.hint_notices()) if hasattr(x11pos, "hint_notices") else -1
    except Exception:
        pass
    log("NOTICE", f"pending={n} rect={_rect_of(self)}")
    return _io(self, *a)


RW._on_hints_io = _on_hints_io

_lay = RW._on_surface_layout


def _on_surface_layout(self, *a):
    log("LAYOUT", f"args={a[1:]} rect={_rect_of(self)}")
    return _lay(self, *a)


RW._on_surface_layout = _on_surface_layout

_asz = RW._apply_aspect_size


def _apply_aspect_size(self, size):
    try:
        mn_w = self.measure(Gtk.Orientation.HORIZONTAL, 0)[0]
        mn_h = self.measure(Gtk.Orientation.VERTICAL, 0)[0]
    except Exception:
        mn_w = mn_h = -1
    log("CORRECT", f"_apply_aspect_size({size}) logical=({self.get_width()},"
                   f"{self.get_height()}) gtk_min=({mn_w},{mn_h}) "
                   f"callers={callers(4)}")
    return _asz(self, size)


RW._apply_aspect_size = _apply_aspect_size

_sstart = RW._geom_settle_start


def _geom_settle_start(self, rect):
    log("SETTLE", f"start target={rect} live={_rect_of(self)}")
    return _sstart(self, rect)


RW._geom_settle_start = _geom_settle_start

_sfin = RW._geom_settle_finish


def _geom_settle_finish(self, reason, live):
    st = getattr(self, "_geom_settle", None) or {}
    log("SETTLE", f"finish reason={reason} live={live} target={st.get('target')} "
                  f"moves={st.get('moves')}")
    return _sfin(self, reason, live)


RW._geom_settle_finish = _geom_settle_finish

_pers = RW._geom_persist


def _geom_persist(self):
    r = _pers(self)
    log("PERSIST", f"live={_rect_of(self)} -> {r}")
    return r


RW._geom_persist = _geom_persist

_cap = RW.capture_geometry


def capture_geometry(self):
    r = _cap(self)
    log("PERSIST", f"capture_geometry -> {r} live={_rect_of(self)}")
    return r


RW.capture_geometry = capture_geometry

# ------------------------------------------------------------------ independent probe
_state = {}

_i = RW.__init__


def _init(self, *a, **k):
    r = _i(self, *a, **k)
    _state["win"] = self
    return r


RW.__init__ = _init


def _tick():
    win = _state.get("win")
    if win is None:
        return True
    try:
        arr = None
        got = x11pos._window(win)
        if got is not None:
            arr = x11pos._read_hints(got[1], got[0])
            _tap_configure(got[1].id)
        snap = {}
        try:
            sn = win.core.snapshot
            snap = {k: sn.get(k) for k in ("dwidth", "dheight", "pause")}
        except Exception:
            pass
        try:
            mn = (win.measure(Gtk.Orientation.HORIZONTAL, 0)[0],
                  win.measure(Gtk.Orientation.VERTICAL, 0)[0])
        except Exception:
            mn = "?"
        log("TICK", f"rect={_rect_of(win)} flags={arr[0] if arr else None} "
                    f"aspect={arr[11:15] if arr else None} "
                    f"min={arr[5:7] if arr else None} max={arr[7:9] if arr else None} "
                    f"logical=({win.get_width()},{win.get_height()}) gtk_min={mn} "
                    f"core={snap}")
    except Exception as exc:
        log("TICK", f"ERR {exc!r}")
    return True


# ---------------------------------------------------------------- configure tap
# A SECOND, independent Xlib connection selects StructureNotify on our own
# xid: every ConfigureNotify the client receives (its own requests, GTK's
# internal resizes, and WM-originated clamps) is logged.  A configure that
# matches no APP-RESIZE line is somebody ELSE resizing us.
_tap = {}


def _tap_configure(xid):
    if _tap.get("xid") == xid:
        return
    try:
        from Xlib import X as XlX
        from Xlib.display import Display
        import Xlib.error
        d = Display()
        xw = d.create_resource_object("window", xid)
        xw.change_attributes(event_mask=XlX.StructureNotifyMask)
        d.sync()
        _tap.clear()
        _tap.update(xid=xid, disp=d, xw=xw)
        GLib.io_add_watch(d, GLib.IO_IN | GLib.IO_ERR, _tap_io)
        log("TAP", f"StructureNotify on xid={xid}")
    except Exception as exc:
        log("TAP", f"ERR {exc!r} (xid={xid})")


def _tap_io(source, condition):
    d = _tap.get("disp")
    if d is None:
        return False
    try:
        n = 0
        # NEVER call next_event() with an empty queue: it BLOCKS the GTK
        # main loop (this blocked the R28 run-4 loop and ate a :0 run).
        while d.pending_events() and n < 50:
            ev = d.next_event()
            n += 1
            et = getattr(ev, "type", None)
            if et in ("ConfigureNotify", "ConfigureRequest"):
                log("CONFIG", f"{ev.type} {getattr(ev, 'x', '?')},"
                              f"{getattr(ev, 'y', '?')},{getattr(ev, 'width', '?')},"
                              f"{getattr(ev, 'height', '?')} send={getattr(ev, 'send_event', '?')}")
            elif et == "DestroyNotify":
                log("CONFIG", "DestroyNotify")
    except Exception as exc:
        log("CONFIG", f"tap ERR {exc!r}")
    return True


GLib.timeout_add(250, _tick)

_lp = RW._load_path


def _load_path(self, path, *a, **k):
    log("LOAD", f"_load_path({path!r}) callers={callers(4)}")
    return _lp(self, path, *a, **k)


RW._load_path = _load_path

_fl = RW._on_file_loaded


def _on_file_loaded(self, *a):
    log("LOAD", f"file_loaded args={a} core={getattr(self.core, 'snapshot', {})}")
    return _fl(self, *a)


RW._on_file_loaded = _on_file_loaded

log("BOOT", "instrumented")
log("BOOT", f"argv={sys.argv}")
try:
    runpy.run_path(str(REPO / "src" / "reelsee.py"), run_name="__main__")
except SystemExit:
    raise
finally:
    log("BOOT", "exit")
    LOG.close()
