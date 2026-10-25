"""RenderView — GLArea + libmpv render context (verbatim spike mechanism).

Proven facts this encodes (PLAN §0 / scripts/spike_render.py):
- vo=libmpv + gpu-api=opengl, render context BEFORE any loadfile;
- render into the FBO GTK currently binds (GL_DRAW_FRAMEBUFFER_BINDING)
  with FLIP_Y=1 — GTK4's GLArea renders to its own offscreen FBO;
- the update callback runs on an mpv thread: it only sets a threading.Event
  (GLib calls from there corrupted the main loop — the P0 segfault); a
  10 ms main-thread poll calls mpv_render_context_update() and re-queues
  the draw iff flags & MPV_RENDER_UPDATE_FRAME;
- LC_NUMERIC=C re-set at both mpv_create and render-context create;
- teardown order (R11): probes first, then detach callback +
  mpv_render_context_free() with the GL context current, terminate later.
"""
from __future__ import annotations

import ctypes
import locale
import logging
import threading

import gi

import mpvlib as M

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402

log = logging.getLogger("reelsee.render")

GETPROC = M.MpvOpenGLInitParams._fields_[0][1]
GETINT = ctypes.CFUNCTYPE(None, ctypes.c_uint, ctypes.POINTER(ctypes.c_int))


class RenderView:
    """Owns the GLArea and the mpv render context of ONE window."""

    def __init__(self, lib, core, on_ready=None):
        self.lib = lib
        self.core = core
        self.on_ready = on_ready          # main-thread cb once rctx exists
        self.rctx: ctypes.c_void_p | None = None
        self.teared_down = False
        self.render_count = 0
        self.update_flag = threading.Event()
        self._egl = ctypes.CDLL("libEGL.so.1")
        self._egl.eglGetProcAddress.restype = ctypes.c_void_p
        self._egl.eglGetProcAddress.argtypes = [ctypes.c_char_p]

        def get_proc_address(_ctx, name):
            addr = self._egl.eglGetProcAddress(name)
            if not addr:
                log.warning("GL proc unresolved: %s",
                            name.decode(errors="replace") if name else name)
            return addr

        self._get_proc = GETPROC(get_proc_address)          # keep referenced
        self._init_params = M.MpvOpenGLInitParams(self._get_proc, None)
        self._flip = ctypes.c_int(1)
        self._fbo = M.MpvOpenGLFbo(0, 0, 0, 0)

        addr = self._egl.eglGetProcAddress(b"glGetIntegerv")
        if not addr:
            raise RuntimeError("eglGetProcAddress(glGetIntegerv) failed")
        self._gl_get_integerv = ctypes.cast(addr, GETINT)

        @M.UPDATE_CALLBACK
        def _on_update(_ctx):
            # mpv-internal thread — set the flag, NOTHING else (§0.3).
            self.update_flag.set()

        self._update_cb = _on_update

        self.area = Gtk.GLArea()
        self.area.set_hexpand(True)   # else 0x0 white window (§0.4)
        self.area.set_vexpand(True)
        self.area.set_has_depth_buffer(False)
        self.area.set_has_stencil_buffer(False)
        self.area.connect("realize", self._on_realize)
        self.area.connect("render", self._on_render)
        self.area.connect("unrealize", self._on_unrealize)

    # ------------------------------------------------------------- lifecycle
    def _on_realize(self, area) -> None:
        area.make_current()
        if area.get_error() is not None:
            log.error("GLArea failed: %s", area.get_error())
            return
        locale.setlocale(locale.LC_NUMERIC, "C")  # GTK init reset it (§0.1)
        params = (M.MpvRenderParam * 3)(
            M.MpvRenderParam(M.MPV_RENDER_PARAM_API_TYPE,
                             ctypes.cast(ctypes.c_char_p(M.MPV_RENDER_API_TYPE_OPENGL),
                                         ctypes.c_void_p)),
            M.MpvRenderParam(M.MPV_RENDER_PARAM_OPENGL_INIT_PARAMS,
                             ctypes.cast(ctypes.byref(self._init_params),
                                         ctypes.c_void_p)),
            M.MpvRenderParam(M.MPV_RENDER_PARAM_INVALID, None),
        )
        rctx = ctypes.c_void_p()
        rc = self.lib.mpv_render_context_create(ctypes.byref(rctx), self.core.h,
                                                params)
        if rc < 0:
            log.error("mpv_render_context_create: %s", M.error_string(self.lib, rc))
            return
        self.rctx = rctx
        self.lib.mpv_render_context_set_update_callback(rctx, self._update_cb, None)
        if self.on_ready:
            self.on_ready(self)   # NOW loadfile may run — rctx exists first

    def _bound_fbo(self) -> int:
        out = (ctypes.c_int * 1)()
        self._gl_get_integerv(M.GL_DRAW_FRAMEBUFFER_BINDING, out)
        return out[0]

    def _on_render(self, area, _ctx) -> bool:
        if self.rctx is None:
            return True
        self._fbo.w = area.get_width() * area.get_scale_factor()
        self._fbo.h = area.get_height() * area.get_scale_factor()
        self._fbo.fbo = self._bound_fbo()          # GTK's FBO, not 0 (§0.4)
        params = (M.MpvRenderParam * 3)(
            M.MpvRenderParam(M.MPV_RENDER_PARAM_OPENGL_FBO,
                             ctypes.cast(ctypes.byref(self._fbo), ctypes.c_void_p)),
            M.MpvRenderParam(M.MPV_RENDER_PARAM_FLIP_Y,
                             ctypes.cast(ctypes.byref(self._flip), ctypes.c_void_p)),
            M.MpvRenderParam(M.MPV_RENDER_PARAM_INVALID, None),
        )
        rc = self.lib.mpv_render_context_render(self.rctx, params)
        if rc < 0:
            log.debug("render_context_render: %s", M.error_string(self.lib, rc))
        self.render_count += 1
        return True

    def _on_unrealize(self, area) -> None:
        self.teardown()

    def teardown(self) -> None:
        """Detach + free with the GL context current (§0.6 order, R11).
        Idempotent; safe from window close handlers on the main thread."""
        if self.teared_down:
            return
        self.teared_down = True
        if self.rctx is not None:
            self.core._data_event.clear()
            self.lib.mpv_render_context_set_update_callback(self.rctx, None, None)
            if self.area.get_realized():
                self.area.make_current()
            self.lib.mpv_render_context_free(self.rctx)
            self.rctx = None

    # ------------------------------------------------------------- main poll
    def poll(self) -> None:
        """10 ms main-thread pump (§0.3): update callback only set a flag."""
        if self.rctx is None or not self.update_flag.is_set():
            return
        self.update_flag.clear()
        flags = self.lib.mpv_render_context_update(self.rctx)
        if flags & M.MPV_RENDER_UPDATE_FRAME and not self.teared_down:
            self.area.queue_render()
