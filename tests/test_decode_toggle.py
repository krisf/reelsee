"""R7-A gate: decode indicator label mapping + bottom-bar placement.

Pure mapping (no display): every HW_REAL_ON member reads GPU, "no" reads
CPU, unknown/None reads the idle marker, CPU entered automatically reads
"CPU (auto)" and a user override outranks that marker.  Classification is
driven ONLY by the observed value: the same function signature has no way
to see the configured/requested mode, and the widget test proves a real
Chrome pinned to a GPU config with observed "no" still paints CPU.

Widget tree (headless GTK, same recipe as test_bottombar): the control is
the LAST child of the bottom bar (rightmost, after mute) with the exact
label/tooltip/a11y for the state.
"""
import _bootstrap  # noqa: F401  (sys.path for src/)
import unittest

from chrome import (BOTTOM_ORDER, Chrome, DECODE_LABELS, decode_state,
                    decode_tooltip)
import watchdog as WD


def child_labels(box):
    out = []
    w = box.get_first_child()
    while w is not None:
        out.append(w.get_name())
        w = w.get_next_sibling()
    return out


class TestClassifier(unittest.TestCase):
    """The indicator's ONLY source: watchdog.mode_base + HW_REAL_ON."""

    def test_hw_real_on_members_read_on(self):
        for v in sorted(WD.HW_REAL_ON):
            self.assertEqual(WD.decode_effective(v), "on", v)

    def test_codec_annotated_value_still_on(self):
        # observed values look like "vaapi(h264)" — mode_base strips them
        self.assertEqual(WD.decode_effective("vaapi(h264)"), "on")
        self.assertEqual(WD.decode_effective("vaapi-copy(hevc)"), "on")
        self.assertEqual(decode_state("vaapi(h264)"), "gpu")

    def test_no_reads_off(self):
        self.assertEqual(WD.decode_effective("no"), "off")
        self.assertEqual(WD.decode_effective("no "), "off")

    def test_missing_reads_unknown(self):
        self.assertEqual(WD.decode_effective(None), "unknown")
        self.assertEqual(WD.decode_effective(""), "unknown")
        self.assertEqual(WD.decode_effective("  "), "unknown")

    def test_HW_REAL_ON_is_the_engaged_set(self):
        self.assertIs(WD.HW_REAL_ON, WD.ENGAGED_MODES)


class TestLabelMapping(unittest.TestCase):
    def test_gpu_for_every_real_on_value(self):
        for v in sorted(WD.HW_REAL_ON):
            self.assertEqual(decode_state(v), "gpu", v)
            self.assertEqual(DECODE_LABELS["gpu"], "GPU")

    def test_cpu_when_off(self):
        self.assertEqual(decode_state("no"), "cpu")
        self.assertEqual(DECODE_LABELS["cpu"], "CPU")

    def test_idle_before_first_frame(self):
        for v in (None, "", "   "):
            self.assertEqual(decode_state(v), "idle", repr(v))
        self.assertEqual(DECODE_LABELS["idle"], "…")

    def test_cpu_auto_marker(self):
        self.assertEqual(decode_state("no", auto_cpu=True), "cpu_auto")
        self.assertEqual(DECODE_LABELS["cpu_auto"], "CPU (auto)")
        # user override outranks the auto marker — plain CPU
        self.assertEqual(decode_state("no", user_cpu=True, auto_cpu=True),
                         "cpu")
        # a USER choice never reads "(auto)"
        self.assertEqual(decode_state("no", user_cpu=True), "cpu")
        # GPU observed: who "chose" CPU is irrelevant — real-on wins
        for v in sorted(WD.HW_REAL_ON):
            self.assertEqual(decode_state(v, auto_cpu=True), "gpu", v)

    def test_never_reads_configured_mode(self):
        # The mapping takes ONLY the observed value (+ who chose CPU):
        # a pinned-GPU CONFIG with observed "no" must still read CPU.
        import inspect
        params = list(inspect.signature(decode_state).parameters)
        self.assertEqual(params[0], "hwdec_current")
        for p in params:
            self.assertNotIn("config", p)
            self.assertNotIn("decoder", p)
            self.assertNotIn("mode", p)


class TestTooltip(unittest.TestCase):
    def test_names_observed_value(self):
        t = decode_tooltip("gpu", "vaapi-copy")
        self.assertIn("hwdec-current: vaapi-copy", t)
        self.assertIn("click", t.lower())

    def test_cpu_auto_says_fallback(self):
        t = decode_tooltip("cpu_auto", "no")
        self.assertIn("hwdec-current: no", t)
        self.assertIn("automatic", t.lower())
        self.assertIn("configured", t.lower())   # what the click will do

    def test_missing_value_named_honestly(self):
        t = decode_tooltip("idle", None)
        self.assertIn("not observed yet", t)


