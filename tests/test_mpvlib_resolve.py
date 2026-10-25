"""R4-P1: libmpv candidate resolution (FINDINGS-INSTALL-CRASH P1/P2).

The installed crash was mpvlib knowing ONLY the dev-tree path
(<repo>/vendor/...), which resolves to /usr/share/vendor/... once the deb
installs the modules to /usr/share/reelsee/.  These tests import the REAL
production mpvlib.py file from simulated layouts — dev repo, package-adjacent
vendor, installed tree WITHOUT vendor (must pick the system libmpv2), and
missing-everything (must raise one clear error listing every path searched) —
and pin the LD_LIBRARY_PATH warning to VENDORED picks only.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import shutil
import sys
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

import _bootstrap  # noqa: F401  (src/ on sys.path)
import mpvlib

REPO_SRC_MPVLIB = Path(mpvlib.__file__).resolve()
REAL_SYSTEM_LIB = Path("/usr/lib/x86_64-linux-gnu/libmpv.so.2")

TMP = Path(__file__).resolve().parent / "tmp_mpvlib_resolve"


def fresh_mpvlib(location: Path, name: str):
    """Import the REAL mpvlib.py as if it lived in `location` (its REPO/HERE
    derive from __file__, exactly like the installed /usr/share/reelsee/)."""
    location.mkdir(parents=True, exist_ok=True)
    shutil.copy2(REPO_SRC_MPVLIB, location / "mpvlib.py")
    spec = importlib.util.spec_from_file_location(name, location / "mpvlib.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def mk_vendor_lib(base: Path, loadable: bool = False) -> Path:
    """Create base/usr/lib/x86_64-linux-gnu/libmpv.so.2 (empty marker, or a
    symlink to the real system library when the test actually CDLL-loads)."""
    d = base / mpvlib.VENDOR_REL
    d.mkdir(parents=True, exist_ok=True)
    p = d / "libmpv.so.2"
    if loadable and REAL_SYSTEM_LIB.exists():
        p.symlink_to(REAL_SYSTEM_LIB)
    else:
        p.write_bytes(b"")
    return p


@contextmanager
def env(**kw):
    """Set env vars for the block; value None REMOVES the var."""
    old = {k: os.environ.get(k) for k in kw}
    try:
        for k, v in kw.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@contextmanager
def neuter_system(mod, found=None):
    """Patch a fresh module so system discovery can only see `found`
    (None = nothing exists system-wide)."""
    with contextlib.ExitStack() as st:
        st.enter_context(mock.patch.object(mod, "SYSTEM_LIBDIRS", ()))
        st.enter_context(mock.patch.object(mod, "SYSTEM_PATHS", ()))
        st.enter_context(mock.patch("ctypes.util.find_library",
                                    return_value=found))
        yield


class ResolveTests(unittest.TestCase):
    """find_libmpv()/resolve_libmpv() candidate order on tmp layouts."""

    def setUp(self):
        shutil.rmtree(TMP, ignore_errors=True)
        TMP.mkdir(parents=True)
        self._n = 0

    def tearDown(self):
        shutil.rmtree(TMP, ignore_errors=True)

    def fresh(self, rel: str):
        self._n += 1
        return fresh_mpvlib(TMP / rel, f"mpvlib_sim_{self._n}")

    # ---------------------------------------------------------- candidate 1
    def test_env_override_beats_every_existing_candidate(self):
        env_lib = TMP / "mine" / "custom-libmpv.so.2"
        env_lib.parent.mkdir()
        env_lib.write_bytes(b"")
        loc = TMP / "pkg" / "usr" / "share" / "reelsee"
        mod = self.fresh(str(loc.relative_to(TMP)))
        pkg = mk_vendor_lib(loc)                          # candidate 2 exists
        repo = mk_vendor_lib(TMP / "pkg" / "usr" / "share")  # candidate 3
        sysfake = TMP / "syslib.so.2"
        sysfake.write_bytes(b"")
        with env(REELSEE_LIBMPV=str(env_lib)), neuter_system(mod, str(sysfake)):
            p, kind = mod.resolve_libmpv()
        self.assertEqual(p, env_lib)
        self.assertEqual(kind, "env")
        self.assertNotIn(str(pkg), str(p))
        self.assertNotIn(str(repo), str(p))

    # ---------------------------------------------------------- candidate 2
    def test_pkg_adjacent_vendor_wins_over_repo_and_system(self):
        loc = TMP / "pkg" / "usr" / "share" / "reelsee"
        mod = self.fresh(str(loc.relative_to(TMP)))
        pkg = mk_vendor_lib(loc)                              # package-adjacent
        mk_vendor_lib(TMP / "pkg" / "usr" / "share")          # dev-layout decoy
        sysfake = TMP / "syslib.so.2"
        sysfake.write_bytes(b"")
        with env(REELSEE_LIBMPV=None), \
                neuter_system(mod, str(sysfake)):
            p, kind = mod.resolve_libmpv()
        self.assertEqual(p, pkg)
        self.assertEqual(kind, "vendored")
        self.assertEqual(p, loc / "vendor" / "usr" / "lib"
                         / "x86_64-linux-gnu" / "libmpv.so.2")

    # ---------------------------------------------------------- candidate 3
    def test_dev_repo_layout_picks_repo_vendor(self):
        loc = TMP / "dev" / "src"
        mod = self.fresh(str(loc.relative_to(TMP)))
        repo = mk_vendor_lib(TMP / "dev")
        with env(REELSEE_LIBMPV=None, LD_LIBRARY_PATH=str(TMP / "dev"
                                                          / "vendor" / "usr"
                                                          / "lib"
                                                          / "x86_64-linux-gnu")), \
                neuter_system(mod, None):
            p, kind = mod.resolve_libmpv()
        self.assertEqual(p, repo)
        self.assertEqual(kind, "vendored")

    # ---------------------------------------------------------- candidate 4
    def test_install_tree_without_vendor_picks_system_via_find_library(self):
        """THE simulated /usr/share/reelsee install: NO vendor dir shipped —
        must fall through to the system library, not crash."""
        loc = TMP / "install" / "usr" / "share" / "reelsee"
        mod = self.fresh(str(loc.relative_to(TMP)))   # NO vendor dirs created
        sysfake = TMP / "sysroot" / "libmpv.so.2"
        sysfake.parent.mkdir()
        sysfake.write_bytes(b"")
        with env(REELSEE_LIBMPV=None, LD_LIBRARY_PATH=None), \
                neuter_system(mod, str(sysfake)):
            p, kind = mod.resolve_libmpv()
        self.assertEqual(p, sysfake)
        self.assertEqual(kind, "system")

    def test_find_library_soname_answer_resolves_via_system_dirs(self):
        """Debian's ctypes.util.find_library('mpv') returns the bare SONAME
        'libmpv.so.2' — resolution must map it onto the system dirs."""
        loc = TMP / "install" / "usr" / "share" / "reelsee"
        mod = self.fresh(str(loc.relative_to(TMP)))
        sysdir = TMP / "sysroot"
        sysdir.mkdir()
        (sysdir / "libmpv.so.2").write_bytes(b"")
        with env(REELSEE_LIBMPV=None, LD_LIBRARY_PATH=None), \
                mock.patch.object(mod, "SYSTEM_LIBDIRS", (str(sysdir),)), \
                mock.patch.object(mod, "SYSTEM_PATHS", ()), \
                mock.patch("ctypes.util.find_library",
                           return_value="libmpv.so.2"):
            p, kind = mod.resolve_libmpv()
        self.assertEqual(p, sysdir / "libmpv.so.2")
        self.assertEqual(kind, "system")

    @unittest.skipUnless(REAL_SYSTEM_LIB.exists(), "needs system libmpv2")
    def test_installed_layout_loads_real_system_libmpv(self):
        """End-to-end of the user's crash: mpvlib at /usr/share/reelsee-like
        path, NO vendor anywhere, clean env — loads the REAL libmpv2 and the
        LD_LIBRARY_PATH warning must NOT fire for a system pick."""
        loc = TMP / "usrshare" / "usr" / "share" / "reelsee"
        mod = self.fresh(str(loc.relative_to(TMP)))
        with env(REELSEE_LIBMPV=None, LD_LIBRARY_PATH=None):
            p, kind = mod.resolve_libmpv()
            self.assertEqual(p, REAL_SYSTEM_LIB)
            self.assertEqual(kind, "system")
            err = io.StringIO()
            with mock.patch("sys.stderr", err):
                lib = mod.load_libmpv()
            self.assertEqual(mod.client_api_version(lib), (2, 5))
            self.assertNotIn("WARNING", err.getvalue())
            self.assertIn("[system]", err.getvalue())

    # ------------------------------------------------------ missing-everything
    def test_missing_everything_raises_clear_error_listing_all_paths(self):
        loc = TMP / "install" / "usr" / "share" / "reelsee"
        mod = self.fresh(str(loc.relative_to(TMP)))
        bogus_env = TMP / "nope" / "libmpv.so.2"
        with env(REELSEE_LIBMPV=str(bogus_env), LD_LIBRARY_PATH=None), \
                neuter_system(mod, None):
            with self.assertRaises(mod.LibmpvNotFound) as cm:
                mod.find_libmpv()
            expected = [str(pth) for pth, _ in mod.libmpv_candidates()]
        err = cm.exception
        self.assertIsInstance(err, FileNotFoundError)
        msg = str(err)
        for want in (str(bogus_env),
                     str(loc / "vendor" / "usr" / "lib" / "x86_64-linux-gnu"
                         / "libmpv.so.2"),
                     str(TMP / "install" / "usr" / "share" / "vendor"
                         / "usr" / "lib" / "x86_64-linux-gnu"
                         / "libmpv.so.2")):
            self.assertIn(want, msg)
        self.assertIn("REELSEE_LIBMPV", msg)     # the remedy is named
        self.assertIn("libmpv2", msg)            # the remedy is named
        self.assertEqual(err.searched, expected)

    # ------------------------------------------------------------- warning --
    def test_warning_when_vendored_pick_off_ld_path(self):
        self.skipUnlessLoadable()
        loc = TMP / "pkg" / "usr" / "share" / "reelsee"
        mod = self.fresh(str(loc.relative_to(TMP)))
        mk_vendor_lib(loc, loadable=True)
        with env(REELSEE_LIBMPV=None, LD_LIBRARY_PATH=None), \
                neuter_system(mod, None):
            err = io.StringIO()
            with mock.patch("sys.stderr", err):
                mod.load_libmpv()
        text = err.getvalue()
        self.assertIn("WARNING", text)
        self.assertIn(str(loc / "vendor" / "usr" / "lib"
                          / "x86_64-linux-gnu"), text)
        self.assertIn("[vendored]", text)

    def test_no_warning_when_vendor_libdir_on_ld_path(self):
        self.skipUnlessLoadable()
        loc = TMP / "pkg" / "usr" / "share" / "reelsee"
        mod = self.fresh(str(loc.relative_to(TMP)))
        lib_file = mk_vendor_lib(loc, loadable=True)
        with env(REELSEE_LIBMPV=None,
                 LD_LIBRARY_PATH=str(lib_file.parent)), \
                neuter_system(mod, None):
            err = io.StringIO()
            with mock.patch("sys.stderr", err):
                mod.load_libmpv()
        self.assertNotIn("WARNING", err.getvalue())

    def skipUnlessLoadable(self):
        if not REAL_SYSTEM_LIB.exists():
            self.skipTest("needs the real libmpv2 to load via symlink")

    # ----------------------------------------------------------- explicit --
    def test_explicit_path_still_loads_and_missing_explicit_lists_it(self):
        self.skipUnlessLoadable()
        err = io.StringIO()
        with mock.patch("sys.stderr", err):
            lib = mpvlib.load_libmpv(str(REAL_SYSTEM_LIB))
        self.assertGreaterEqual(mpvlib.client_api_version(lib), (2, 0))
        self.assertIn("[explicit]", err.getvalue())
        with self.assertRaises(mpvlib.LibmpvNotFound) as cm:
            mpvlib.load_libmpv("/definitely/not/here/libmpv.so.2")
        self.assertIn("/definitely/not/here/libmpv.so.2", str(cm.exception))

    def test_explicit_dev_vendor_path_warns(self):
        """REELSEE_SMOKE-style explicit pin at the dev vendor file, without
        LD_LIBRARY_PATH, still gets the dep warning (it IS the vendored one)."""
        vendor_file = mpvlib.VENDOR_LIBDIR / "libmpv.so.2"
        if not vendor_file.exists():
            self.skipTest("dev vendor tree absent")
        err = io.StringIO()
        with env(LD_LIBRARY_PATH=None):
            with mock.patch("sys.stderr", err):
                try:
                    mpvlib.load_libmpv(str(vendor_file))
                except OSError:      # deps may genuinely fail to load; stderr first
                    pass
        self.assertIn("WARNING", err.getvalue())

    # ------------------------------------------------------------ real repo -
    def test_real_dev_repo_default_is_repo_vendor(self):
        """Unpatched production module in the dev checkout: default pick is
        the repo vendor dir (unchanged dev behaviour, now CANDIDATE 3)."""
        vendor_file = mpvlib.VENDOR_LIBDIR / "libmpv.so.2"
        if not vendor_file.exists():
            self.skipTest("dev vendor tree absent")
        with env(REELSEE_LIBMPV=None):
            p, kind = mpvlib.resolve_libmpv()
        self.assertEqual(p, vendor_file)
        self.assertEqual(kind, "vendored")

    # ------------------------------------------------------------ ordering --
    def test_candidate_order_and_dedupe_contract(self):
        loc = TMP / "pkg" / "usr" / "share" / "reelsee"
        mod = self.fresh(str(loc.relative_to(TMP)))
        envp = TMP / "env"
        envp.mkdir()
        (envp / "libmpv.so.2").write_bytes(b"")
        dup_sys = str(REAL_SYSTEM_LIB if REAL_SYSTEM_LIB.exists()
                      else TMP / "dup.so.2")
        with env(REELSEE_LIBMPV=str(envp / "libmpv.so.2")), \
                mock.patch.object(mod, "SYSTEM_LIBDIRS",
                                  (str(TMP / "sd"), str(TMP / "sd"))), \
                mock.patch.object(mod, "SYSTEM_PATHS", (dup_sys, dup_sys)), \
                mock.patch("ctypes.util.find_library", return_value=dup_sys):
            cands = mod.libmpv_candidates()
        kinds = [k for _p, k in cands]
        paths = [str(p) for p, _k in cands]
        self.assertEqual(kinds[0], "env")
        self.assertEqual(kinds[1:3], ["vendored", "vendored"])
        self.assertTrue(all(k == "system" for k in kinds[3:]))
        self.assertEqual(len(paths), len(set(paths)), "candidates deduped")
        self.assertEqual(paths[0], str(envp / "libmpv.so.2"))


if __name__ == "__main__":
    unittest.main()
