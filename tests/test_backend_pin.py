"""ROUND 24 §B.1 gates: `src/backendpin.py` — the app-side GDK backend pin.

The measured cause (references/ROUND24-BRIEF.md §0): the restore code is
correct — an X11 client lands exactly on the saved rect — but the user's
launch paths (`gtk-launch` on a shadowing user-level desktop entry, and
`/usr/bin/reelsee` from a terminal) produce a NATIVE WAYLAND client, where
positioning is impossible by protocol and Mutter centres the window.  The
durable fix is in the application: it decides its own backend before GDK
opens a display.

Every row of the §A decision table is asserted here, plus the two traps the
brief calls out: an EMPTY-STRING `GDK_BACKEND` counts as unset (an env var
set to "" is not a user choice), and the X-socket test is a seam — no test
in this file touches the real filesystem.

RED on the pre-R24 tree: `src/backendpin.py` does not exist, so this file
fails at import (that is the §B red recorded in DSH-REPORT-R24.md).
"""
from __future__ import annotations

import logging
import os
import tempfile
import unittest
from unittest import mock

import _bootstrap  # noqa: F401  (sys.path + writable TMP_ROOT)

import backendpin
from backendpin import apply_pin, pin_decision

# The operator's real launch environment (§0): a Wayland session AND an
# XWayland DISPLAY, which is exactly where the pin matters.
BASE = {"DISPLAY": ":0", "WAYLAND_DISPLAY": "wayland-0"}


class RecLogger:
    """Logger stand-in that records the lines at each level."""

    def __init__(self):
        self.infos: list[str] = []
        self.warnings: list[str] = []
        self.debugs: list[str] = []

    def info(self, msg, *a):
        self.infos.append(msg % a if a else str(msg))

    def warning(self, msg, *a):
        self.warnings.append(msg % a if a else str(msg))

    def debug(self, msg, *a):
        self.debugs.append(msg % a if a else str(msg))

    def exception(self, msg, *a):
        self.warnings.append("EXC " + (msg % a if a else str(msg)))

    def log(self, level, msg, *a):
        (self.infos if level >= logging.INFO else self.debugs).append(
            msg % a if a else str(msg))


def socketed(seam=True):
    """Patch the module's X-socket seam (never the real filesystem)."""
    return mock.patch.object(backendpin, "_socket_exists", lambda d: seam)


class Row1ExplicitEnv(unittest.TestCase):
    """§A row 1: a user/env GDK_BACKEND always wins, even over pref=x11."""

    def test_explicit_backend_wins_over_pref(self):
        self.assertEqual(pin_decision(dict(BASE, GDK_BACKEND="wayland"),
                                       "x11"),
                         (None, "explicit-env"))

    def test_explicit_backend_wins_over_xwayland(self):
        with socketed(True):
            self.assertEqual(pin_decision(dict(BASE, GDK_BACKEND="wayland")),
                             (None, "explicit-env"))

    def test_explicit_backend_value_is_untouched(self):
        env = dict(BASE, GDK_BACKEND="broadway")
        with socketed(True):
            self.assertIsNone(apply_pin(env=env, logger=RecLogger()))
        self.assertEqual(env["GDK_BACKEND"], "broadway")
        self.assertNotIn("REELSEE_BACKEND_PINNED", env)


class Row1EmptyIsUnset(unittest.TestCase):
    """§B.1: an empty-string GDK_BACKEND counts as UNSET — "" is not a
    choice, and treating it as one silently disables the pin."""

    def test_empty_backend_counts_as_unset_and_is_replaced(self):
        env = dict(BASE, GDK_BACKEND="")
        with socketed(True):
            self.assertEqual(pin_decision(env), ("x11", "xwayland"))
            self.assertEqual(apply_pin(env=env, logger=RecLogger()), "x11")
        self.assertEqual(env["GDK_BACKEND"], "x11")

    def test_empty_backend_with_empty_pin_still_decides(self):
        env = dict(BASE, GDK_BACKEND="", REELSEE_BACKEND_PIN="")
        with socketed(True):
            self.assertEqual(pin_decision(env), ("x11", "xwayland"))


