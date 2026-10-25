"""R4-P3: friendly libmpv failure — exit 2 + remedies + dialog when a
desktop is reachable; never a traceback (FINDINGS-INSTALL-CRASH P4: the
0.1.0 deb crashed straight into Apport on the user's machine)."""
import _bootstrap  # noqa: F401

import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import mpvlib as M
import reelsee


@contextlib.contextmanager
def env(**kw):
    old = {}
    for k, v in kw.items():
        old[k] = os.environ.get(k)
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    try:
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


SEARCHED = [(Path("/env/libmpv.so.2"), "env"),
            (Path("/install/usr/share/reelsee/vendor/usr/lib"
                  "/x86_64-linux-gnu/libmpv.so.2"), "vendored"),
            (Path("/usr/lib/x86_64-linux-gnu/libmpv.so.2"), "system")]


class ExitContract(unittest.TestCase):
    def _exit(self, err, **e):
        cap = io.StringIO()
        with contextlib.redirect_stderr(cap):
            rc = reelsee._libmpv_error_exit(err)
        return rc, cap.getvalue()

    def test_exit_code_is_2(self):
        self.assertEqual(reelsee.LIBMPV_FAIL_EXIT, 2)

    def test_headless_stderr_lists_paths_and_remedy_no_dialog(self):
        with env(REELSEE_HEADLESS="1", DISPLAY=":99",
                 WAYLAND_DISPLAY=None), \
                mock.patch.object(reelsee, "_show_libmpv_error_dialog") as dlg:
            rc, s = self._exit(M.LibmpvNotFound(SEARCHED))
        self.assertEqual(rc, 2)
        for p, _ in SEARCHED:
            self.assertIn(str(p), s)
        self.assertIn("libmpv2", s)
        self.assertIn("REELSEE_LIBMPV", s)
        self.assertNotIn("Traceback", s)
        dlg.assert_not_called()          # headless = no GUI, no hang

    def test_dialog_shown_on_x11_desktop(self):
        with env(REELSEE_HEADLESS=None, DISPLAY=":99",
                 WAYLAND_DISPLAY=None), \
                mock.patch.object(reelsee, "_show_libmpv_error_dialog") as dlg:
            rc, s = self._exit(M.LibmpvNotFound(SEARCHED))
        self.assertEqual(rc, 2)
        dlg.assert_called_once()
        body = dlg.call_args[0][0]
        self.assertIn("/usr/lib/x86_64-linux-gnu/libmpv.so.2", body)
        self.assertIn("REELSEE_LIBMPV", body)

    def test_wayland_counts_as_display(self):
        with env(REELSEE_HEADLESS=None, DISPLAY=None,
                 WAYLAND_DISPLAY="wayland-0"), \
                mock.patch.object(reelsee, "_show_libmpv_error_dialog") as dlg:
            rc, _ = self._exit(M.LibmpvNotFound(SEARCHED))
        self.assertEqual(rc, 2)
        dlg.assert_called_once()

    def test_no_display_means_no_dialog(self):
        with env(REELSEE_HEADLESS=None, DISPLAY=None,
                 WAYLAND_DISPLAY=None), \
                mock.patch.object(reelsee, "_show_libmpv_error_dialog") as dlg:
            rc, s = self._exit(M.LibmpvNotFound(SEARCHED))
        self.assertEqual(rc, 2)
        dlg.assert_not_called()
        self.assertIn("libmpv not found", s)

    def test_broken_dialog_still_exits_2(self):
        err = M.LibmpvNotFound(SEARCHED)
        with env(REELSEE_HEADLESS=None, DISPLAY=":99",
                 WAYLAND_DISPLAY=None), \
                mock.patch.object(reelsee, "_show_libmpv_error_dialog",
                                  side_effect=RuntimeError("no display")), \
                self.assertLogs("reelsee", level="WARNING") as logs:
            rc, s = self._exit(err)
        self.assertEqual(rc, 2)                       # dialog failure ≠ crash
        self.assertTrue(any("error dialog" in m for m in logs.output))

    def test_oserror_body_carries_remedy(self):
        err = OSError("libplacebo.so.324: cannot open shared object file")
        with env(REELSEE_HEADLESS="1", DISPLAY=None, WAYLAND_DISPLAY=None):
            rc, s = self._exit(err)
        self.assertEqual(rc, 2)
        self.assertIn("libmpv could not be loaded", s)
        self.assertIn("libplacebo.so.324", s)         # original cause kept
        self.assertIn("install libmpv2", s)
        self.assertIn("REELSEE_LIBMPV", s)
        self.assertNotIn("Traceback", s)


class RunIntegration(unittest.TestCase):
    """run() itself must RETURN 2 when the library is missing — the exact
    shape of the 0.1.0 launch crash, now handled."""

    def test_run_returns_2_not_traceback(self):
        with tempfile.TemporaryDirectory() as td:
            with env(REELSEE_HEADLESS="1", DISPLAY=None, WAYLAND_DISPLAY=None,
                     REELSEE_CONFIG_DIR=os.path.join(td, "cfg"),
                     REELSEE_STATE_DIR=os.path.join(td, "st")), \
                    mock.patch.object(M, "load_libmpv",
                                      side_effect=M.LibmpvNotFound(
                                          SEARCHED)), \
                    contextlib.redirect_stderr(io.StringIO()) as cap:
                rc = reelsee.run(["reelsee.py", "v.mp4"])
        self.assertEqual(rc, 2)
        s = cap.getvalue()
        self.assertNotIn("Traceback", s)
        self.assertIn("install libmpv2", s)
        self.assertIn("libmpv not found", s)


if __name__ == "__main__":
    unittest.main()
