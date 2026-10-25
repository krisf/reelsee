"""R6-B: bottom-bar contract — order, play/pause first, NO stop, mute
beside volume, icon mapping from the observed snapshot.  Headless:
Gio/Gtk widgets and pure data build fine with no display.

Gate coverage (brief §B/§C): the exact left->right order
play/pause, prev, next, A, B, position, seek, duration, repeat, speed,
volume, mute (index assertion); the stop button is absent from the spec
AND from a real widget tree; play/pause + mute icon names map from the
observed booleans; tooltips/a11y track the state.
"""
import _bootstrap  # noqa: F401  (sys.path for src/)
import unittest

import chrome
from chrome import (BOTTOM_ORDER, Chrome, mute_icon, playpause_icon)


def child_labels(box):
    """identity names of the direct children of a Gtk.Box, in order
    (uses the widget set_name() tags the bar assigns per BOTTOM_ORDER)."""
    out = []
    w = box.get_first_child()
    while w is not None:
        out.append(w.get_name())
        w = w.get_next_sibling()
    return out


class TestPureMapping(unittest.TestCase):
    def test_playpause_icons(self):
        self.assertEqual(playpause_icon(True),
                         "media-playback-pause-symbolic")
        self.assertEqual(playpause_icon(False),
                         "media-playback-start-symbolic")

    def test_mute_icons(self):
        self.assertEqual(mute_icon(True), "audio-volume-muted-symbolic")
        self.assertEqual(mute_icon(False), "audio-volume-high-symbolic")

    def test_stop_is_not_in_the_order_spec(self):
        self.assertNotIn("stop", BOTTOM_ORDER)
        self.assertNotIn("stop", chrome.__dict__.get("BOTTOM_ICONS", {}))


class TestBarTree(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import gi
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk
        cls.Gtk = Gtk
        cls.cfg = _bootstrap.mini_cfg()
        cls.win = _bootstrap.mini_win()
        cls.bar = Chrome(cls.win, cls.cfg)

    def test_exact_order_by_index(self):
        names = child_labels(self.bar.bottom)
        self.assertEqual(names, list(BOTTOM_ORDER))
        self.assertEqual(BOTTOM_ORDER[0], "play_pause")
        self.assertEqual(BOTTOM_ORDER[1], "prev")
        self.assertEqual(BOTTOM_ORDER[2], "next")
        self.assertEqual(BOTTOM_ORDER[3], "a")
        self.assertEqual(BOTTOM_ORDER[4], "b")
        self.assertEqual(BOTTOM_ORDER[5], "position")
        self.assertEqual(BOTTOM_ORDER[6], "seek")
        self.assertEqual(BOTTOM_ORDER[7], "duration")
        self.assertEqual(BOTTOM_ORDER[8], "repeat")
        self.assertEqual(BOTTOM_ORDER[9], "speed")
        self.assertEqual(BOTTOM_ORDER[10], "volume")
        self.assertEqual(BOTTOM_ORDER[11], "mute")

    def test_mute_adjacent_to_volume(self):
        i = list(BOTTOM_ORDER).index("volume")
        self.assertEqual(BOTTOM_ORDER[i + 1], "mute")

    def test_no_stop_button_in_tree(self):
        self.assertFalse(hasattr(self.bar, "btn_stop"))
        w = self.bar.bottom.get_first_child()
        while w is not None:
            self.assertNotIn("stop", w.get_name())
            w = w.get_next_sibling()

    def test_play_pause_first_widget_is_button_with_icon(self):
        first = self.bar.bottom.get_first_child()
        self.assertIsInstance(first, self.Gtk.Button)
        self.assertEqual(first.get_name(), "play_pause")
        img = first.get_child()
        self.assertIsInstance(img, self.Gtk.Image)

    def test_icons_track_snapshot(self):
        self.bar.update_snapshot({"pause": True, "mute": True,
                                  "idle-active": False,
                                  "eof-reached": False})
        self.assertEqual(
            self.bar.btn_play.get_child().get_icon_name(),
            "media-playback-start-symbolic")
        self.assertEqual(
            self.bar.btn_mute.get_child().get_icon_name(),
            "audio-volume-muted-symbolic")
        self.assertIn("play", str(self.bar.btn_play.get_tooltip_text())
                      .lower())
        self.bar.update_snapshot({"pause": False, "mute": False,
                                  "idle-active": False,
                                  "eof-reached": False})
        self.assertEqual(
            self.bar.btn_play.get_child().get_icon_name(),
            "media-playback-pause-symbolic")
        self.assertEqual(
            self.bar.btn_mute.get_child().get_icon_name(),
            "audio-volume-high-symbolic")
        self.assertIn("pause", str(self.bar.btn_play.get_tooltip_text())
                        .lower())

    def test_tooltip_a11y_tracks_state(self):
        # a11y label: the GTK 4.22 API is update_property([LABEL], [text])
        # (this build has NO get_accessible()) — record the REAL calls.
        seen = []
        orig = self.bar.btn_play.update_property

        def spy(props, values):
            seen.append((list(props), list(values)))
            return orig(props, values)
        self.bar.btn_play.update_property = spy
        try:
            # drive a full flip cycle regardless of the previous test's
            # leftover state — the Pause repaint is guaranteed inside.
            self.bar.update_snapshot({"pause": True, "mute": True,
                                      "idle-active": False,
                                      "eof-reached": False})
            self.bar.update_snapshot({"pause": False, "mute": True,
                                      "idle-active": False,
                                      "eof-reached": False})
        finally:
            del self.bar.btn_play.update_property
        label = self.Gtk.AccessibleProperty.LABEL
        self.assertIn(([label], ["Pause"]), seen)
        self.assertIn("Pause", self.bar.btn_play.get_tooltip_text())
        self.assertIn("unmute",
                      self.bar.btn_mute.get_tooltip_text().lower())
        self.bar.update_snapshot({"pause": True, "mute": True,
                                  "idle-active": False,
                                  "eof-reached": False})
        self.assertIn("Play", self.bar.btn_play.get_tooltip_text())


if __name__ == "__main__":
    unittest.main()
