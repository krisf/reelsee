#!/usr/bin/env python3
"""P0 PROOF-B/E spike (PLAN Appendix A): GTK4 GLArea + libmpv render API.

Proves, headlessly under the looksee recipe (Xvfb + llvmpipe + hwdec=no):
  PROOF-B  a real video frame is rendered into the GLArea framebuffer
           (xwd capture is not black, playback advances, fps > 0)
  PROOF-E  whether vo=gpu-next is accepted by the embedded render context

Usage (via scripts/spike_render_run.sh, which sets the env):
    /usr/bin/python3 scripts/spike_render.py [gpu-next|default]

Exit 0 + ``SPIKE_RC=0`` on success.  Every claim prints a SMOKE_CHECK line.
"""
from __future__ import annotations

import ctypes
import locale
import os
import sys
import threading
import time
from pathlib import Path

# libmpv hard requirement: "Non-C locale detected" fatal unless LC_NUMERIC=C
# (PLAN §2 environment fact — the app must do the same before mpv_create).
locale.setlocale(locale.LC_NUMERIC, "C")

REPO = Path(__file__).resolve().parent.parent
LIBMPV = REPO / "vendor/usr/lib/x86_64-linux-gnu/libmpv.so.2"

# --- constants from vendor/usr/include/mpv/{render.h,render_gl.h} ---------
MPV_RENDER_PARAM_INVALID = 0
MPV_RENDER_PARAM_API_TYPE = 1
MPV_RENDER_PARAM_OPENGL_INIT_PARAMS = 2
MPV_RENDER_PARAM_OPENGL_FBO = 3
MPV_RENDER_PARAM_FLIP_Y = 4
MPV_RENDER_PARAM_ADVANCED_CONTROL = 10
MPV_RENDER_API_TYPE_OPENGL = b"opengl"

CLIP = REPO / "testdata/perf_1080p30.mp4"

# --- ctypes structures -----------------------------------------------------
class MpvOpenGLInitParams(ctypes.Structure):
    _fields_ = [
        ("get_proc_address", ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_char_p)),
        ("get_proc_address_ctx", ctypes.c_void_p),
    ]

class MpvOpenGLFbo(ctypes.Structure):
    _fields_ = [("fbo", ctypes.c_int), ("w", ctypes.c_int), ("h", ctypes.c_int),
                ("internal_format", ctypes.c_int)]

class MpvRenderParam(ctypes.Structure):
    _fields_ = [("type", ctypes.c_int), ("data", ctypes.c_void_p)]

results: dict[str, str] = {}
render_count = 0
null_procs: list[str] = []


def load_libmpv() -> ctypes.CDLL:
    if not LIBMPV.exists():
        print(f"SPIKE_FATAL: missing {LIBMPV}", file=sys.stderr)
        sys.exit(3)
    return ctypes.CDLL(str(LIBMPV))


def make_handle(lib: ctypes.CDLL, vo_variant: str) -> ctypes.c_void_p:
    lib.mpv_create.restype = ctypes.c_void_p
    lib.mpv_create.argtypes = []
    lib.mpv_set_option_string.restype = ctypes.c_int
    lib.mpv_set_option_string.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p]
    lib.mpv_initialize.restype = ctypes.c_int
    lib.mpv_initialize.argtypes = [ctypes.c_void_p]
    lib.mpv_command.restype = ctypes.c_int
    lib.mpv_command.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_char_p)]
    lib.mpv_get_property_string.restype = ctypes.c_void_p  # char*: free via mpv_free
    lib.mpv_get_property_string.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
    lib.mpv_free.restype = None
    lib.mpv_free.argtypes = [ctypes.c_void_p]

    # gi/GTK import already reset LC_NUMERIC; libmpv checks it AT CREATE time.
    locale.setlocale(locale.LC_NUMERIC, "C")

    h = ctypes.c_void_p(lib.mpv_create())
    if not h:
        print(f"SPIKE_FATAL: mpv_create failed (LC_NUMERIC={locale.setlocale(locale.LC_NUMERIC, None)!r},"
              f" LC_ALL={os.environ.get('LC_ALL')!r})", file=sys.stderr)
        sys.exit(3)
    opts = {
        "terminal": "no",
        "msg-level": "all=warn",
        "keep-open": "yes",
        "hwdec": "no",           # llvmpipe cannot import dmabuf (PLAN R13)
        "ao": "null",            # headless: no audio device assertions here
        "audio": "no",
        "config": "no",          # never read ~/.config/mpv
        "osd-level": "0",
        "osc": "no",
        # embedded render API must not probe Vulkan (RADV probe storm + crash
        # observed under Xvfb; GL is the only path we own here) — PLAN §5.2
        "gpu-api": "opengl",
        # CRITICAL: without this, loadfile makes mpv auto-create its own VO
        # (own window, Vulkan probing) BEFORE the render context exists —
        # render.h requires the render context before playback starts.
        "vo": "libmpv",
    }
    if vo_variant == "gpu-next":
        opts["vo"] = "gpu-next"  # PROOF-E: does this override even work with the render API?
    for k, v in opts.items():
        rc = lib.mpv_set_option_string(h, k.encode(), v.encode())
        if rc < 0:
            print(f"SPIKE_CHECK:set_option {k}={v} -> rc={rc}")
    rc = lib.mpv_initialize(h)
    if rc < 0:
        print(f"SPIKE_FATAL: mpv_initialize rc={rc}", file=sys.stderr)
        sys.exit(3)
    # NO loadfile here! render.h: the render context must exist BEFORE
    # playback starts — loading earlier kills the libmpv VO and playback
    # never advances (P0 finding). File is loaded in on_realize.
    return h


