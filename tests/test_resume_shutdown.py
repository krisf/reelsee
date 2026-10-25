"""R3-P1: graceful shutdown must persist the CURRENT Snapshot position.

The resume position used to be written only by the 5 s tick, and the
shutdown flush wrote *that stored value* — a clean quit before the first
tick saved nothing, otherwise up to 5 s was lost.  These tests drive the
shutdown-persist path (ReelseeWindow.capture_resume_position) with a fake
Snapshot/core/store: no display, no mpv, no file.
"""
import _bootstrap  # noqa: F401
import unittest

from window import ReelseeWindow

PATH = "/media/clip.mp4"


class FakeCore:
    def __init__(self, snap):
        self.snapshot = dict(snap)


class FakeStore:
    def __init__(self):
        self.ops: list[tuple] = []
        self.flushes = 0

    def set(self, key, value, *, persist=True):
        self.ops.append(("set", key, dict(value)))

    def remove(self, key, *, persist=True):
        self.ops.append(("remove", key))

    def persist(self):
        self.flushes += 1


class FakeApp:
    def __init__(self):
        self.resume_store = FakeStore()


class FakeWin:
    """Duck-typed window carrying exactly the members the resume path
    touches; the production methods run verbatim (no display needed)."""

    _resume_record = ReelseeWindow._resume_record
    capture_resume_position = ReelseeWindow.capture_resume_position

    def __init__(self, snap, target=PATH, resume_last=-10.0):
        self.core = FakeCore(snap)
        self.app = FakeApp()
        self.target = target
        self._resume_last = resume_last


class TestShutdownPersist(unittest.TestCase):
    def test_quit_before_first_tick_persists_position(self):
        # the operator's core case: kill at t=2 s, no tick ever fired
        w = FakeWin({"time-pos": 2.1, "duration": 10.0})
        w.capture_resume_position()
        self.assertEqual(w.app.resume_store.ops,
                         [("set", PATH, {"pos": 2.1})])

    def test_forced_write_beats_the_4s_delta_gate(self):
        # last write 1 s ago: the 5 s tick gate would SKIP, shutdown must
        # still record the exact position
        w = FakeWin({"time-pos": 7.55, "duration": 10.0}, resume_last=6.5)
        w.capture_resume_position()
        self.assertEqual(w.app.resume_store.ops,
                         [("set", PATH, {"pos": 7.55})])

    def test_tick_path_still_respects_delta_gate(self):
        w = FakeWin({"time-pos": 7.55, "duration": 10.0}, resume_last=6.5)
        w._resume_record(force=False)
        self.assertEqual(w.app.resume_store.ops, [])   # < 4.0 delta: skip
        w.core.snapshot["time-pos"] = 11.0
        w.core.snapshot["duration"] = 20.0
        w._resume_record(force=False)
        self.assertEqual(w.app.resume_store.ops,
                         [("set", PATH, {"pos": 11.0})])

    def test_near_start_clears_entry_on_shutdown(self):
        w = FakeWin({"time-pos": 0.4, "duration": 10.0})
        w.capture_resume_position()
        self.assertEqual(w.app.resume_store.ops, [("remove", PATH)])

    def test_near_eof_clears_entry_on_shutdown(self):
        w = FakeWin({"time-pos": 9.6, "duration": 10.0})
        w.capture_resume_position()
        self.assertEqual(w.app.resume_store.ops, [("remove", PATH)])

    def test_no_media_touches_nothing(self):
        # lazy-store contract: an empty window must not create/touch
        # resume.json at shutdown
        w = FakeWin({"time-pos": None}, target=None)
        w.capture_resume_position()
        self.assertEqual(w.app.resume_store.ops, [])

    def test_capture_never_raises(self):
        class BoomApp:
            @property
            def resume_store(self):
                raise RuntimeError("store exploded")

        w = FakeWin({"time-pos": 3.0, "duration": 10.0})
        w.app = BoomApp()                        # must not propagate
        w.capture_resume_position()


if __name__ == "__main__":
    unittest.main()
