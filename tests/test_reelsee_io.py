"""§4.8 reelsee_io — natural sort, extension filter, async scan,
FileMonitor refresh, worker probe with supersede-cancel, latency hook."""
import _bootstrap  # noqa: F401

import os
import shutil
import tempfile
import threading
import time
import unittest

import reelsee_io as IO
from gi.repository import GLib

from _bootstrap import REPO, SRC  # noqa: F401

TESTDATA = REPO / "testdata"


def run_loop(ms: int) -> None:
    loop = GLib.MainLoop()
    GLib.timeout_add(ms, loop.quit)
    loop.run()


class NaturalSort(unittest.TestCase):
    def test_digit_run_ordering(self):
        self.assertEqual(
            IO.natural_sort(["ep10.mp4", "ep2.mp4", "ep1.mp4"]),
            ["ep1.mp4", "ep2.mp4", "ep10.mp4"])

    def test_case_insensitive_and_paths(self):
        out = IO.natural_sort(["/v/B.mp4", "/v/a.mp4", "/v/2x.mp4",
                               "/v/10x.mp4"])
        self.assertEqual([os.path.basename(p) for p in out],
                         ["2x.mp4", "10x.mp4", "a.mp4", "B.mp4"])

    def test_ext_filter(self):
        self.assertTrue(IO.is_video("MOVIE.MKV"))
        self.assertFalse(IO.is_video("notes.txt"))
        self.assertFalse(IO.is_video("noext"))


class FolderScan(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(dir=str(REPO / "scratch"))
        for n in ("ep2.mp4", "ep10.mp4", "ep1.mkv", "cover.jpg",
                  "sub.srt"):
            open(os.path.join(self.dir, n), "wb").close()
        os.mkdir(os.path.join(self.dir, "nested.mp4"))   # dir: not a file

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)
        os.environ.pop("REELSEE_FAKE_LATENCY_MS", None)

    def _scan(self, timeout_ms=3000):
        got = []
        IO.FolderScan(self.dir, got.append).start()
        deadline = time.monotonic() + timeout_ms / 1000.0
        loop = GLib.MainLoop()
        def _q():
            if got or time.monotonic() > deadline:
                loop.quit()
            return True
        GLib.timeout_add(20, _q)
        loop.run()
        return got

    def test_scan_filters_and_naturally_sorts(self):
        got = self._scan()
        self.assertEqual([os.path.basename(p) for p in got[0]],
                         ["ep1.mkv", "ep2.mp4", "ep10.mp4"])
        self.assertTrue(all(p.startswith(self.dir) for p in got[0]))

    def test_unreadable_dir_delivers_empty(self):
        got = []
        IO.FolderScan(os.path.join(self.dir, "nope"), got.append).start()
        run_loop(1500)
        self.assertEqual(got, [[]])

    def test_cancel_stops_delivery(self):
        got = []
        s = IO.FolderScan(self.dir, got.append)
        s.start()
        s.cancel()
        run_loop(1500)
        self.assertEqual(got, [])

    def test_fake_latency_defers_callback(self):
        os.environ["REELSEE_FAKE_LATENCY_MS"] = "400"
        self.assertAlmostEqual(IO.fake_latency_s(), 0.4)
        t0 = time.monotonic()
        got = self._scan()
        self.assertEqual(len(got), 1)
        self.assertGreaterEqual(time.monotonic() - t0, 0.4)

    def test_bad_env_value_is_zero(self):
        os.environ["REELSEE_FAKE_LATENCY_MS"] = "banana"
        self.assertEqual(IO.fake_latency_s(), 0.0)


class FolderMonitor(unittest.TestCase):
    def test_change_debounces_to_one_fire(self):
        d = tempfile.mkdtemp(dir=str(REPO / "scratch"))
        try:
            events = []
            m = IO.FolderMonitor(d, lambda p: events.append(p)).start()
            run_loop(300)                      # let inotify settle
            for n in ("x.mp4", "y.mp4", "z.mp4"):
                open(os.path.join(d, n), "wb").close()
            run_loop(700)
            m.close()
            self.assertGreaterEqual(len(events), 1)
            self.assertLessEqual(len(events), 3)   # debounced, not per-file×N
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_close_is_idempotent(self):
        d = tempfile.mkdtemp(dir=str(REPO / "scratch"))
        try:
            m = IO.FolderMonitor(d, lambda p: None).start()
            m.close()
            m.close()
        finally:
            shutil.rmtree(d, ignore_errors=True)


