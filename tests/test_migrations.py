"""R15-B: the one-time repeat-default migration (config.run_migrations).

Unit-level — no display, no mpv.  The GUI counterpart is
scripts/smoke_repeatdefault.py; these tests pin the migration contract
itself: flip exactly one literal, exactly once, touching nothing else.
"""
import json
import tempfile
import unittest
from pathlib import Path

import config


def _write(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.path = self.dir / "config.json"

    def tearDown(self):
        self._tmp.cleanup()

    # -- the flip ---------------------------------------------------------
    def test_stored_no_flips_once_and_flags(self):
        _write(self.path, {"version": 1,
                           "playback": {"speed": 1.25, "loop_file": "no",
                                        "loop_playlist": "no"},
                           "ui": {"chrome_timeout_ms": 4000},
                           "zeta_custom": {"keep": "me"}})
        config.run_migrations(self.path)
        got = _read(self.path)
        self.assertEqual(got["playback"]["loop_file"], "inf")
        self.assertTrue(got["migrations"]["repeat_default_on"])
        # NOTHING else moved (raw edit, no re-serialisation of defaults)
        self.assertEqual(got["playback"]["speed"], 1.25)
        self.assertEqual(got["playback"]["loop_playlist"], "no")
        self.assertEqual(got["ui"], {"chrome_timeout_ms": 4000})
        self.assertEqual(got["zeta_custom"], {"keep": "me"})
        self.assertEqual(set(got), {"version", "playback", "ui",
                                    "zeta_custom", "migrations"})

    def test_second_run_rewrites_nothing(self):
        _write(self.path, {"version": 1, "playback": {"loop_file": "no"}})
        config.run_migrations(self.path)
        before = self.path.read_bytes()
        config.run_migrations(self.path)
        config.run_migrations(self.path)
        self.assertEqual(self.path.read_bytes(), before)

    def test_user_off_survives_behind_the_flag(self):
        # the real round trip: migrated, user turned repeat off, restarts
        _write(self.path, {"version": 1, "playback": {"loop_file": "no"}})
        config.run_migrations(self.path)          # -> "inf" + flag
        stored = _read(self.path)
        stored["playback"]["loop_file"] = "no"    # user's explicit OFF
        _write(self.path, stored)
        for _ in range(3):
            config.run_migrations(self.path)
        self.assertEqual(_read(self.path)["playback"]["loop_file"], "no")

    # -- what must NOT be touched ------------------------------------------
    def test_fresh_install_writes_no_file(self):
        config.run_migrations(self.path)
        self.assertFalse(self.path.exists())

    def test_corrupt_file_left_alone(self):
        self.path.write_text("{ broken", encoding="utf-8")
        before = self.path.read_bytes()
        config.run_migrations(self.path)
        self.assertEqual(self.path.read_bytes(), before)

    def test_non_literal_values_untouched(self):
        for value in ("inf", "3", 3, True, None, "", "all"):
            with self.subTest(value=value):
                _write(self.path, {"version": 1,
                                   "playback": {"loop_file": value}})
                config.run_migrations(self.path)
                got = _read(self.path)
                self.assertEqual(got["playback"]["loop_file"], value)
                self.assertTrue(got["migrations"]["repeat_default_on"])

    def test_missing_playback_only_adds_flag(self):
        _write(self.path, {"version": 1, "ui": {"volume": 3}})
        config.run_migrations(self.path)
        got = _read(self.path)
        self.assertEqual(got["ui"], {"volume": 3})
        self.assertTrue(got["migrations"]["repeat_default_on"])
        self.assertNotIn("playback", got)

    def test_garbage_migrations_key_replaced(self):
        _write(self.path, {"version": 1, "migrations": "yes please",
                           "playback": {"loop_file": "no"}})
        config.run_migrations(self.path)
        got = _read(self.path)
        self.assertTrue(got["migrations"]["repeat_default_on"])
        self.assertEqual(got["playback"]["loop_file"], "inf")

    # -- schema surface -----------------------------------------------------
    def test_default_is_on(self):
        self.assertEqual(config.DEFAULTS["playback"]["loop_file"], "inf")
        self.assertEqual(config.DEFAULTS["playback"]["loop_playlist"], "no")
        self.assertTrue(
            config.DEFAULTS["migrations"]["repeat_default_on"])

    def test_migrations_is_not_a_settings_leaf(self):
        self.assertFalse([k for k in config.schema_leaf_keys()
                          if k.startswith("migrations")])

    def test_merged_defaults_say_migrated(self):
        cfg = config.Config(self.dir / "fresh.json")
        self.assertTrue(cfg.get("migrations.repeat_default_on"))
        self.assertEqual(cfg.get("playback.loop_file"), "inf")


if __name__ == "__main__":
    unittest.main()
