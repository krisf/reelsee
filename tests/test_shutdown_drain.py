"""Exit-time crash gate: never let CPython finalize under a live mpv thread.

The battery's post-assert crashes were not assertion failures at all: every
`SMOKE_CHECK` passed, `SMOKE_ASSERTS_PASSED` printed, and only then did the
process die — `Fatal Python error: gilstate_tss_set: failed to set current
tstate (TSS)` (SIGABRT) or `Segmentation fault (core dumped)`.  The window
close queues `core.destroy()` (`mpv_terminate_destroy`) on a worker and
`run()` joined those workers with a 3 s cap; on a loaded machine the cap
expires, `run()` returns, and the interpreter finalizes while libmpv's threads
— and the event thread, which wakes into Python and needs the GIL — are still
alive.  A thread that asks for the GIL after finalization has started cannot
get it.

`reelsee.drain_retiring()` is the seam: join with the cap, give a straggler a
longer grace, and if it still will not finish, leave without running
finalizers (after flushing stdio and the log handlers, so a smoke's summary
lines survive).  These tests drive it with fake workers, so no display and no
mpv are needed.
"""
import inspect
import threading
import unittest

import _bootstrap  # noqa: F401  (sys.path for src/)

import reelsee


class Recorder:
    """Stand-in for os._exit / logging.shutdown: records that it ran."""

    def __init__(self, raises=None):
        self.calls = []
        self.raises = raises

    def __call__(self, *args):
        self.calls.append(args)
        if self.raises is not None:
            raise self.raises
        return None


class Flushable:
    """Counts flushes; still writable, because logging's lastResort handler
    writes to sys.stderr while drain_retiring is running."""

    def __init__(self):
        self.flushes = 0
        self.text = []

    def write(self, s):
        self.text.append(s)
        return len(s)

    def flush(self):
        self.flushes += 1


def worker(keep_open, seconds=0.0):
    """A thread that finishes after `seconds`, or until `keep_open` is set."""
    def body():
        if seconds:
            threading.Event().wait(seconds)
        else:
            keep_open.wait()
    t = threading.Thread(target=body, daemon=True)
    t.start()
    return t


class TestDrainRetiring(unittest.TestCase):
    def test_finished_workers_return_the_return_code(self):
        keep = threading.Event()
        keep.set()
        w = worker(keep)
        exit_ = Recorder()
        log_close = Recorder()
        rc = reelsee.drain_retiring([w], 0, cap=0.5, grace=0.5,
                                    hard_exit=exit_, close_logging=log_close)
        self.assertEqual(rc, 0)
        self.assertEqual(exit_.calls, [], "must not hard-exit when mpv is done")
        self.assertEqual(log_close.calls, [])

    def test_straggler_gets_a_longer_grace(self):
        """A worker slower than the cap is not abandoned — it gets `grace`."""
        keep = threading.Event()
        w = worker(keep, seconds=0.15)
        exit_ = Recorder()
        rc = reelsee.drain_retiring([w], 0, cap=0.01, grace=5.0, hard_exit=exit_,
                                    close_logging=Recorder())
        self.assertEqual(rc, 0)
        self.assertEqual(exit_.calls, [])
        self.assertFalse(w.is_alive())

    def test_hung_worker_leaves_without_finalizers(self):
        """The regression: returning here while the worker is alive is what
        let the interpreter finalize under a live mpv thread."""
        keep = threading.Event()
        w = worker(keep)
        try:
            rc = reelsee.drain_retiring(
                [w], 3, cap=0.01, grace=0.05,
                hard_exit=Recorder(raises=SystemExit(3)),
                close_logging=Recorder())
        except SystemExit as exc:
            self.assertEqual(exc.code, 3)
        else:
            self.fail(f"drain_retiring returned {rc} with a live mpv thread")
        keep.set()
        w.join(timeout=2.0)

    def test_hard_exit_carries_the_real_return_code(self):
        keep = threading.Event()
        w = worker(keep)
        exit_ = Recorder(raises=SystemExit(0))
        try:
            reelsee.drain_retiring([w], 7, cap=0.01, grace=0.05,
                                   hard_exit=exit_, close_logging=Recorder())
        except SystemExit:
            pass
        self.assertEqual(exit_.calls, [(7,)])
        keep.set()
        w.join(timeout=2.0)

    def test_stdio_and_logging_are_flushed_before_leaving(self):
        """os._exit skips the interpreter's teardown, so the smoke's summary
        lines have to be pushed out by hand first — otherwise the battery
        would fail on a missing SMOKE_ASSERTS_PASSED instead of a crash."""
        keep = threading.Event()
        w = worker(keep)
        out, err, log_close = Flushable(), Flushable(), Recorder()
        order = []

        def hard_exit(_rc):
            order.append("exit")
            raise SystemExit(0)

        def close_logging():
            order.append("close_logging")
            log_close()

        orig_out, orig_err = reelsee.sys.stdout, reelsee.sys.stderr
        reelsee.sys.stdout, reelsee.sys.stderr = out, err
        try:
            try:
                reelsee.drain_retiring([w], 0, cap=0.01, grace=0.05,
                                       hard_exit=hard_exit,
                                       close_logging=close_logging)
            except SystemExit:
                pass
        finally:
            reelsee.sys.stdout, reelsee.sys.stderr = orig_out, orig_err
            keep.set()
            w.join(timeout=2.0)
        self.assertGreaterEqual(out.flushes, 1)
        self.assertGreaterEqual(err.flushes, 1)
        self.assertEqual(order, ["close_logging", "exit"],
                         "log handlers must be closed before os._exit")

    def test_run_is_wired_to_the_drain(self):
        """A helper that exists but is never called passes every test above
        and still crashes the app, so pin the wiring."""
        src = inspect.getsource(reelsee.run)
        self.assertIn("drain_retiring", src)


if __name__ == "__main__":
    unittest.main()
