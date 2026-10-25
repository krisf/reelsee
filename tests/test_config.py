"""P1 gate: config schema, corrupt tolerance, unknown-key preservation,
atomic writes, debounced/immediate persistence (R-4)."""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path

import _bootstrap  # noqa: F401
import config


class TmpDirCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.path = self.dir / "config.json"


class TestDefaults(TmpDirCase):
    def test_missing_file_gives_defaults(self):
        cfg = config.Config(self.path)
        self.assertEqual(cfg.data, config.DEFAULTS)
        self.assertTrue(cfg.get("audio.muted"))          # R-4 default mute
        self.assertEqual(cfg.get("engine.decoder"), "auto")
        self.assertEqual(cfg.get("ab.count"), "inf")
        self.assertIsNone(cfg.get("nope.nope"))
        self.assertEqual(cfg.get("nope", "dflt"), "dflt")

    def test_corrupt_file_never_crashes(self):
        self.path.write_text("{ this is not json ]")
        cfg = config.Config(self.path)
        self.assertEqual(cfg.data, config.DEFAULTS)

    def test_corrupt_variants(self):
        for junk in ("", "[1,2,3]", "null", '"str"', "\x00\x01"):
            self.path.write_text(junk)
            cfg = config.Config(self.path)
            self.assertEqual(cfg.data, config.DEFAULTS)

    def test_per_key_merge(self):
        self.path.write_text(json.dumps(
            {"audio": {"muted": False}, "engine": {"decoder": "cpu"}}))
        cfg = config.Config(self.path)
        self.assertFalse(cfg.get("audio.muted"))
        self.assertEqual(cfg.get("audio.volume"), 100.0)   # untouched default
        self.assertEqual(cfg.get("engine.decoder"), "cpu")


class TestUnknownKeys(TmpDirCase):
    def test_unknown_keys_survive_roundtrip(self):
        self.path.write_text(json.dumps({
            "audio": {"muted": False, "custom_new_key": {"deep": [1, 2]}},
            "future_section": {"a": 1},
        }))
        cfg = config.Config(self.path)
        cfg.set("audio.volume", 55.0)
        cfg.flush_now()
        raw = json.loads(self.path.read_text())
        self.assertEqual(raw["audio"]["custom_new_key"], {"deep": [1, 2]})
        self.assertEqual(raw["future_section"], {"a": 1})
        self.assertEqual(raw["audio"]["volume"], 55.0)


class TestAtomicWrite(TmpDirCase):
    def test_uses_replace_and_leaves_no_tmp(self):
        calls = []
        real_replace = os.replace

        def spy(src, dst, *a, **k):
            calls.append((src, dst))
            return real_replace(src, dst, *a, **k)

        os.replace = spy
        try:
            cfg = config.Config(self.path)
            cfg.set("ui.theme", "black")
            cfg.flush_now()
        finally:
            os.replace = real_replace
        self.assertEqual(len(calls), 1)
        src, dst = calls[0]
        self.assertEqual(Path(dst), self.path)
        self.assertEqual(Path(src).parent, self.path.parent)   # same fs
        leftovers = [p.name for p in self.dir.iterdir() if p.name.endswith(".tmp")]
        self.assertEqual(leftovers, [])
        self.assertEqual(json.loads(self.path.read_text())["ui"]["theme"], "black")

    def test_content_valid_after_many_sets(self):
        cfg = config.Config(self.path)
        for i in range(20):
            cfg.set("audio.volume", float(i))
        cfg.flush_now()
        self.assertEqual(json.loads(self.path.read_text())["audio"]["volume"], 19.0)


class TestPersistenceTiming(TmpDirCase):
    def test_mute_and_volume_immediate(self):
        """R-4: mute/volume written on every user change, not debounced."""
        cfg = config.Config(self.path)
        cfg.set("audio.muted", False)
        self.assertFalse(json.loads(self.path.read_text())["audio"]["muted"])
        cfg.set("audio.volume", 42.0)
        self.assertEqual(json.loads(self.path.read_text())["audio"]["volume"], 42.0)

    def test_debounced_write(self):
        cfg = config.Config(self.path)
        cfg.set("playback.speed", 1.75)
        # not yet on disk (well inside the 250 ms debounce)
        if self.path.exists():
            self.assertNotEqual(
                json.loads(self.path.read_text())["playback"]["speed"], 1.75)
        time.sleep(0.5)
        self.assertEqual(json.loads(self.path.read_text())["playback"]["speed"], 1.75)


class TestStateStores(TmpDirCase):
    def test_resume_lru_and_corrupt(self):
        p = self.dir / "resume.json"
        p.write_text("garbage{")
        store = config.ResumeStore(p)
        store.cap = 3
        for i in range(5):
            store.set(f"/v/{i}.mp4", {"pos": float(i), "dur": 10.0})
            time.sleep(0.002)  # distinct ts ordering
        self.assertEqual(len(list(store.items())), 3)
        self.assertIsNone(store.get("/v/0.mp4"))
        self.assertEqual(store.get("/v/4.mp4")["pos"], 4.0)
        self.assertTrue(p.exists())

    def test_ab_store_roundtrip(self):
        p = self.dir / "ab.json"
        store = config.AbStore(p)
        store.set("/movie.mkv", {"a": 1.0, "b": 2.0})
        store2 = config.AbStore(p)
        self.assertEqual(store2.get("/movie.mkv")["a"], 1.0)


class TestSchemaKeys(unittest.TestCase):
    def test_leaf_keys_shape(self):
        keys = config.schema_leaf_keys()
        self.assertGreaterEqual(len(keys), 25)
        for must in ("audio.muted", "audio.volume", "engine.decoder",
                     "engine.watchdog_enabled", "playback.speed",
                     "ui.chrome_timeout_ms", "subtitles.visible",
                     "screenshot.directory", "mpv.options"):
            self.assertIn(must, keys)
        # every leaf key must be settable/gettable
        cfg = config.Config(Path("/nonexistent/reelsee-test/cfg.json"))
        for k in keys:
            cfg.get(k)


if __name__ == "__main__":
    unittest.main()