class TestWidget(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import gi
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk
        cls.Gtk = Gtk
        cls.cfg = _bootstrap.mini_cfg()
        cls.win = _bootstrap.mini_win()
        cls.bar = Chrome(cls.win, cls.cfg)

    def test_control_is_rightmost_after_mute(self):
        names = child_labels(self.bar.bottom)
        self.assertEqual(names, list(BOTTOM_ORDER))
        self.assertEqual(names[-1], "decode")
        self.assertEqual(names[-2], "mute")
        self.assertIsInstance(self.bar.btn_decode, self.Gtk.Button)

    def test_label_tracks_observed_snapshot(self):
        snap = {"pause": False, "idle-active": False, "eof-reached": False}
        self.bar.update_snapshot(dict(snap))
        self.assertEqual(self.bar.btn_decode.get_label(),
                         DECODE_LABELS["idle"])
        self.bar.update_snapshot(dict(snap, **{"hwdec-current": "no"}))
        self.assertEqual(self.bar.btn_decode.get_label(), "CPU")
        self.bar.update_snapshot(dict(snap, **{"hwdec-current":
                                               "vaapi-copy"}))
        self.assertEqual(self.bar.btn_decode.get_label(), "GPU")
        self.bar.update_snapshot(dict(snap, **{"hwdec-current":
                                               "vaapi(h264)"}))
        self.assertEqual(self.bar.btn_decode.get_label(), "GPU")

    def test_pinned_gpu_config_never_claims_gpu(self):
        # config requests GPU (engine.decoder pinned) — observed "no"
        # (silent engagement failure) must still paint CPU.
        self.bar.cfg.set("engine.decoder", "0000:13:00.0")
        try:
            self.bar.update_snapshot({"pause": False, "idle-active": False,
                                      "eof-reached": False,
                                      "hwdec-current": "no"})
            self.assertEqual(self.bar.btn_decode.get_label(), "CPU")
        finally:
            self.bar.cfg.set("engine.decoder", "auto")

    def test_dim_class_cpu_only(self):
        base = {"pause": False, "idle-active": False, "eof-reached": False}
        self.bar.update_snapshot(dict(base, **{"hwdec-current": "no"}))
        self.assertTrue(self.bar.btn_decode.has_css_class(
            "reelsee-decode-cpu"))
        self.bar.update_snapshot(dict(base, **{"hwdec-current":
                                               "vaapi-copy"}))
        self.assertFalse(self.bar.btn_decode.has_css_class(
            "reelsee-decode-cpu"))

    def test_flags_repaint_user_vs_auto(self):
        base = {"pause": False, "idle-active": False, "eof-reached": False,
                "hwdec-current": "no"}
        self.bar.set_decode_flags(False, True)     # automatic fallback
        self.bar.update_snapshot(base)
        self.assertEqual(self.bar.btn_decode.get_label(), "CPU (auto)")
        self.bar.set_decode_flags(True, False)     # user toggle
        self.assertEqual(self.bar.btn_decode.get_label(), "CPU")
        self.bar.set_decode_flags(False, False)
        self.bar.update_snapshot(base)
        self.assertEqual(self.bar.btn_decode.get_label(), "CPU")

    def test_tooltip_a11y_track_state(self):
        seen = []
        orig = self.bar.btn_decode.update_property

        def spy(props, values):
            seen.append((list(props), list(values)))
            return orig(props, values)
        self.bar.btn_decode.update_property = spy
        try:
            self.bar.update_snapshot({"pause": False, "idle-active": False,
                                      "eof-reached": False,
                                      "hwdec-current": "vaapi-copy"})
            self.bar.update_snapshot({"pause": False, "idle-active": False,
                                      "eof-reached": False,
                                      "hwdec-current": "no"})
        finally:
            del self.bar.btn_decode.update_property
        label = self.Gtk.AccessibleProperty.LABEL
        self.assertTrue(any(l == [label] and "GPU" in v[0]
                            for l, v in seen), str(seen))
        self.assertTrue(any(l == [label] and "CPU decode" in v[0]
                            for l, v in seen), str(seen))
        tip = str(self.bar.btn_decode.get_tooltip_text())
        self.assertIn("hwdec-current: no", tip)
        self.assertIn("configured", tip.lower())


if __name__ == "__main__":
    unittest.main()
