"""P9 gate lint #2: no synchronous mpv calls off the sanctioned zones (R9).

The GTK main thread NEVER calls a synchronous mpv getter: reads flow
through mpv_observe_property → Snapshot, writes through
mpv_command_async / mpv_set_property_async.  The only sync entry points
are the two worker-thread wrappers in mpvlib/mpvcore, each header-
pragma'd `SYNC-MPV`; every other call to a blocking libmpv symbol is a
lint violation.  Async variants are always allowed.
"""
import _bootstrap  # noqa: F401

import ast
import tempfile
import unittest
from pathlib import Path

from _bootstrap import SRC

SYNC_MPV_ATTRS = {
    "mpv_get_property", "mpv_get_property_string", "mpv_get_property_flag",
    "mpv_get_property_osd_string", "mpv_get_property_node",
    "mpv_command", "mpv_set_property", "mpv_set_property_string",
    "mpv_set_property_flag",
    # sync wrappers (worker-thread-only by contract)
    "get_property_string", "get_property_string_sync",
}
PRAGMA = "SYNC-MPV"


def _sync_mpv_call(node) -> str | None:
    if not isinstance(node, ast.Call):
        return None
    fn = node.func
    if isinstance(fn, ast.Attribute) and fn.attr in SYNC_MPV_ATTRS:
        return fn.attr
    if isinstance(fn, ast.Name) and fn.id in SYNC_MPV_ATTRS:
        return fn.id
    return None


def scan_source(src: str, name: str = "<src>"):
    lines = src.splitlines()
    tree = ast.parse(src)
    scopes = [(n.lineno, n.end_lineno) for n in ast.walk(tree)
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef,
                                ast.ClassDef))
              and PRAGMA in lines[n.lineno - 1]]
    found = []
    for node in ast.walk(tree):
        code = _sync_mpv_call(node)
        if code is None:
            continue
        ln = node.lineno
        text = lines[ln - 1].strip()
        if PRAGMA in text:
            continue
        if any(a <= ln <= b for a, b in scopes):
            continue
        found.append((ln, code, text))
    return sorted(found)


class LintGreenOverSrc(unittest.TestCase):
    def test_all_src_clean(self):
        offenders = []
        for p in sorted(SRC.glob("*.py")):
            for ln, code, text in scan_source(p.read_text(), p.name):
                offenders.append(f"{p.name}:{ln} {code}: {text}")
        self.assertEqual(offenders, [],
                         "sync mpv call outside SYNC-MPV zone:\n"
                         + "\n".join(offenders))

    def test_sync_wrappers_are_explicitly_zoned(self):
        # the two sanctioned wrappers exist AND carry their pragma
        lib = (SRC / "mpvlib.py").read_text()
        core = (SRC / "mpvcore.py").read_text()
        self.assertRegex(lib, r"def get_property_string\(.*SYNC-MPV")
        self.assertRegex(core,
                         r"def get_property_string_sync\(.*SYNC-MPV")

    def test_async_variants_not_banned(self):
        # sanity: async spellings must never be flagged
        v = scan_source("def f():\n    return lib.mpv_command_async(h, a)\n")
        self.assertEqual(v, [])


class LintActuallyLints(unittest.TestCase):
    def _scan(self, src):
        with tempfile.TemporaryDirectory(
                dir=str(SRC.parent / "scratch")) as td:
            f = Path(td) / "sample.py"
            f.write_text(src)
            return scan_source(f.read_text(), "sample.py")

    def test_flags_sync_getter(self):
        v = self._scan("def ui():\n"
                       "    return lib.mpv_get_property_string(h, b'duration')\n")
        self.assertEqual([c for _, c, _ in v], ["mpv_get_property_string"])

    def test_flags_sync_command(self):
        v = self._scan("def ui():\n    lib.mpv_command(h, args)\n")
        self.assertEqual([c for _, c, _ in v], ["mpv_command"])

    def test_flags_wrapper_call(self):
        v = self._scan("import mpvlib\n"
                       "def ui():\n    mpvlib.get_property_string(l, h, 'x')\n")
        self.assertEqual([c for _, c, _ in v], ["get_property_string"])

    def test_zone_pragma_allows(self):
        v = self._scan("def worker():  # SYNC-MPV\n"
                       "    lib.mpv_get_property_string(h, b'x')\n")
        self.assertEqual(v, [])


if __name__ == "__main__":
    unittest.main()
