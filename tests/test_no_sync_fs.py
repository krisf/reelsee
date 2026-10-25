"""P9 gate lint #1: no synchronous filesystem calls in src/ (R-P9).

Static AST scan.  Sanctioned call sites are marked with a `SYNC-FS-OK`
pragma on the call line or on any enclosing def/class header (startup
config load, atomic config save, sysfs engine enumeration, input-conf
read — all documented in DSH-REPORT).  The showcase async module
`reelsee_io.py` must be CLEAN with zero pragmas, and the
REELSEE_FAKE_LATENCY_MS test hook may exist in reelsee_io.py only.
"""
import _bootstrap  # noqa: F401

import ast
import tempfile
import unittest
from pathlib import Path

from _bootstrap import SRC

BANNED_OS = {"listdir", "scandir", "walk", "remove", "unlink", "rename",
             "replace", "makedirs", "mkdir", "stat", "rmdir"}
BANNED_OSPATH = {"exists", "isdir", "isfile", "getsize"}
BANNED_PATHM = {"read_text", "write_text", "iterdir", "glob", "mkdir",
                "unlink", "rmdir", "touch"}
PRAGMA = "SYNC-FS-OK"


def _banned_call(node) -> str | None:
    if not isinstance(node, ast.Call):
        return None
    fn = node.func
    if isinstance(fn, ast.Name) and fn.id in ("open", "io.open"):
        return fn.id
    if isinstance(fn, ast.Attribute):
        b = fn.value
        if isinstance(b, ast.Name) and b.id == "os" and fn.attr in BANNED_OS:
            return f"os.{fn.attr}"
        if (isinstance(b, ast.Attribute) and isinstance(b.value, ast.Name)
                and b.value.id == "os" and b.attr == "path"
                and fn.attr in BANNED_OSPATH):
            return f"os.path.{fn.attr}"
        if isinstance(b, ast.Name) and b.id in ("glob", "shutil"):
            return f"{b.id}.{fn.attr}"
        if fn.attr in BANNED_PATHM and isinstance(b, (ast.Name, ast.Call)):
            return f"pathlib.{fn.attr}"
    return None


def scan_source(src: str, name: str = "<src>"):
    """Return [(lineno, code, text)] violations for one source string."""
    lines = src.splitlines()
    tree = ast.parse(src)
    # enclosing sanctioned scopes: every def/class header carrying a pragma
    scopes = [(n.lineno, n.end_lineno) for n in ast.walk(tree)
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef,
                                ast.ClassDef))
              and PRAGMA in lines[n.lineno - 1]]
    found = []
    for node in ast.walk(tree):
        code = _banned_call(node)
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


def src_files():
    return sorted(SRC.glob("*.py"))


class LintGreenOverSrc(unittest.TestCase):
    def test_all_src_clean(self):
        offenders = []
        for p in src_files():
            for ln, code, text in scan_source(p.read_text(), p.name):
                offenders.append(f"{p.name}:{ln} {code}: {text}")
        self.assertEqual(offenders, [],
                         "unsanctioned sync fs:\n" + "\n".join(offenders))

    def test_reelsee_io_is_pragma_free(self):
        io_src = (SRC / "reelsee_io.py").read_text()
        self.assertNotIn(PRAGMA, io_src)
        self.assertEqual(scan_source(io_src, "reelsee_io.py"), [])
        # async APIs genuinely used (anti-stub guard)
        for api in ("enumerate_children_async", "next_files_async",
                    "monitor("):
            self.assertIn(api, io_src)

    def test_fake_latency_hook_locality(self):
        users = [p.name for p in src_files()
                 if "REELSEE_FAKE_LATENCY_MS" in p.read_text()]
        self.assertEqual(users, ["reelsee_io.py"])


class LintActuallyLints(unittest.TestCase):
    """Negative controls — a lint that flags nothing gates nothing."""

    def _scan(self, src):
        with tempfile.TemporaryDirectory(
                dir=str(SRC.parent / "scratch")) as td:
            f = Path(td) / "sample.py"
            f.write_text(src)
            return scan_source(f.read_text(), "sample.py")

    def test_flags_bare_open(self):
        v = self._scan("def f():\n    return open('x')\n")
        self.assertEqual([c for _, c, _ in v], ["open"])

    def test_flags_os_listdir_and_glob(self):
        v = self._scan("import os, glob\n"
                       "def f():\n"
                       "    a = os.listdir('/tmp')\n"
                       "    b = glob.glob('*.mp4')\n"
                       "    return a, b, os.path.exists('/x')\n")
        self.assertEqual([c for _, c, _ in v],
                         ["os.listdir", "glob.glob", "os.path.exists"])

    def test_def_pragma_sanctions_whole_body(self):
        v = self._scan(f"def f():  # {PRAGMA}\n"
                       "    import os\n"
                       "    return os.listdir('/tmp')\n")
        self.assertEqual(v, [])

    def test_class_pragma_covers_methods(self):
        v = self._scan(f"class C:  # {PRAGMA}\n"
                       "    def m(self):\n"
                       "        import os\n"
                       "        os.remove('x')\n")
        self.assertEqual(v, [])

    def test_call_line_pragma(self):
        v = self._scan(f"import os\n"
                       f"def f():\n"
                       f"    os.remove('x')  # {PRAGMA}\n")
        self.assertEqual(v, [])


if __name__ == "__main__":
    unittest.main()
