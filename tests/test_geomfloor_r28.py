"""ROUND 28 §3-D gate: an unusable rect is never remembered, and an
already-broken window.json heals itself on load.

The operator's file (references/ROUND28-BRIEF.md §0): window.json held
102x102 / 100x100 — the corpse of the WM clamp fight — and every later
launch "showed no window at all" because the app faithfully restored
exactly that.  The persist-side rule is now the app's own usable-window
floor: anything below aspect.MIN_W/MIN_H (320x180) is refused by
remember() (returned False, nothing written) and REPAIRED by the loader
to a sane 16:9 rect with one INFO line.  PURE (no display, no gi).

Negative control: HEAD's GeometryStore persisted 102x102 happily and its
loader returned it verbatim — run against HEAD's src this file fails on
`remember(102x102) is True`, on the un-repaired `last()`, and on the
missing wingeom helpers.  That is the point.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import _bootstrap  # noqa: F401  (sys.path)

import aspect
import config
import wingeom


class FloorPinned(unittest.TestCase):
    def test_floor_is_the_aspect_floor(self):
        # ONE language for "too small to be a window" (the brief names
        # aspect.MIN_W/MIN_H as the persist floor; wingeom stays import-
        # free, so the pin lives here instead).
        self.assertEqual((wingeom.FLOOR_W, wingeom.FLOOR_H),
                         (aspect.MIN_W, aspect.MIN_H))


class BelowFloor(unittest.TestCase):
    def test_boundaries(self):
        self.assertFalse(wingeom.below_floor((0, 0, 320, 180)))   # exact = OK
        self.assertTrue(wingeom.below_floor((0, 0, 319, 180)))
        self.assertTrue(wingeom.below_floor((0, 0, 320, 179)))
        self.assertFalse(wingeom.below_floor((0, 0, 1200, 900)))

    def test_invalid_is_not_tiny(self):
        # two error classes stay two: garbage is "not a rect" (None), not
        # "a rect that needs repairing".
        self.assertFalse(wingeom.below_floor((0, 0, -5, 5)))
        self.assertFalse(wingeom.below_floor(None))
        self.assertFalse(wingeom.below_floor({"x": 1}))


class Repair(unittest.TestCase):
    def test_operator_file_heals(self):
        for bad in ((200, 140, 102, 102), (100, 100, 100, 100)):
            r = wingeom.repair_tiny_rect(bad, (5120, 2800))
            self.assertIsNotNone(r, bad)
            x, y, w, h = r
            self.assertGreaterEqual(w, aspect.MIN_W)
            self.assertGreaterEqual(h, aspect.MIN_H)
            self.assertLessEqual(w, 5120)
            self.assertLessEqual(h, 2800)
            # sane 16:9: within one pixel of the display ratio
            self.assertLessEqual(abs(w / h - 16 / 9), 1.0 / min(w, h) + 1e-9)

    def test_healthy_and_garbage_need_no_repair(self):
        self.assertIsNone(wingeom.repair_tiny_rect((200, 140, 1200, 900)))
        self.assertIsNone(wingeom.repair_tiny_rect("nonsense"))
        self.assertIsNone(wingeom.repair_tiny_rect(None))

    def test_work_area_respected_and_defaulted(self):
        r = wingeom.repair_tiny_rect((50, 50, 64, 64), (800, 600))
        self.assertIsNotNone(r)
        self.assertLessEqual(r[2], 800)
        self.assertLessEqual(r[3], 600)
        d = wingeom.repair_tiny_rect((50, 50, 64, 64))    # work unknown
        self.assertIsNotNone(d)
        self.assertGreaterEqual(d[2], aspect.MIN_W)


class Store(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="r28-floor-"))
        self.path = self.dir / "window.json"

    def _write(self, rect):
        self.path.write_text(json.dumps(
            {"version": 1, "rect": ({"x": rect[0], "y": rect[1],
                                     "w": rect[2], "h": rect[3]}
                                    if rect else None),
             "monitor": None}))

    def test_load_repairs_tiny_file(self):
        self._write((200, 140, 102, 102))
        with self.assertLogs("reelsee.config", level="INFO") as cap:
            st = config.GeometryStore(path=self.path, debounce_s=3600.0)
        self.assertEqual(len([l for l in cap.output if "repaired" in l]), 1)
        got = st.last()
        self.assertIsNotNone(got)
        self.assertFalse(wingeom.below_floor(got))
        # the FILE heals: one flush and the tiny rect is gone for good
        st.flush()
        back = json.loads(self.path.read_text())["rect"]
        self.assertEqual((back["w"], back["h"]), (got[2], got[3]))
        self.assertFalse(wingeom.below_floor((back["x"], back["y"],
                                             back["w"], back["h"])))

    def test_healthy_file_untouched(self):
        self._write((200, 140, 1200, 900))
        st = config.GeometryStore(path=self.path, debounce_s=3600.0)
        self.assertEqual(st.last(), (200, 140, 1200, 900))
        self.assertFalse(st._dirty)         # nothing marked, nothing healed
        st.flush()                          # no-op: byte-for-byte identical
        self.assertEqual(json.loads(self.path.read_text())["rect"],
                         {"x": 200, "y": 140, "w": 1200, "h": 900})

    def test_tiny_never_persisted(self):
        self._write((200, 140, 1200, 900))
        st = config.GeometryStore(path=self.path, debounce_s=3600.0)
        self.assertFalse(st.remember((300, 300, 102, 102), persist=False))
        self.assertFalse(st.remember((300, 300, 320, 179), persist=False))
        self.assertFalse(st.remember((300, 300, 319, 900), persist=False))
        self.assertEqual(st.last(), (200, 140, 1200, 900))
        st.flush()
        back = json.loads(self.path.read_text())["rect"]
        self.assertEqual((back["x"], back["y"], back["w"], back["h"]),
                         (200, 140, 1200, 900))          # disk untouched

    def test_floor_exact_and_normal_roundtrip(self):
        self._write(None)
        st = config.GeometryStore(path=self.path, debounce_s=3600.0)
        self.assertTrue(st.remember((10, 20, 320, 180), persist=False))
        self.assertEqual(st.last(), (10, 20, 320, 180))    # exact floor OK
        self.assertTrue(st.remember((10, 20, 1600, 900), monitor=1))
        st.flush()
        st2 = config.GeometryStore(path=self.path, debounce_s=3600.0)
        self.assertEqual(st2.last(), (10, 20, 1600, 900))
        self.assertEqual(st2.last_monitor(), 1)


if __name__ == "__main__":
    unittest.main()
