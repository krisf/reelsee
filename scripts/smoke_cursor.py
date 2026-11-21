#!/usr/bin/python3
"""R33 gate (cursor): the pointer must ACTUALLY disappear from the screen.

The bug this gate exists to kill: `_blank_cursor()` called
`Gdk.Cursor.new_from_name(self.win.get_display(), "none")`, but GDK4's
constructor is `new_from_name(name, fallback)` — the DISPLAY was passed as
the name, so every call raised TypeError and a bare `except Exception: pass`
swallowed it.  The chrome retracted and the cursor stayed on the video
forever: exactly what the user reported after 0.2.16.

A gate that only asserts "we called set_cursor" cannot catch that class, so
this one reads the X server's CURRENT CURSOR IMAGE through libXfixes and
asserts on its pixels: invisible means every pixel has zero alpha.

Legs:
  * POSITIVE CONTROL: with the chrome up, the cursor image is a real arrow
    (non-zero alpha pixels) — if this fails, nothing else here is meaningful;
  * after the 3 s idle the chrome is hidden AND `_cursor_blank` is True AND
    the X cursor image has ZERO non-zero-alpha pixels;
  * RESTORE CONTROL: a genuine pointer move (new coordinates) brings the
    chrome back AND a visible cursor image back.
"""
import ctypes
import json
import os
import sys

from smoke_harness import SCRATCH, check, finish

WIN_W, WIN_H = 640, 360


class _CursorImage(ctypes.Structure):
    _fields_ = [("x", ctypes.c_short), ("y", ctypes.c_short),
                ("width", ctypes.c_ushort), ("height", ctypes.c_ushort),
                ("xhot", ctypes.c_ushort), ("yhot", ctypes.c_ushort),
                ("cursor_serial", ctypes.c_ulong),
                ("cursor_image", ctypes.POINTER(ctypes.c_ulong)),
                ("name", ctypes.c_char_p)]


def cursor_image():
    """(w, h, opaque_pixels) of the cursor the X server is showing NOW.

    Reads it out of libXfixes via ctypes: python-xlib 0.33 ships an XFixes
    wrapper that raises BadRRCrtcError on this server, so the binding is not
    usable and the raw library is.
    """
    x11 = ctypes.CDLL("libX11.so.6")
    xf = ctypes.CDLL("libXfixes.so.3")
    x11.XOpenDisplay.restype = ctypes.c_void_p
    xf.XFixesGetCursorImage.restype = ctypes.POINTER(_CursorImage)
    dpy = x11.XOpenDisplay(None)
    if not dpy:
        return (0, 0, -1)
    ptr = xf.XFixesGetCursorImage(ctypes.c_void_p(dpy))
    ci = ptr.contents
    n = ci.width * ci.height
    opaque = sum(1 for i in range(n)
                 if (ci.cursor_image[i] >> 24) & 0xFF)
    return (ci.width, ci.height, opaque)


def warp(x, y):
    from Xlib import display
    d = display.Display()
    d.screen().root.warp_pointer(int(x), int(y))
    d.sync()
    d.close()


CFG = SCRATCH / "cursor-cfg"
CFG.mkdir(parents=True, exist_ok=True)
(CFG / "config.json").write_text(json.dumps({"ui": {"chrome_timeout_ms": 3000}}))
os.environ["REELSEE_CONFIG_DIR"] = str(CFG)
os.environ["REELSEE_STATE_DIR"] = str(SCRATCH / "cursor-state")

import reelsee  # noqa: E402  (after env, like the real entry)


def probe_schedule(app):
    from gi.repository import GLib

    st = {}

    def win():
        return app.windows[0]

    def playing() -> bool:
        snap = win().core.snapshot
        return (snap.get("pause") is False
                and snap.get("idle-active") is False
                and snap.get("eof-reached") is not True)

    def wait_playing(tries=0):
        if playing():
            return start()
        if tries >= 40:
            check("file_started_playing", False, repr(win().core.snapshot))
            app.quit()
            return False
        GLib.timeout_add(100, wait_playing, tries + 1)
        return False

    def start():
        check("file_started_playing", True, "pause=False idle=False")
        # Put the pointer over the VIDEO so the X server picks our surface's
        # cursor.  This is itself a motion edge, which is correct.
        warp(WIN_W // 2, WIN_H // 2)
        GLib.timeout_add(700, phase_shown)
        return False

    def phase_shown():
        w = win()
        st["visible"] = cursor_image()
        check("ui_up_before_idle",
              w.chrome.bottom.get_opacity() > 0.5,
              f"bottom_op={w.chrome.bottom.get_opacity():.2f}")
        check("cursor_visible_control", st["visible"][2] > 0,
              f"cursor={st['visible']}")
        # 3 s timeout + fade + margin, with the pointer parked.
        GLib.timeout_add(4500, phase_hidden)
        return False

    def phase_hidden():
        w = win()
        check("chrome_hidden_after_idle",
              w.chrome.bottom.get_opacity() < 0.05,
              f"bottom_op={w.chrome.bottom.get_opacity():.2f}")
        check("cursor_blank_flag", w.chrome._cursor_blank is True,
              f"_cursor_blank={w.chrome._cursor_blank}")
        st["blank"] = cursor_image()
        check("cursor_image_invisible", st["blank"][2] == 0,
              f"opaque_pixels={st['blank'][2]} of {st['blank'][0]}x{st['blank'][1]}")
        # RESTORE CONTROL: a real move must bring the pointer back.
        warp(WIN_W // 2 + 7, WIN_H // 2 + 5)
        GLib.timeout_add(900, phase_restored)
        return False

    def phase_restored():
        w = win()
        check("cursor_restored_flag", w.chrome._cursor_blank is False,
              f"_cursor_blank={w.chrome._cursor_blank}")
        st["after"] = cursor_image()
        check("cursor_image_visible_again", st["after"][2] > 0,
              f"opaque_pixels={st['after'][2]}")
        finish()
        app.quit()
        return False

    GLib.timeout_add(800, wait_playing)
    return False


def main() -> int:
    # 10 s clip: the timeline here is ~7 real seconds, so EOF (which
    # legitimately reveals the chrome) is far away.
    rc = reelsee.run(["reelsee", "testdata/perf_1080p30.mp4"],
                     on_app=probe_schedule)
    check("app_rc", rc == 0, str(rc))
    finish()
    return 0


if __name__ == "__main__":
    sys.exit(main())
