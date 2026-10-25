"""Shared bootstrap for the unittest suite: put src/ on sys.path.

Run the suite with /usr/bin/python3 (the PATH python3 is a venv WITHOUT gi):
    LD_LIBRARY_PATH=vendor/usr/lib/x86_64-linux-gnu /usr/bin/python3 -m unittest discover tests
"""
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

# The ambient TMPDIR may be read-only (sandbox); use the repo scratch dir.
TMP_ROOT = REPO / "scratch" / "test-tmp"
TMP_ROOT.mkdir(parents=True, exist_ok=True)


def mini_cfg():
    """A real Config over a throwaway REELSEE_CONFIG_DIR (no display,
    no mpv) — the R6-B headless widget tests build real widgets on it."""
    os.environ.setdefault(
        "REELSEE_CONFIG_DIR",
        tempfile.mkdtemp(prefix="cfg-", dir=str(TMP_ROOT)))
    os.environ.setdefault(
        "REELSEE_STATE_DIR",
        tempfile.mkdtemp(prefix="state-", dir=str(TMP_ROOT)))
    import config
    return config.Config()


class _StubCore:
    def __init__(self):
        self.snapshot = {}
        self.commands = []
        self.props = []

    def command(self, *a):
        self.commands.append(a)

    def set_property(self, name, value):
        self.props.append((name, value))
        self.snapshot[name] = value


def mini_win():
    return SimpleNamespace(core=_StubCore())