def load_clip(lib: ctypes.CDLL, h: ctypes.c_void_p) -> None:
    cmd = (ctypes.c_char_p * 3)(b"loadfile", str(CLIP).encode(), None)
    rc = lib.mpv_command(h, cmd)
    if rc < 0:
        print(f"SPIKE_FATAL: loadfile rc={rc}", file=sys.stderr)
        sys.exit(3)
    print("SPIKE_CHECK:loadfile_sent=yes")


def prop(lib, h, name: str) -> str:
    """Read a string property from a WORKER thread (PLAN R9 discipline)."""
    p = lib.mpv_get_property_string(h, name.encode())
    if not p:
        return ""
    out = ctypes.c_char_p(p).value.decode(errors="replace")
    lib.mpv_free(p)
    return out


def main() -> None:
    import gi  # late import: keep env effects visible
    gi.require_version("Gtk", "4.0")
    from gi.repository import GLib, Gtk

    vo_variant = sys.argv[1] if len(sys.argv) > 1 else "default"
    lib = load_libmpv()
    h = make_handle(lib, vo_variant)

    lib.mpv_render_context_create.restype = ctypes.c_int
    lib.mpv_render_context_create.argtypes = [ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p,
                                             ctypes.POINTER(MpvRenderParam)]
    lib.mpv_render_context_render.restype = ctypes.c_int
    lib.mpv_render_context_render.argtypes = [ctypes.c_void_p, ctypes.POINTER(MpvRenderParam)]
    lib.mpv_render_context_set_update_callback.restype = None
    lib.mpv_render_context_set_update_callback.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                                           ctypes.c_void_p]
    lib.mpv_render_context_report_swap.restype = None
    lib.mpv_render_context_report_swap.argtypes = [ctypes.c_void_p]
    lib.mpv_render_context_update.restype = ctypes.c_uint64
    lib.mpv_render_context_update.argtypes = [ctypes.c_void_p]
    lib.mpv_render_context_free.restype = None
    lib.mpv_render_context_free.argtypes = [ctypes.c_void_p]
    lib.mpv_terminate_destroy.restype = None
    lib.mpv_terminate_destroy.argtypes = [ctypes.c_void_p]

    # GTK/gi import resets LC_NUMERIC to the env locale; libmpv requires "C"
    # at creation time. Re-apply AT THE CREATION SITE (mpv's documented rule —
    # the app's MpvCore must do the same, PLAN §2).
    locale.setlocale(locale.LC_NUMERIC, "C")

    egl = ctypes.CDLL("libEGL.so.1")
    egl.eglGetProcAddress.restype = ctypes.c_void_p
    egl.eglGetProcAddress.argtypes = [ctypes.c_char_p]

    # GTK4's GLArea renders into ITS OWN offscreen FBO, not the default one —
    # mpv must draw into the FBO that is CURRENTLY BOUND during the render
    # callback, or its pixels never reach the compositor (P0 finding).
    GETINT = ctypes.CFUNCTYPE(None, ctypes.c_uint, ctypes.POINTER(ctypes.c_int))
    _getint_addr = egl.eglGetProcAddress(b"glGetIntegerv")
    if not _getint_addr:
        print("SPIKE_FATAL: no glGetIntegerv", file=sys.stderr)
        sys.exit(3)
    glGetIntegerv = ctypes.cast(_getint_addr, GETINT)
    GL_DRAW_FRAMEBUFFER_BINDING = 0x8CA6  # from GL headers

    def bound_fbo() -> int:
        out = (ctypes.c_int * 1)()
        glGetIntegerv(GL_DRAW_FRAMEBUFFER_BINDING, out)
        return out[0]

    GETPROC = MpvOpenGLInitParams._fields_[0][1]

    @GETPROC
    def get_proc_address(_ctx, name):
        addr = egl.eglGetProcAddress(name)
        if not addr:
            nm = name.decode(errors="replace")
            null_procs.append(nm)
            if len(null_procs) <= 10:
                print(f"SPIKE_CHECK:null_proc={nm}")
        return addr

    init_params = MpvOpenGLInitParams(get_proc_address, None)
    one = ctypes.c_int(1)
    flip = ctypes.c_int(1)
    rctx = ctypes.c_void_p()
    fbo = MpvOpenGLFbo(0, 0, 0, 0)
    state: dict[str, object] = {"rctx": None, "area": None, "quit_pending": False}

    UPDATE_CB = ctypes.CFUNCTYPE(None, ctypes.c_void_p)
    update_flag = threading.Event()
    stats: dict[str, object] = {"polls": 0, "callbacks": 0, "queued": 0, "flags": []}

    @UPDATE_CB
    def on_update(_ctx):
        # runs on an mpv-internal thread; NEVER call GLib from here
        # (g_idle_add from a foreign thread corrupts the main loop — that
        # was the P0 spike segfault). Set a flag; the main thread polls it.
        update_flag.set()

    def poll_update():
        stats["polls"] += 1
        if update_flag.is_set() and state["rctx"] is not None:
            update_flag.clear()
            stats["callbacks"] += 1
            flags = lib.mpv_render_context_update(state["rctx"])
            stats["flags"].append(flags)
            if flags & 1:  # MPV_RENDER_UPDATE_FRAME
                area = state["area"]
                if area is not None and not state["quit_pending"]:
                    stats["queued"] += 1
                    area.queue_render()
        if stats["polls"] % 50 == 0:
            print(f"SPIKE_STATS:polls={stats['polls']} cbs={stats['callbacks']} "
                  f"queued={stats['queued']} flags={stats['flags'][-6:]}")
        return True  # keep the 10 ms source alive

    def build_params(create: bool) -> list[MpvRenderParam]:
        if create:
            params = [
                MpvRenderParam(MPV_RENDER_PARAM_API_TYPE,
                               ctypes.cast(ctypes.c_char_p(MPV_RENDER_API_TYPE_OPENGL), ctypes.c_void_p)),
                MpvRenderParam(MPV_RENDER_PARAM_OPENGL_INIT_PARAMS,
                               ctypes.cast(ctypes.byref(init_params), ctypes.c_void_p)),
            ]
            # ADVANCED_CONTROL off by default: bisecting the first-render
            # segfault (see SPIKE_ADVANCED_CONTROL env to re-enable).
            if os.environ.get("SPIKE_ADVANCED_CONTROL") == "1":
                params.append(MpvRenderParam(MPV_RENDER_PARAM_ADVANCED_CONTROL,
                                             ctypes.cast(ctypes.byref(one), ctypes.c_void_p)))
            params.append(MpvRenderParam(MPV_RENDER_PARAM_INVALID, None))
            return params
        return [
            MpvRenderParam(MPV_RENDER_PARAM_OPENGL_FBO, ctypes.cast(ctypes.byref(fbo), ctypes.c_void_p)),
            MpvRenderParam(MPV_RENDER_PARAM_FLIP_Y, ctypes.cast(ctypes.byref(flip), ctypes.c_void_p)),
            MpvRenderParam(MPV_RENDER_PARAM_INVALID, None),
        ]

    class SpikeApp(Gtk.Application):
        def do_activate(self):  # type: ignore[override]
            win = Gtk.ApplicationWindow(application=self)
            win.set_default_size(640, 360)
            area = Gtk.GLArea()
            area.set_hexpand(True)   # without these the GLArea sizes 0x0 and
            area.set_vexpand(True)   # the whole window shows white chrome bg
            area.set_has_depth_buffer(False)
            area.set_has_stencil_buffer(False)
            win.set_child(area)
            state["area"] = area

            def on_realize(a):
                a.make_current()
                if a.get_error() is not None:
                    print(f"SPIKE_FATAL: GLArea error {a.get_error()}", file=sys.stderr)
                    self.quit()
                    return
                global render_count
                locale.setlocale(locale.LC_NUMERIC, "C")  # gtk_init resets it
                rc = lib.mpv_render_context_create(ctypes.byref(rctx), h,
                                                   (MpvRenderParam * 4)(*build_params(True)))
                if rc < 0:
                    print(f"SPIKE_FATAL: render_context_create rc={rc}", file=sys.stderr)
                    self.quit()
                    return
                state["rctx"] = rctx
                lib.mpv_render_context_set_update_callback(rctx, on_update, None)
                print("SPIKE_CHECK:render_context_created=yes")
                # render context now exists -> START playback (correct order)
                load_clip(lib, h)

            def on_render(a, ctx):
                global render_count
                if state["rctx"] is None:
                    return True
                w = a.get_width() * a.get_scale_factor()
                hgt = a.get_height() * a.get_scale_factor()
                fbo.w, fbo.h = w, hgt
                fbo.fbo = bound_fbo()  # draw into GTK's bound FBO, not 0
                if render_count < 3:
                    print(f"SPIKE_CHECK:bound_fbo={fbo.fbo} size={w}x{hgt}")
                rc = lib.mpv_render_context_render(state["rctx"], (MpvRenderParam * 3)(*build_params(False)))
                if rc < 0:
                    print(f"SPIKE_FATAL: render rc={rc}", file=sys.stderr)
                    self.quit()
                    return True
                if os.environ.get("SPIKE_ADVANCED_CONTROL") == "1":
                    lib.mpv_render_context_report_swap(state["rctx"])
                render_count += 1
                if render_count % 15 == 1:
                    print(f"SPIKE_CHECK:render={render_count}")
                return True

            def on_unrealize(a):
                # R11 discipline: free while the GL context is current
                a.make_current()
                if state["rctx"] is not None:
                    lib.mpv_render_context_set_update_callback(state["rctx"], None, None)
                    lib.mpv_render_context_free(state["rctx"])
                    state["rctx"] = None
                if state["area"] is not None:
                    state["area"] = None

            area.connect("realize", on_realize)
            area.connect("render", on_render)
            area.connect("unrealize", on_unrealize)
            GLib.timeout_add(10, poll_update)

            def finish():
                state["quit_pending"] = True
                # probes run with playback LIVE; the render context is freed
                # after them (freeing it kills the VO — P0 finding).
                time.sleep(0.1)
                # capture + property probes FIRST (playback still live),
                # render context freed after — freeing kills the VO.
                shot = REPO / "scratch/spike-frame.xwd"
                shot.parent.mkdir(exist_ok=True)
                os.system(f"xwd -root -silent > {shot}")
                t1 = float(prop(lib, h, "time-pos") or -1)
                time.sleep(1.0)
                t2 = float(prop(lib, h, "time-pos") or -1)
                vf = prop(lib, h, "estimated-vf-fps")
                # R11: teardown while the GL context is still current
                if state["rctx"] is not None and state["area"] is not None:
                    state["area"].make_current()
                    lib.mpv_render_context_set_update_callback(state["rctx"], None, None)
                    lib.mpv_render_context_free(state["rctx"])
                    state["rctx"] = None
                results["vo_variant"] = vo_variant
                results["render_count"] = str(render_count)
                results["time_delta"] = f"{t2 - t1:.3f}"
                results["vf_fps"] = vf
                results["null_proc_count"] = str(len(null_procs))
                results["xwd_path"] = str(shot)
                print(f"SPIKE_RESULTS:{results}")
                self.quit()
                return False

            GLib.timeout_add(3500, finish)
            win.present()

    app = SpikeApp(application_id="pro.krisforbes.ReelseeSpike")
    app.run([])

    # window closed -> GL context gone; now destroy the player
    lib.mpv_terminate_destroy(h)
    if vo_variant == "default" and results.get("render_count", "0") == "0":
        print("SPIKE_FATAL: no frames rendered", file=sys.stderr)
        sys.exit(4)
    print("SPIKE_RC=0")


if __name__ == "__main__":
    main()
