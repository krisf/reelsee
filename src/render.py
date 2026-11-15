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

R23 — the fullscreen / maximize cycle.  Fullscreen and maximize-restore
reparent the window, which unrealizes and re-realizes the GLArea.  Three
separate defects lived on that path (all measured under the headless
harness): (1) `teardown()`'s `teared_down` latch is one-shot, so the SECOND
unrealize freed nothing and the leaked render context made the next
`mpv_render_context_create` fail — the user's crash on the second
double-click; (2) the free could run with no GL context current (§0.6
breaks when `get_realized()` is already False); (3) even with a clean
free/create, mpv's libmpv VO never resumes picture delivery, so the surface
goes black.  Unrealize now frees via `_free_rctx()` (context-current,
defer-if-impossible, idempotent), `teardown()` is the final close path only,
and a re-creation is followed by the §0.7 track reattach.
"""
from __future__ import annotations

import ctypes
import locale
import logging
import threading
import time

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
        # R23: a render context whose free could not be done under a current
        # GL context is parked here and freed at the next realize (§0.6).
        self._pending_free: ctypes.c_void_p | None = None
        # R23: True once a render context has been created for this view;
        # distinguishes a first create from a fullscreen/maximize re-create.
        self._had_rctx = False
        self.render_count = 0
        # R16-A: real-time stamp of the LAST MPV_RENDER_UPDATE_FRAME this
        # pump drained — the cadence ladder's only evidence that a video
        # track is producing pictures (frame_age_ms).  Written on the main
        # thread only; the mpv update callback still sets a flag and
        # NOTHING else (§0.3).
        self._last_frame_wall: float | None = None
        # R27-A: (w, h, scale, fbo) of the last logged probe + its stamp.
        # The probe logs ONE DEBUG line per CHANGE of the draw-target size
        # (rate-limited), to confirm or kill the "FBO 0x0 during fullscreen"
        # hypothesis with data instead of guessing.
        self._fbo_probe_last: tuple | None = None
        self._fbo_probe_t = 0.0
        self._pump_probe_t = 0.0
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
        # R27-A: lifecycle line (see _on_unrealize) — the draw counter's
        # value across a cycle is the audit trail of the pump.
        log.debug("realize: render_count=%d had_rctx=%s",
                  self.render_count, self._had_rctx)
        if self.teared_down:
            # R23: the window is on its way out (final teardown already ran);
            # a late realize must not re-create a context against a handle
            # that is being terminated.
            log.debug("realize after final teardown ignored")
            return
        # R23: is this the first context of this view, or a re-creation after
        # a fullscreen / maximize unrealize?  mpv's libmpv VO does NOT resume
        # picture delivery after a free + create cycle (measured:
        # mpv_render_context_update() returns 0 forever from the second
        # create on), so a re-creation needs the §0.7 track reattach.
        recreate = self._had_rctx
        area.make_current()
        if area.get_error() is not None:
            log.error("GLArea failed: %s", area.get_error())
            return
        # R23: a fullscreen / maximize transition reparents the window, so
        # this realize may arrive with the PREVIOUS render context still
        # alive (its unrealize free was deferred for want of a current GL
        # context).  mpv refuses a second render context on the same handle
        # — that is the "mpv_render_context_create: something happened" the
        # user saw on the second toggle.  Free-then-create, both under the
        # now-current context (§0.6 order).
        self._free_rctx()
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
        log.debug("rctx created handle=%s", rctx.value)
        self.rctx = rctx
        self._had_rctx = True
        self.lib.mpv_render_context_set_update_callback(rctx, self._update_cb, None)
        if self.on_ready:
            self.on_ready(self)   # NOW loadfile may run — rctx exists first
        if recreate:
            # R23: the second and every later create leaves mpv's VO idle
            # (flags=0 forever) unless the video track is re-attached.
            self._reattach_video()

    def _reattach_video(self) -> None:
        """R23: re-init mpv's video output after a render-context cycle.

        Measured on this libmpv: `mpv_render_context_free` +
        `mpv_render_context_create` on the same handle succeeds but the VO
        never delivers another frame (`mpv_render_context_update` returns 0
        forever, the pump stops queueing draws, the capture goes black).
        The proven re-init sequence is the track reattach of §0.7 /
        AGENTS.md — `set vid no` then `set vid auto` — the same one
        `MpvCore.apply_hwdec` uses for runtime decoder switching.  Both are
        async commands, so the main thread never blocks on mpv (R9)."""
        log.info("render context re-created: re-attaching video track")
        self.core.command("set", "vid", "no")
        self.core.command("set", "vid", "auto")

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
        # R27-A measurement (ROUND27-BRIEF §1): fullscreen replaces the GL
        # surface; IF the first draws after the re-realize carried a
        # degenerate area (or scale), the FBO would be 0x0 and every later
        # draw would blit nothing.  One rate-limited DEBUG line per CHANGE
        # of the target geometry — never a per-frame log.
        probe = (area.get_width(), area.get_height(),
                 area.get_scale_factor(), self._fbo.fbo,
                 self._fbo.w, self._fbo.h)
        now = time.monotonic()
        if probe != self._fbo_probe_last and now - self._fbo_probe_t > 0.1:
            self._fbo_probe_last = probe
            self._fbo_probe_t = now
            log.debug("fbo probe: area=%dx%d scale=%d -> fbo=%d (%dx%d)",
                      probe[0], probe[1], probe[2], probe[3],
                      probe[4], probe[5])
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
        # R27-A: one lifecycle line per event, carrying the draw counter —
        # "did the pump draw anything after this cycle" becomes auditable.
        log.debug("unrealize: render_count=%d", self.render_count)
        # R27-B: PARK — do NOT free here (measured, ROUND27 §0 follow-up).
        # Freeing here detached mpv's render context while the player thread
        # re-initialised the VO inside the unrealize→realize gap: vo/libmpv
        # preinit REQUIRES a render context (0.35 source: "No render context
        # set." → -1), and that failure is FATAL to the current playlist
        # entry (measured: `end_file reason=4 err=-15 "video output
        # initialization failed"`, then idle — the file is simply gone and
        # the §0.7 reattach re-attaches nothing, because vid no/auto fires
        # against a dead entry).  Parking keeps mpv's render context
        # ATTACHED for the whole cycle — the context is GL-free to leave
        # idle (only create/render/free touch GL, §0.6) — and the next
        # realize frees it under the new current context before re-creating
        # (R23 order unchanged).  A LIFECYCLE CYCLE still means "keep the
        # view alive" (R23): nothing here latches, nothing terminates.
        #
        # R23: fullscreen / maximize-restore reparents the window, which
        # unrealizes the GLArea.  The old code routed this through
        # teardown(), whose `teared_down` latch is one-shot — so the SECOND
        # unrealize freed nothing, the rctx leaked, and mpv refused the next
        # mpv_render_context_create (the user's crash on the second
        # double-click).
        # R27-B: and DROP THE VIDEO CHAIN before the gap.  Even parked, the
        # context is UNLINKED the instant the next realize frees it, and a
        # still-alive video chain re-initialising the VO at that moment is
        # the fatal above.  `set vid no` here (async, §0.7's proven track
        # switch, position survives) means there is no video chain left to
        # re-init anywhere in the cycle; the reattach at the next realize
        # rebuilds it against the new context.
        if self.rctx is not None:
            self.core.command("set", "vid", "no")

    def _make_current(self) -> bool:
        """Make this view's GL context current again (§0.6: mpv must be
        freed with its GL context still current).

        Order matters.  A REALIZED area is the case GTK owns and the case the
        pre-R23 code already exercised (`area.make_current()`), and measured
        during a fullscreen unrealize `get_realized()` is still True — so that
        path is taken first.  Only when the area is no longer realized do we
        reach for `area.get_context()`: GTK4 keeps the GdkGLContext alive
        across unrealize, so the very context mpv was created against is still
        usable, and it is the only way to honour §0.6 at that point.  Asking
        for the context first would touch a GdkGLContext that may be
        finalizing during widget destruction for no benefit in the common
        case.  Returns False when no context can be made current."""
        try:
            if self.area.get_realized():
                self.area.make_current()
                return True
        except Exception:                           # noqa: BLE001
            pass                                    # area already gone
        get_ctx = getattr(self.area, "get_context", None)
        if get_ctx is not None:
            try:
                ctx = get_ctx()
            except Exception:                       # noqa: BLE001
                ctx = None
            if ctx is not None:
                try:
                    ctx.make_current()
                    return True
                except Exception:                   # noqa: BLE001
                    pass
        return False

    def _free_rctx(self, force: bool = False) -> bool:
        """Free the live render context with the GL context current (§0.6,
        R11).  Idempotent: nothing to free ⇒ success, no double free.

        Returns True when every outstanding context is gone.  If no GL
        context can be made current, the context is parked in
        `_pending_free` and freed at the next realize instead of being
        freed under no context (which is what left mpv's GL state dangling
        and aborted in the user's session).

        `force=True` is the close path, where there is no next realize to
        pick the parked context up: free under whatever context is current
        anyway rather than hand mpv a live render context to terminate."""
        if self.rctx is None and self._pending_free is None:
            return True
        # Detach first: the update callback fires on an mpv thread (§0.3).
        self.core._data_event.clear()
        if self.rctx is not None:
            self.lib.mpv_render_context_set_update_callback(
                self.rctx, None, None)
        if not self._make_current():
            if not force:
                if self.rctx is not None:
                    self._pending_free = self.rctx
                    self.rctx = None
                log.warning("no current GL context; deferring "
                            "mpv_render_context_free to the next realize")
                return False
            log.warning("no current GL context at teardown; freeing anyway")
        if self._pending_free is not None:
            log.debug("rctx freed handle=%s (parked)", self._pending_free.value)
            self.lib.mpv_render_context_free(self._pending_free)
            self._pending_free = None
        if self.rctx is not None:
            log.debug("rctx freed handle=%s", self.rctx.value)
            self.lib.mpv_render_context_free(self.rctx)
            self.rctx = None
        return True

    def teardown(self) -> None:
        """FINAL teardown (§0.6 order, R11).  Idempotent; safe from window
        close handlers on the main thread.  After this a late realize must
        not re-create — the mpv handle is on its way out.

        R23: unrealize no longer routes through here (its one-shot latch
        leaked the render context on the second fullscreen toggle); this is
        the close/dispose path only."""
        if self.teared_down:
            return
        self.teared_down = True
        # R27-B: same belt as _on_unrealize — drop the video chain BEFORE
        # unlinking the render context, so mpv's shutdown-time VO re-init
        # cannot abort the still-loaded entry with `end_file reason=4
        # err=-15 "video output initialization failed"` (measured at close:
        # harmless to the user, but it stamps a misleading fatal into every
        # clean-exit log).
        if self.rctx is not None:
            self.core.command("set", "vid", "no")
        # force: at close there is no next realize to pick a parked context
        # up, so an un-freeable context must not be left alive against a
        # terminating mpv handle.
        self._free_rctx(force=True)

    # ------------------------------------------------------------- main poll
    def poll(self) -> None:
        """Main-thread pump (§0.3): the update callback only set a flag.

        R16-A: this pump runs on the ADAPTIVE cadence (src/cadence.py) —
        10 ms while frames are arriving, 100 ms while nothing is — and it is
        never suspended while the window lives, so video and the mpv event
        queue are always drained.  The frame flag read here is what tells
        the driver "pictures are moving" (frame_age_ms).
        """
        if self.rctx is None or not self.update_flag.is_set():
            return
        self.update_flag.clear()
        flags = self.lib.mpv_render_context_update(self.rctx)
        if flags & M.MPV_RENDER_UPDATE_FRAME and not self.teared_down:
            self._last_frame_wall = time.monotonic()
            self.area.queue_render()
        # R27-A measurement: ONE rate-limited liveness line (≥5 s apart)
        # proving the pump itself still runs and what mpv still reports —
        # the audit trail that separates "flags dead" from "pump dead".
        now = time.monotonic()
        if now - self._pump_probe_t > 5.0:
            self._pump_probe_t = now
            log.debug("pump alive: rc=%d flags=%s age_ms=%s tpos=%s",
                      self.render_count, flags,
                      None if self._last_frame_wall is None else
                      f"{(now - self._last_frame_wall) * 1000.0:.0f}",
                      self.core.snapshot.get("time-pos"))

    def frame_age_ms(self) -> float | None:
        """Real-time ms since the last frame flag was drained here (None =
        never one).  A paused / video-off / media-less track ages out; a
        playing one keeps it near 0, which is exactly the ladder's test."""
        if self._last_frame_wall is None:
            return None
        return (time.monotonic() - self._last_frame_wall) * 1000.0
