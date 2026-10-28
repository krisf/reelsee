"""R16-B unit gate: the hidden-video suppression truth table (src/vidsup.py).

Every row is "what does the app write to mpv's `vid`?" — `None` means WRITE
NOTHING, which is as important an assertion as a value: the setting-off case
and the already-audio-only case are both "write nothing" cases, and a
regression that writes `vid=auto` on remap would silently turn a user's
audio-only window back into a decoding window.
"""
import _bootstrap  # noqa: F401  (sys.path for src/)
import unittest

from vidsup import AUTO, VidSuppression


class TestUnmapRemap(unittest.TestCase):
    def test_default_enabled_unmap_stops_video(self):
        s = VidSuppression(enabled=True)
        self.assertEqual(s.unmap(), "no")
        self.assertTrue(s.suppressed)
        self.assertEqual(s.effective(), "no")

    def test_remap_restores_auto(self):
        s = VidSuppression(enabled=True)
        s.unmap()
        self.assertEqual(s.remap(), AUTO)
        self.assertFalse(s.suppressed)
        self.assertEqual(s.effective(), AUTO)

    def test_remap_restores_the_user_track_not_auto(self):
        """The whole point of remembering: a remap must not quietly pick a
        different track than the one the user chose."""
        s = VidSuppression(enabled=True)
        self.assertEqual(s.user_wants("2"), "2")
        self.assertEqual(s.unmap(), "no")
        self.assertEqual(s.remap(), "2")
        self.assertEqual(s.effective(), "2")

    def test_user_choice_while_hidden_is_recorded_not_applied(self):
        s = VidSuppression(enabled=True)
        s.unmap()
        # A file loading into a hidden window must not turn the decoder on.
        self.assertEqual(s.user_wants(AUTO), "no")
        self.assertEqual(s.effective(), "no")
        self.assertEqual(s.user_vid, AUTO)
        self.assertEqual(s.remap(), AUTO)

    def test_double_unmap_writes_once(self):
        s = VidSuppression(enabled=True)
        self.assertEqual(s.unmap(), "no")
        self.assertIsNone(s.unmap())

    def test_double_remap_writes_once(self):
        s = VidSuppression(enabled=True)
        s.unmap()
        self.assertEqual(s.remap(), AUTO)
        self.assertIsNone(s.remap())

    def test_remap_without_unmap_writes_nothing(self):
        """A window that was never hidden (first map, fullscreen enter) must
        not get a vid write from this code path."""
        s = VidSuppression(enabled=True)
        self.assertIsNone(s.remap())
        self.assertEqual(s.effective(), AUTO)


class TestUserAudioOnly(unittest.TestCase):
    def test_already_off_unmap_writes_nothing(self):
        s = VidSuppression(enabled=True)
        self.assertEqual(s.user_wants("no"), "no")
        self.assertIsNone(s.unmap())
        self.assertFalse(s.suppressed)

    def test_already_off_remap_does_not_turn_video_back_on(self):
        """The user's explicit audio-only choice outranks the power saving."""
        s = VidSuppression(enabled=True)
        s.user_wants("no")
        s.unmap()
        self.assertIsNone(s.remap())
        self.assertEqual(s.effective(), "no")

    def test_turning_video_off_while_hidden_keeps_it_off(self):
        s = VidSuppression(enabled=True)
        s.unmap()
        self.assertEqual(s.user_wants("no"), "no")
        self.assertEqual(s.remap(), "no")


class TestSettingOff(unittest.TestCase):
    def test_disabled_unmap_remap_write_nothing(self):
        s = VidSuppression(enabled=False)
        self.assertIsNone(s.unmap())
        self.assertIsNone(s.remap())
        self.assertEqual(s.effective(), AUTO)
        self.assertFalse(s.suppressed)

    def test_disabled_still_tracks_user_choice(self):
        s = VidSuppression(enabled=False)
        self.assertEqual(s.user_wants("3"), "3")
        self.assertEqual(s.effective(), "3")

    def test_turning_off_while_hidden_hands_the_decoder_back(self):
        s = VidSuppression(enabled=True)
        s.unmap()
        self.assertEqual(s.set_enabled(False), AUTO)
        self.assertFalse(s.suppressed)
        self.assertEqual(s.effective(), AUTO)

    def test_turning_on_while_hidden_stops_it(self):
        s = VidSuppression(enabled=False)
        self.assertIsNone(s.unmap())       # hidden while off: nothing written
        self.assertEqual(s.set_enabled(True), "no")
        self.assertTrue(s.suppressed)
        self.assertEqual(s.effective(), "no")

    def test_turning_off_while_hidden_after_user_chose_no_writes_nothing(self):
        s = VidSuppression(enabled=True)
        s.user_wants("no")
        s.unmap()
        self.assertIsNone(s.set_enabled(False))

    def test_noop_when_flag_unchanged(self):
        s = VidSuppression(enabled=True)
        self.assertIsNone(s.set_enabled(True))
        s.unmap()
        self.assertIsNone(s.set_enabled(True))
        self.assertEqual(s.effective(), "no")

    def test_reenabled_after_release(self):
        """Off → on → unmap → remap behaves like a fresh enabled machine."""
        s = VidSuppression(enabled=False)
        s.set_enabled(True)
        self.assertEqual(s.unmap(), "no")
        self.assertEqual(s.remap(), AUTO)
        self.assertEqual(s.unmap(), "no")


class TestNormalisation(unittest.TestCase):
    def test_none_and_empty_become_auto(self):
        for v in (None, "", "   "):
            self.assertEqual(VidSuppression(user_vid=v).effective(), AUTO)

    def test_whitespace_stripped(self):
        s = VidSuppression()
        self.assertEqual(s.user_wants(" 2 "), "2")
        self.assertEqual(s.user_vid, "2")

    def test_numeric_track_id_becomes_string(self):
        s = VidSuppression()
        self.assertEqual(s.user_wants(1), "1")
        s.unmap()
        self.assertEqual(s.remap(), "1")


class TestSync(unittest.TestCase):
    """The window re-states the OBSERVED surface state on every pump tick
    (GTK4 gives no signal for a window-manager unmap — measured in round 16),
    so `sync` must stay SILENT until the state actually flips: a 100 ms poll
    must not hammer mpv with `vid=no`."""

    def test_repeated_hidden_observations_write_once(self):
        s = VidSuppression()
        self.assertEqual(s.sync(True), "no")
        for _ in range(20):
            self.assertIsNone(s.sync(True))
        self.assertEqual(s.effective(), "no")

    def test_repeated_visible_observations_write_once(self):
        s = VidSuppression()
        s.sync(True)
        self.assertEqual(s.sync(False), AUTO)
        for _ in range(20):
            self.assertIsNone(s.sync(False))

    def test_flapping_writes_each_transition(self):
        s = VidSuppression()
        self.assertEqual([s.sync(h) for h in (True, False, True, False)],
                         ["no", AUTO, "no", AUTO])

    def test_sync_with_setting_off_writes_nothing(self):
        s = VidSuppression(enabled=False)
        self.assertIsNone(s.sync(True))
        self.assertIsNone(s.sync(True))
        self.assertEqual(s.effective(), AUTO)


if __name__ == "__main__":
    unittest.main()
