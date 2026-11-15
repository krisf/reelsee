"""R23-B4 / R27-B: render-context lifecycle around the fullscreen cycle,
and the teardown idempotence the close path depends on.

The user's crash ("double clicking to maximize a video causes a crash") came
from `RenderView._on_unrealize()` routing the fullscreen reparent through
`teardown()`, whose `teared_down` latch is one-shot: the first unrealize
freed the render context and latched, the second freed nothing, the context
leaked, and mpv refused the next `mpv_render_context_create`.

R27-B moved the free from unrealize to the next realize (PARK): unlinking
the render context while a video chain still lives lets the player thread
re-init the VO against `render_context == NULL`, which vo/libmpv answers
with -1 and player/video.c with `MPV_ERROR_VO_INIT_FAILED` — an END_FILE
reason=4 that KILLS the playlist entry (the operator's black fullscreen).
Unrealize now only issues the `set vid no` gap guard; these tests pin the
new contract (the out-of-process witness is scripts/smoke_fullscreen_real.py).

These tests drive the lifecycle methods directly with a stub libmpv and a
stub GLArea, so they need no display and no mpv (the end-to-end version is
scripts/smoke_render_cycle.py).  `RenderView.__init__` builds a real
Gtk.GLArea and resolves EGL entry points, so the instance is assembled
explicitly instead — the stubs are the point.
"""
import ctypes
import threading
import unittest

import _bootstrap  # noqa: F401  (sys.path for src/)

import render as render_mod  # noqa: E402


class FakeGLContext:
    def __init__(self, log):
        self.log = log

    def make_current(self):
        self.log.append("make_current")


class FakeArea:
    """Stands in for Gtk.GLArea.  `realized` mirrors GTK's own flag;
    `context` models GTK4 keeping the GdkGLContext alive across unrealize."""

    def __init__(self, log, realized=True, context=True):
        self.log = log
        self.realized = realized
        self.context = FakeGLContext(log) if context else None

    # GTK4 API the view uses
    def make_current(self):
        self.log.append("area.make_current")

    def get_error(self):
        return None

    def get_context(self):
        return self.context

    def get_realized(self):
        return self.realized


class FakeCore:
    def __init__(self):
        self.h = ctypes.c_void_p(0x1000)
        self._data_event = threading.Event()
        self.commands = []
        self.snapshot = {}

    def command(self, *argv):
        self.commands.append(argv)


class FakeLib:
    """Records every mpv_render_context_* call in order and hands out fresh
    context handles, so a leak (a create whose free never happened) shows up
    as create_calls > free_calls."""

    def __init__(self, fail_after=None):
        self.log = []
        self.create_calls = 0
        self.free_calls = 0
        self.fail_after = fail_after       # make create N+1 fail, if set

    def mpv_render_context_create(self, out, handle, params):
        self.create_calls += 1
        if self.fail_after is not None and self.create_calls > self.fail_after:
            return -1
        ctypes.cast(out, ctypes.POINTER(ctypes.c_void_p))[0] = 0x2000 + self.create_calls
        self.log.append("create")
        return 0

    def mpv_render_context_free(self, rctx):
        self.free_calls += 1
        self.log.append(f"free:{rctx.value:#x}")

    def mpv_render_context_set_update_callback(self, rctx, cb, user):
        self.log.append(f"set_cb:{'None' if cb is None else 'cb'}")

    def mpv_error_string(self, code):            # used by the error log line
        return b"something happened"


def bare_view(lib=None, core=None, area=None):
    """A RenderView assembled without Gtk.GLArea / EGL (no display needed)."""
    v = render_mod.RenderView.__new__(render_mod.RenderView)
    v.lib = lib or FakeLib()
    v.core = core or FakeCore()
    v.on_ready = None
    v.rctx = None
    v.teared_down = False
    v._pending_free = None
    v._had_rctx = False
    v.render_count = 0
    v._last_frame_wall = None
    v.update_flag = threading.Event()
    v._update_cb = None
    v._get_proc = render_mod.GETPROC(lambda *_a: None)   # keep referenced
    v._init_params = render_mod.M.MpvOpenGLInitParams(v._get_proc, None)
    v._flip = ctypes.c_int(1)
    v._fbo = render_mod.M.MpvOpenGLFbo(0, 0, 0, 0)
    v.area = area if area is not None else FakeArea(v.lib.log)
    return v


