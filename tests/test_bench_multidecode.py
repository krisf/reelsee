"""R16-D gate: the decode-capacity bench's own rule and constants.

`scripts/bench_multidecode.py` is an OPERATOR tool — it needs the real iGPU
and is deliberately NOT part of the headless battery (llvmpipe numbers say
nothing about a Radeon).  What can be gated headlessly is the pure logic
inside it: the margin rule that turns per-stream counters into a capacity,
and the libmpv event constant that decides whether a stream is still running.

Both of those had real bugs during development, found by measurement rather
than reading: `mpv_wait_event` with a negative timeout blocks forever in this
build, and MPV_EVENT_END_FILE is 7 here (3 in older libmpv) — with a guessed 8
the bench mistook FILE_LOADED for EOF and reported capacity=0 four tenths of
a second into a run.
"""
import importlib.util
import re
import unittest

from _bootstrap import REPO

SCRIPT = REPO / "scripts" / "bench_multidecode.py"


def load_bench():
    spec = importlib.util.spec_from_file_location("bench_multidecode", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


B = load_bench()


def stream(fps, src=30.0, adv=1.0, drops=(0, 0, 0), n=4, dt=0.2,
           drop_growth=0):
    """A Stream that never touched libmpv: only the reporting half is used,
    so the rule under test runs on the real code path."""
    s = B.Stream(None, 0, "clip.mp4", {"hwdec": "vaapi"})
    s.t0 = 0.0
    s.t1 = n * dt
    s.src_fps = src
    for i in range(n):
        grown = tuple(d + drop_growth * i for d in drops)
        s.samples.append([i * dt, fps, *grown, i * adv * dt * src])
    s.progress = (n - 1) * adv * dt * src
    return s


class MarginRule(unittest.TestCase):
    def test_capacity_is_the_highest_healthy_prefix(self):
        good, bad = stream(30.0), stream(12.0)
        self.assertEqual(B.capacity_rule([good, good, bad, good]), 2)

    def test_all_healthy_is_everything(self):
        self.assertEqual(B.capacity_rule([stream(30.0)] * 4), 4)

    def test_empty_is_zero_not_a_crash(self):
        self.assertEqual(B.capacity_rule([]), 0)

    def test_below_the_floor_does_not_count(self):
        # the floor is absolute: even a 20 fps clip has to hold 24 fps of
        # decode headroom, so 23.9 fails and 24.0 passes
        self.assertEqual(B.capacity_rule([stream(24.0, src=20.0)]), 1)
        self.assertEqual(B.capacity_rule([stream(23.9, src=20.0)]), 0)

    def test_a_stream_slower_than_its_own_clip_fails(self):
        """The 24 fps floor alone is not enough: a 60 fps clip decoding at
        30 fps clears the floor and still drops every other frame."""
        s = stream(30.0, src=60.0)
        self.assertFalse(s.keeps_up())
        self.assertEqual(B.capacity_rule([s]), 0)

    def test_rising_drop_trend_breaks_the_rule(self):
        s = stream(30.0, drop_growth=1)          # counters climb every sample
        self.assertTrue(s.keeps_up())            # fps is fine ...
        self.assertEqual(s.drop_delta(), 6)      # ... but frames are being lost
        self.assertEqual(B.capacity_rule([s]), 0)

    def test_stalled_stream_is_not_plausible(self):
        """A dead decoder keeps reporting the last fps; position stops."""
        s = stream(30.0, adv=0.0)
        self.assertEqual(s.advance_rate(), 0.0)
        self.assertFalse(s.keeps_up())


class EventConstant(unittest.TestCase):
    def test_end_file_matches_the_vendored_header(self):
        hdr = (REPO / "vendor" / "usr" / "include" / "mpv" / "client.h")
        m = re.search(r"MPV_EVENT_END_FILE\s*=\s*(\d+)",
                      hdr.read_text(encoding="utf-8"))
        self.assertIsNotNone(m, "vendored client.h lost the enum")
        self.assertEqual(B.MPV_EVENT_END_FILE, int(m.group(1)))

    def test_end_file_is_not_the_file_loaded_value(self):
        """The specific bug this guard was written for: 8 is FILE_LOADED in
        this build, and treating it as END_FILE ends every stream at once."""
        hdr = (REPO / "vendor" / "usr" / "include" / "mpv" / "client.h")
        text = hdr.read_text(encoding="utf-8")
        loaded = int(re.search(r"MPV_EVENT_FILE_LOADED\s*=\s*(\d+)",
                               text).group(1))
        self.assertNotEqual(B.MPV_EVENT_END_FILE, loaded)


class HarnessSelfTest(unittest.TestCase):
    def test_bench_runs_cpu_only_and_reports_counters(self):
        """The headless battery runs the bench once with --hwdec no --n 2.
        That is a self-test of the harness, so the gate here is the same
        thing the battery will check: it completes, and the counters are
        plausible (real sample count, real wall time, a capacity number)."""
        import subprocess
        import sys
        root = str(REPO)
        env = {"LD_LIBRARY_PATH": str(REPO / "vendor" / "usr"
                                      / "lib" / "x86_64-linux-gnu"),
               "PATH": "/usr/bin:/bin"}
        r = subprocess.run(
            [sys.executable, str(SCRIPT), "--hwdec", "no", "--n", "2",
             "--duration", "3"],
            cwd=root, env=env, capture_output=True, text=True, timeout=180)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("BENCH_CONFIG n=2 hwdec=no", r.stdout)
        lines = [ln for ln in r.stdout.splitlines()
                 if ln.startswith("BENCH_STREAM:")]
        self.assertEqual(len(lines), 2, r.stdout)
        summary = [ln for ln in r.stdout.splitlines()
                   if ln.startswith("BENCH_SUMMARY")]
        self.assertEqual(len(summary), 1, r.stdout)
        fields = dict(kv.split("=", 1) for kv in summary[0].split()[1:])
        self.assertEqual(fields["ok"], "1")
        self.assertEqual(fields["capacity"], "2")
        self.assertGreaterEqual(float(fields["min_vf_fps"]), B.FPS_FLOOR)
        for ln in lines:
            f = dict(kv.split("=", 1) for kv in ln.split()[1:])
            self.assertGreaterEqual(int(f["samples"]), 3, ln)
            self.assertGreater(float(f["wall"]), 1.0, ln)
            self.assertEqual(f["keeps_up"], "1", ln)


if __name__ == "__main__":
    unittest.main()