class Row2PinEscapeHatch(unittest.TestCase):
    """§A row 2: REELSEE_BACKEND_PIN (any value) is the escape hatch — the
    app never pins when the operator asked to drive the backend themselves."""

    def test_any_value_never_pins(self):
        for val in ("x11", "wayland", "off", "0", "nonsense", "true"):
            with self.subTest(val=val):
                with socketed(True):
                    self.assertEqual(
                        pin_decision(dict(BASE, REELSEE_BACKEND_PIN=val)),
                        (None, "explicit-pin-env"))

    def test_beats_pref_x11(self):
        with socketed(True):
            self.assertEqual(
                pin_decision(dict(BASE, REELSEE_BACKEND_PIN="no"), "x11"),
                (None, "explicit-pin-env"))

    def test_applier_sets_nothing(self):
        env = dict(BASE, REELSEE_BACKEND_PIN="off")
        with socketed(True):
            self.assertIsNone(apply_pin(env=env, logger=RecLogger()))
        self.assertNotIn("GDK_BACKEND", env)
        self.assertNotIn("REELSEE_BACKEND_PINNED", env)

    def test_empty_pin_is_not_an_escape_hatch(self):
        env = dict(BASE, REELSEE_BACKEND_PIN="")
        with socketed(True):
            self.assertEqual(pin_decision(env), ("x11", "xwayland"))


class Row34Pref(unittest.TestCase):
    """§A rows 3/4: the config key ui.x11_backend (auto|x11|wayland)."""

    def test_pref_wayland_never_pins(self):
        for pref in ("wayland", "Wayland", "  WAYLAND  ", "wayland\n"):
            with self.subTest(pref=pref):
                with socketed(True):
                    self.assertEqual(pin_decision(dict(BASE), pref),
                                     (None, "pref-wayland"))

    def test_pref_wayland_wins_over_xwayland_row(self):
        with socketed(True):
            self.assertEqual(pin_decision(dict(BASE), "wayland"),
                             (None, "pref-wayland"))

    def test_pref_x11_pins(self):
        for pref in ("x11", "X11", " x11 ", "X11\n"):
            with self.subTest(pref=pref):
                with socketed(True):
                    self.assertEqual(pin_decision(dict(BASE), pref),
                                     ("x11", "pref-x11"))

    def test_pref_x11_pins_without_wayland(self):
        # The preference is a directive, not a probe: no WAYLAND_DISPLAY
        # needed, and the socket seam is not consulted at all.
        calls: list[str] = []
        with mock.patch.object(backendpin, "_socket_exists",
                               lambda d: calls.append(d) or True):
            self.assertEqual(pin_decision({"DISPLAY": ":0"}, "x11"),
                             ("x11", "pref-x11"))
        self.assertEqual(calls, [])

    def test_invalid_pref_is_auto(self):
        for pref in ("watson", "", None, "11", "x11x", "auto"):
            with self.subTest(pref=pref):
                with socketed(True):
                    self.assertEqual(pin_decision(dict(BASE), pref),
                                     ("x11", "xwayland"))

    def test_applier_pref_x11(self):
        env = {"DISPLAY": ":0"}
        self.assertEqual(apply_pin(env=env, pref="x11",
                                   logger=RecLogger()), "x11")
        self.assertEqual(env["GDK_BACKEND"], "x11")


class Row56XWayland(unittest.TestCase):
    """§A rows 5/6: auto-detect — pin x11 only when XWayland is REAL."""

    def test_socket_present_pins(self):
        with socketed(True):
            self.assertEqual(pin_decision(dict(BASE)), ("x11", "xwayland"))

    def test_socket_missing_does_not_pin(self):
        """Fail-closed: a DISPLAY whose socket we cannot confirm is NOT a
        backend we may force (pinning a dead backend = a window that never
        opens)."""
        with socketed(False):
            self.assertEqual(pin_decision(dict(BASE)),
                             (None, "no-xwayland-socket"))

    def test_no_wayland_display_is_not_applicable(self):
        with socketed(True):
            self.assertEqual(pin_decision({"DISPLAY": ":0"}),
                             (None, "not-applicable"))

    def test_no_display_is_not_applicable(self):
        with socketed(True):
            self.assertEqual(pin_decision({"WAYLAND_DISPLAY": "wayland-0"}),
                             (None, "not-applicable"))

    def test_empty_wayland_display_is_not_applicable(self):
        with socketed(True):
            self.assertEqual(
                pin_decision({"DISPLAY": ":0", "WAYLAND_DISPLAY": ""}),
                (None, "not-applicable"))

    def test_empty_display_is_not_applicable(self):
        with socketed(True):
            self.assertEqual(
                pin_decision({"DISPLAY": "", "WAYLAND_DISPLAY": "wayland-0"}),
                (None, "not-applicable"))

    def test_socket_seam_receives_the_display(self):
        seen = []
        with mock.patch.object(backendpin, "_socket_exists",
                               lambda d: seen.append(d) or True):
            pin_decision(dict(BASE))
        self.assertEqual(seen, [":0"])