class TestRenderCycle(unittest.TestCase):
    def test_first_realize_creates_once(self):
        v = bare_view()
        v._on_realize(v.area)
        self.assertIsNotNone(v.rctx)
        self.assertEqual(v.lib.create_calls, 1)
        self.assertEqual(v.lib.free_calls, 0)

    def test_unrealize_is_a_lifecycle_park_not_a_shutdown(self):
        """R27-B contract change (was: lifecycle FREE).  Unrealize must not
        touch mpv_render_context_free at all: freeing here unlinks the
        render context while the player thread can still re-initialise the
        VO, and that re-init is FATAL to the playlist entry (measured
        `end_file reason=4 err=-15`, the operator's black fullscreen).  The
        context is PARKED — left attached, GL-free to idle — and the only
        mpv traffic at unrealize is the `set vid no` that removes the video
        chain able to attempt that fatal re-init.  Still the R23 contract:
        nothing latches, the next realize frees-under-new-GL then creates."""
        v = bare_view()
        v._on_realize(v.area)
        v._on_unrealize(v.area)
        self.assertIsNotNone(v.rctx, "unrealize must PARK, not free")
        self.assertEqual(v.lib.free_calls, 0, "no free during the gap")
        self.assertFalse(v.teared_down, "unrealize must not latch the view off")
        self.assertEqual(v.core.commands, [("set", "vid", "no")],
                         "the gap needs the video chain dropped")
        # ... and the next realize frees (under the now-current GL, §0.6)
        # then re-creates — still no leak.
        v._on_realize(v.area)
        self.assertIsNotNone(v.rctx)
        self.assertEqual(v.lib.create_calls, 2)
        self.assertEqual(v.lib.free_calls, 1)

    def test_repeated_cycles_never_leak_a_context(self):
        """on -> off -> on -> off: every created context is freed."""
        v = bare_view()
        for _ in range(4):
            v._on_realize(v.area)
            v._on_unrealize(v.area)
        v._on_realize(v.area)
        self.assertEqual(v.lib.create_calls, 5)
        self.assertEqual(v.lib.free_calls, 4,
                         "one live context expected, the rest freed")
        self.assertIsNotNone(v.rctx)
        v.teardown()
        self.assertEqual(v.lib.free_calls, 5)
        self.assertIsNone(v.rctx)

    def test_teardown_after_close_frees_exactly_once(self):
        """B4: teardown() after the window is closing does not free twice."""
        v = bare_view()
        v._on_realize(v.area)
        v.teardown()
        self.assertEqual(v.lib.free_calls, 1)
        v.teardown()
        v.teardown()
        self.assertEqual(v.lib.free_calls, 1)
        self.assertIsNone(v.rctx)

    def test_teardown_after_unrealize_frees_exactly_once(self):
        """Close right after a fullscreen restore: the parked context is
        freed by teardown, exactly once (double free = SIGABRT)."""
        v = bare_view()
        v._on_realize(v.area)
        v._on_unrealize(v.area)
        v.teardown()
        self.assertEqual(v.lib.free_calls, 1)
        self.assertEqual(v.lib.create_calls, 1)

    def test_teardown_frees_even_without_a_context(self):
        """At close there is no next realize to pick a parked context up, so
        the FINAL teardown frees under whatever context exists instead of
        parking — an un-freed context against a terminating mpv handle is a
        crash-on-exit, not a leak that resolves itself."""
        area = FakeArea([], realized=False, context=False)
        v = bare_view(area=area)
        v._on_realize(v.area)
        v.teardown()
        self.assertEqual(v.lib.free_calls, 1)
        self.assertIsNone(v._pending_free)
        self.assertIsNone(v.rctx)

    def test_teardown_without_realize_is_a_noop(self):
        v = bare_view()
        v.teardown()
        self.assertEqual(v.lib.free_calls, 0)
        self.assertEqual(v.lib.create_calls, 0)
        self.assertTrue(v.teared_down)

    def test_late_realize_after_teardown_does_not_recreate(self):
        """A realize that arrives during dispose must not build a context
        against an mpv handle that is being terminated."""
        v = bare_view()
        v._on_realize(v.area)
        v.teardown()
        v._on_realize(v.area)
        self.assertEqual(v.lib.create_calls, 1)
        self.assertIsNone(v.rctx)

    def test_free_happens_with_the_gl_context_current(self):
        """AGENTS.md §0.6 / R11: free mpv while its GL context is current —
        R27: the free moved from unrealize to the NEXT realize, so the
        order is make_current → free → create, always."""
        v = bare_view()
        v._on_realize(v.area)
        v._on_unrealize(v.area)
        v._on_realize(v.area)
        log = v.lib.log
        frees = [i for i, e in enumerate(log) if e.startswith("free")]
        self.assertEqual(len(frees), 1, "one park, one free at next realize")
        makes = [i for i, e in enumerate(log) if "make_current" in e]
        creates = [i for i, e in enumerate(log) if e == "create"]
        self.assertLess(makes[-1], frees[0],
                        "mpv_render_context_free ran before a make_current")
        self.assertLess(frees[0], creates[1],
                        "the re-create must follow the free (R23 order)")

    def test_realized_area_path_is_preferred_over_the_saved_context(self):
        """A realized GLArea is the case GTK owns and the case the pre-R23
        code already exercised, so it is used first; the GdkGLContext is only
        reached for once the area is NOT realized.  Touching it earlier means
        asking a context that may be finalizing during widget destruction to
        become current — the shutdown segfault class."""
        v = bare_view()
        v._on_realize(v.area)
        v._on_unrealize(v.area)
        self.assertIn("area.make_current", v.lib.log)
        self.assertNotIn("make_current", v.lib.log)

    def test_unrealized_area_falls_back_to_the_saved_context(self):
        """GTK4 keeps the GdkGLContext alive across unrealize (spike-
        proven), so the CLOSE path's force-free — taken while the area is
        unrealized but the saved context still lives — still honours §0.6:
        via the context, not by freeing under nothing.  (Pre-R27 this ran
        at unrealize; R27 moved the whole cycle, the fallback did not
        change.)"""
        lib = FakeLib()
        area = FakeArea(lib.log, realized=True, context=True)
        v = bare_view(lib=lib, area=area)
        v._on_realize(v.area)
        area.realized = False
        v.teardown()
        self.assertIn("make_current", v.lib.log)
        self.assertEqual(v.lib.free_calls, 1)
        self.assertIsNone(v.rctx)

    def test_free_is_deferred_when_no_context_can_be_made_current(self):
        """The `_pending_free` mechanism still exists (defensive: any
        non-forced `_free_rctx` that cannot make a GL current parks the
        context and frees it once one is available — never under nothing).
        R27 additionally made the GAP itself safe by construction:
        `_on_unrealize` performs NO free even in this worst-case area state,
        so the operator's fatal re-init window has no unlink in it at all."""
        area = FakeArea([], realized=False, context=False)
        v = bare_view(area=area)
        v._on_realize(v.area)
        created = v.lib.create_calls
        v._on_unrealize(v.area)
        self.assertEqual(v.lib.free_calls, 0, "unrealize must never free")
        self.assertIsNotNone(v.rctx, "…it parks by leaving it attached")
        self.assertIsNone(v._pending_free)
        # the defensive mechanism, exercised directly: a non-forced free
        # with nothing current parks instead of freeing under no context
        self.assertFalse(v._free_rctx())
        self.assertEqual(v.lib.free_calls, 0)
        self.assertIsNone(v.rctx)
        self.assertIsNotNone(v._pending_free)
        # …and it is picked up as soon as a GL current exists (next realize
        # frees BEFORE the create, R23 order).
        area.realized = True
        area.context = FakeGLContext(v.lib.log)
        v._on_realize(v.area)
        self.assertEqual(v.lib.free_calls, 1)
        self.assertEqual(v.lib.create_calls, created + 1)
        self.assertIsNone(v._pending_free)
        self.assertIsNotNone(v.rctx)

    def test_recreate_reattaches_the_video_track(self):
        """R23: after a free/create this libmpv's VO stops delivering
        frames (mpv_render_context_update() returns 0 forever) unless the
        video track is re-attached — §0.7's `set vid no` -> `set vid auto`.
        R27 adds the PRE-gap `set vid no` (unrealize): it removes the video
        chain that could re-init the VO fatally while the context is being
        swapped.  So a full cycle is no (gap) + no/auto (reattach), and the
        FIRST create still never disturbs track selection."""
        v = bare_view()
        v._on_realize(v.area)
        self.assertEqual(v.core.commands, [],
                         "the FIRST create must not disturb track selection")
        v._on_unrealize(v.area)
        self.assertEqual(v.core.commands, [("set", "vid", "no")],
                         "unrealize must drop the chain BEFORE the gap")
        v._on_realize(v.area)
        self.assertEqual(v.core.commands,
                         [("set", "vid", "no"),        # R27-B gap guard
                          ("set", "vid", "no"),        # R23 §0.7 reattach
                          ("set", "vid", "auto")])

    def test_unrealize_without_a_context_writes_nothing(self):
        """Negative control for the R27-B belt: the vid-no at unrealize is
        conditional on an attached render context — a stray unrealize of a
        view that never created one must leave mpv untouched."""
        v = bare_view()
        v._on_unrealize(v.area)
        self.assertEqual(v.core.commands, [])

    def test_create_failure_leaves_no_context_and_still_frees(self):
        """Negative control: the stub really does refuse the second create,
        and the view neither crashes nor pretends to hold a context."""
        lib = FakeLib(fail_after=1)
        v = bare_view(lib=lib)
        v._on_realize(v.area)
        self.assertIsNotNone(v.rctx)
        v._on_unrealize(v.area)
        v._on_realize(v.area)
        self.assertIsNone(v.rctx)
        self.assertEqual(v.lib.create_calls, 2)
        v.teardown()
        self.assertEqual(v.lib.free_calls, 1)


if __name__ == "__main__":
    unittest.main()
