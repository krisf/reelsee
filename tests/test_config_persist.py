"""P4 gate: settings must survive a PROCESS restart (R-4 proof).

Two separate /usr/bin/python3 runs (this suite itself is executed by
/usr/bin/python3, so sys.executable is that interpreter): run 1 writes
mute+volume through the real Config API, run 2 reads them back from disk.
Unknown (non-schema) keys must survive the round trip unchanged.
"""
import _bootstrap  # noqa: F401  (adds src/ to sys.path)

import json
import os
import subprocess
import sys
import tempfile
import unittest

SRC = str(_bootstrap.SRC)

# The ambient TMPDIR may be read-only (sandbox); use the repo scratch dir.
TMP_ROOT = _bootstrap.REPO / "scratch" / "test-tmp"
TMP_ROOT.mkdir(parents=True, exist_ok=True)


def _tmpdir():
    return tempfile.TemporaryDirectory(dir=str(TMP_ROOT))

WRITE_CODE = f"""
import sys
sys.path.insert(0, {SRC!r})
from config import Config
c = Config()
c.set("audio.muted", False)
c.set("audio.volume", 63.5)
print("wrote")
"""

READ_CODE = f"""
import sys
sys.path.insert(0, {SRC!r})
from config import Config
c = Config()
print(repr([c.get("audio.muted"), c.get("audio.volume"),
            (c.data.get("custom") or {{}}).get("x")]))
"""


def _run(code: str, cfg_dir: str) -> str:
    env = dict(os.environ, REELSEE_CONFIG_DIR=cfg_dir,
               REELSEE_STATE_DIR=cfg_dir)
    r = subprocess.run([sys.executable, "-c", code], env=env,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, f"subprocess failed: {r.stderr}"
    return r.stdout.strip()


class RestartPersistence(unittest.TestCase):
    def test_mute_volume_survive_restart(self):
        with _tmpdir() as d:
            # seed file carries a user-defined key outside the schema
            with open(os.path.join(d, "config.json"), "w") as fh:
                json.dump({"custom": {"x": 1}}, fh)

            self.assertEqual(_run(WRITE_CODE, d), "wrote")
            got = _run(READ_CODE, d)
            self.assertEqual(got, repr([False, 63.5, 1]))

    def test_atomic_write_leaves_no_temp_files(self):
        with _tmpdir() as d:
            _run(WRITE_CODE, d)
            self.assertEqual(sorted(os.listdir(d)), ["config.json"])

    def test_mute_is_written_immediately_without_flush(self):
        # R-4: mute/volume hit the disk on the debounced timer even if the
        # process would exit right after (flush_now path).
        code = f"""
import sys
sys.path.insert(0, {SRC!r})
from config import Config
c = Config()
c.set("audio.muted", True)
# deliberately NO c.flush_now(): process exits ~immediately
"""
        with _tmpdir() as d:
            _run(code, d)
            data = json.load(open(os.path.join(d, "config.json")))
            self.assertTrue(data["audio"]["muted"])

    def test_corrupt_file_falls_back_to_defaults(self):
        with _tmpdir() as d:
            with open(os.path.join(d, "config.json"), "w") as fh:
                fh.write("{not json at all")
            got = _run(READ_CODE, d)
            self.assertEqual(got, repr([True, 100.0, None]))


if __name__ == "__main__":
    unittest.main()