class SocketSeam(unittest.TestCase):
    """§A: `:N` and `:N.S` map to /tmp/.X11-unix/X<N>; anything else is
    "socket missing" (fail-closed).  os.path.exists is patched — the real
    filesystem is never consulted."""

    def _exists(self, present, seen):
        def fake(path):
            seen.append(path)
            return present
        return fake

    def test_display_n(self):
        seen: list[str] = []
        with mock.patch.object(os.path, "exists", self._exists(True, seen)):
            self.assertTrue(backendpin._socket_exists(":0"))
        self.assertEqual(seen, ["/tmp/.X11-unix/X0"])

    def test_display_n_screen(self):
        seen: list[str] = []
        with mock.patch.object(os.path, "exists", self._exists(True, seen)):
            self.assertTrue(backendpin._socket_exists(":1.2"))
        self.assertEqual(seen, ["/tmp/.X11-unix/X1"])

    def test_multi_digit_display(self):
        seen: list[str] = []
        with mock.patch.object(os.path, "exists", self._exists(True, seen)):
            self.assertTrue(backendpin._socket_exists(":12"))
        self.assertEqual(seen, ["/tmp/.X11-unix/X12"])

    def test_missing_socket_is_false(self):
        with mock.patch.object(os.path, "exists", self._exists(False, [])):
            self.assertFalse(backendpin._socket_exists(":0"))

    def test_unparseable_forms_are_false_without_probing(self):
        for disp in ("host:0", "abstract:foo", "unix:/tmp/x", "0", "", None,
                     ":abc", ":0.0.0", ":0 ", " :0"):
            with self.subTest(disp=disp):
                with mock.patch.object(os.path, "exists",
                                       lambda p: self.fail(f"probed {p}")):
                    self.assertFalse(backendpin._socket_exists(disp))