class NextProbe(unittest.TestCase):
    """R3-P3(a): the ffprobe worker never touches GLib — results travel on
    a queue with a generation token and the OWNER drains them via poll(),
    so delivery is main-thread by construction."""

    def setUp(self):
        self.seen = []

    def _done(self, path, info):
        self.seen.append((path, info))

    def _drain(self, probe, ms=4000):
        """Run the GLib loop the way the window's 10 ms tick does: poll()
        from the MAIN thread until delivery or deadline."""
        loop = GLib.MainLoop()

        def _q():
            probe.poll()
            if self.seen:
                loop.quit()
            return True

        GLib.timeout_add(10, _q)
        GLib.timeout_add(ms, loop.quit)
        loop.run()

    def test_probe_real_clip(self):
        p = IO.NextProbe(self._done)
        p.probe(str(TESTDATA / "smoke_360p.mp4"))
        self._drain(p)
        path, info = self.seen[0]
        self.assertTrue(path.endswith("smoke_360p.mp4"))
        self.assertAlmostEqual(info["duration"], 3.0, delta=0.5)
        self.assertEqual(info["width"], 640)
        self.assertEqual(info["height"], 360)
        self.assertTrue(info["has_video"])

    def test_supersede_drops_old_result(self):
        p = IO.NextProbe(self._done)
        p.probe(str(TESTDATA / "does-not-exist.mkv"))   # old: never lands
        p.probe(str(TESTDATA / "smoke_360p.mp4"))       # new: lands
        self._drain(p)
        # exactly one delivered result and it is the NEW path
        self.assertEqual(len(self.seen), 1)
        self.assertTrue(self.seen[0][0].endswith("smoke_360p.mp4"))

    def test_missing_binary_silently_drops(self):
        p = IO.NextProbe(self._done, ffprobe="/nonexistent/ffprobe")
        p.probe("whatever.mp4")
        self._drain(p, ms=1200)
        self.assertEqual(self.seen, [])

    def test_poll_idle_is_false(self):
        p = IO.NextProbe(self._done)
        self.assertFalse(p.poll())

    def test_cancel_drops_result(self):
        p = IO.NextProbe(self._done)
        # simulate a landed result, then cancel: poll must drop it
        p._results.put((p._gen, ("/x.mp4", {})))
        # mirror production: probe() clears a cancelled Event before it
        # starts a worker, so _cancel is fresh-but-set here
        p._cancel = threading.Event()
        p.cancel()
        self.assertFalse(p.poll())
        self.assertEqual(self.seen, [])

    def test_stale_generation_dropped(self):
        p = IO.NextProbe(self._done)
        p._gen = 5
        p._results.put((4, ("/stale.mp4", {})))    # superseded generation
        self.assertFalse(p.poll())
        self.assertEqual(self.seen, [])

    def test_worker_runs_off_main_thread(self):
        main = threading.get_ident()
        worker_ids = []
        delivered_on = []

        class P(IO.NextProbe):
            def _run(self, path, ev, gen):   # record the worker identity
                worker_ids.append(threading.get_ident())
                return super()._run(path, ev, gen)

        def done(path, info):
            delivered_on.append(threading.get_ident())
            self._done(path, info)

        p = P(done)
        p.probe(str(TESTDATA / "smoke_360p.mp4"))
        # no poll() has run yet: nothing may have been delivered — the old
        # GLib.timeout_add-from-worker path delivered WITHOUT any poll
        run_loop(300)
        self.assertEqual(self.seen, [])
        self._drain(p)
        self.assertEqual(len(worker_ids), 1)
        self.assertNotEqual(worker_ids[0], main)   # ffprobe ran on worker
        self.assertEqual(delivered_on, [main])     # ... delivered on MAIN


if __name__ == "__main__":
    unittest.main()