class Applier(unittest.TestCase):
    """§A: the applier mutates os.environ only, logs ONE INFO line naming
    the reason token, is idempotent, and never raises."""

    def test_sets_backend_and_marker(self):
        env = dict(BASE)
        rec = RecLogger()
        with socketed(True):
            self.assertEqual(apply_pin(env=env, logger=rec), "x11")
        self.assertEqual(env["GDK_BACKEND"], "x11")
        self.assertEqual(env["REELSEE_BACKEND_PINNED"], "x11")
        self.assertEqual(len(rec.infos), 1, rec.infos)
        self.assertIn("xwayland", rec.infos[0])

    def test_declined_still_logs_one_info_with_reason(self):
        for case, token in (
                (dict(BASE, GDK_BACKEND="wayland"), "explicit-env"),
                (dict(BASE, REELSEE_BACKEND_PIN="off"), "explicit-pin-env"),
                ({"DISPLAY": ":0"}, "not-applicable")):
            with self.subTest(token=token):
                env = dict(case)
                before = dict(env)
                rec = RecLogger()
                with socketed(False):
                    self.assertIsNone(apply_pin(env=env, logger=rec))
                self.assertEqual(len(rec.infos), 1, rec.infos)
                self.assertIn(token, rec.infos[0])
                # A declined decision writes NOTHING — not even the marker.
                self.assertEqual({k: v for k, v in env.items()
                                  if k != backendpin.ENV_PINNED}, before)
                self.assertNotIn("REELSEE_BACKEND_PINNED", env)

    def test_idempotent_second_call_is_a_noop(self):
        env = dict(BASE)
        rec = RecLogger()
        with socketed(True):
            self.assertEqual(apply_pin(env=env, logger=rec), "x11")
            self.assertEqual(apply_pin(env=env, logger=rec), None)
            self.assertEqual(apply_pin(env=env, logger=rec), None)
        self.assertEqual(env["GDK_BACKEND"], "x11")
        self.assertEqual(env["REELSEE_BACKEND_PINNED"], "x11")

    def test_never_raises_on_read_only_env(self):
        """§B.1: a dict-like that refuses writes must not take the boot down."""
        class ReadOnly(dict):
            def __setitem__(self, key, value):
                raise OSError(30, "read-only file system")

        env = ReadOnly(BASE)
        rec = RecLogger()
        with socketed(True):
            self.assertIsNone(apply_pin(env=env, logger=rec))
        self.assertEqual(len(rec.warnings), 1, rec.warnings)
        self.assertNotIn("GDK_BACKEND", env)

    def test_never_raises_on_broken_env(self):
        """Even reading the environment may fail (a hostile mapping); the
        answer is "do not pin", never an exception."""
        class Exploding(dict):
            def get(self, key, default=None):
                raise RuntimeError("no env for you")

        rec = RecLogger()
        with socketed(True):
            self.assertIsNone(apply_pin(env=Exploding(), logger=rec))
        self.assertTrue(rec.warnings, "the failure must be logged")

    def test_never_raises_when_the_socket_probe_explodes(self):
        env = dict(BASE)
        rec = RecLogger()
        with mock.patch.object(backendpin, "_socket_exists",
                               lambda d: (_ for _ in ()).throw(
                                   OSError("stat failed"))):
            self.assertIsNone(apply_pin(env=env, logger=rec))
        self.assertTrue(rec.warnings, "the failure must be logged")
        self.assertNotIn("GDK_BACKEND", env)

    def test_uses_os_environ_when_none_given(self):
        env = dict(BASE)
        with mock.patch.dict(os.environ, env, clear=True):
            with socketed(True):
                self.assertEqual(apply_pin(logger=RecLogger()), "x11")
            self.assertEqual(os.environ["GDK_BACKEND"], "x11")
            self.assertEqual(os.environ["REELSEE_BACKEND_PINNED"], "x11")
        self.assertNotIn("GDK_BACKEND", os.environ)

    def test_default_logger_is_the_module_logger(self):
        """The operator greps the app log, so the line must land on
        reelsee.backendpin at INFO (not DEBUG: the whole point is that the
        pin is visible in a normal launch)."""
        rec = RecLogger()
        real = backendpin.log
        backendpin.log = rec
        try:
            with socketed(True):
                apply_pin(env=dict(BASE))
        finally:
            backendpin.log = real
        self.assertEqual(len(rec.infos), 1, rec.infos)
        self.assertIn("xwayland", rec.infos[0])


class ConfigKey(unittest.TestCase):
    """§A: ui.x11_backend — new key, default "auto", no migration."""

    def test_default_is_auto(self):
        import config
        self.assertEqual(config.DEFAULTS["ui"]["x11_backend"], "auto")

    def test_fresh_config_reads_auto(self):
        import config
        with tempfile.TemporaryDirectory(prefix="cfg-r24-",
                                         dir=str(_bootstrap.TMP_ROOT)) as d:
            with mock.patch.dict(os.environ, {"REELSEE_CONFIG_DIR": d}):
                cfg = config.Config()
                self.assertEqual(cfg.get("ui.x11_backend"), "auto")

    def test_unknown_key_is_not_invented(self):
        """The pin reads ONE key; a typo must not silently read another."""
        import config
        self.assertNotIn("x11backend", config.DEFAULTS["ui"])
        self.assertNotIn("backend", config.DEFAULTS["ui"])


class NoDisplayImport(unittest.TestCase):
    """§A: the module is pure — importable with no display and no gi."""

    def test_no_gi_import(self):
        import sys
        self.assertNotIn("gi", sys.modules.get(
            "backendpin", sys.modules["__main__"]).__dict__.get("_gi", {}))
        self.assertFalse(any(m.startswith("gi.") for m in
                             getattr(backendpin, "_GI_MODULES", ())))

    def test_pure_functions_take_an_env(self):
        # No os.environ mutation from the decision function.
        before = dict(os.environ)
        with socketed(True):
            pin_decision(dict(BASE))
        self.assertEqual(dict(os.environ), before)


if __name__ == "__main__":
    unittest.main()
