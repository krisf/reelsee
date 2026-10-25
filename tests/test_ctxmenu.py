"""R6-A: context-menu model tests — pure Gio.Menu, no display, no mpv.

Gate coverage (brief §A/§C): disabled filename header first; exact entry
order Audio, Subtitles, Repeat, Playlist, Chapters, Tracks; missing data
keeps the entry PRESENT but bound to the disabled no-op action; Repeat is
a stateful radio with the three targets; playlist/chapter/track entries
carry the exact integer targets the window routes to mpv; one shared
track-label formatter.
"""
import _bootstrap  # noqa: F401  (sys.path for src/)
import unittest

from ctxmenu import build, model_dump, track_label

FULL = {
    "media-title": "/x/dir/file.mkv",
    "track-list": [
        {"type": "audio", "id": 1, "selected": True, "lang": "eng",
         "codec": "aac"},
        {"type": "audio", "id": 2, "lang": "jpn", "codec": "eac3"},
        {"type": "video", "id": 1, "selected": True, "codec": "h264"},
        {"type": "sub", "id": 1, "lang": "por", "codec": "subrip"},
    ],
    "playlist": [{"filename": "a.mkv", "current": True},
                 {"filename": "b.mkv"}],
    "chapter-list": [{"title": "Intro"}, {"title": "End"}],
}


def flat(menu):
    return model_dump(build(menu))


class TestStructure(unittest.TestCase):
    def test_header_first_and_disabled(self):
        rows = flat(FULL)
        self.assertEqual(rows[0][0], "file.mkv")
        self.assertEqual(rows[0][1], "ctx.noop")
        self.assertIsNone(rows[0][3])

    def test_header_uses_basename(self):
        rows = flat({"filename": "/deep/path/vid.mp4"})
        self.assertEqual(rows[0][0], "vid.mp4")

    def test_body_order_exact(self):
        rows = flat(FULL)
        self.assertEqual([r[0] for r in rows[1:]],
                         ["Audio", "Subtitles", "Repeat", "Playlist",
                          "Chapters", "Tracks"])

    def test_empty_snapshot_all_present_disabled(self):
        rows = flat({})
        self.assertEqual([r[0] for r in rows],
                         ["REELSEE", "Audio", "Subtitles", "Repeat",
                          "Playlist", "Chapters", "Tracks"])
        for r in rows[1:]:
            if r[0] == "Repeat":
                continue          # repeat needs NO file data: live
            self.assertEqual(r[1], "ctx.noop", r[0])
            self.assertIsNone(r[3], r[0])


class TestSections(unittest.TestCase):
    def by_label(self, rows):
        return {r[0]: r for r in rows}

    def test_audio_submenu_targets_int(self):
        by = self.by_label(flat(FULL))
        subs = by["Audio"][3]
        acts = [(a, t) for _l, a, t, _s in subs]
        self.assertIn(("ctx.pick-audio", 2), acts)
        self.assertIn(("ctx.audio-off", None), acts)
        self.assertIn(("ctx.noop", None), acts)   # selected row: present
        # ...but the selected row is not pickable
        self.assertNotIn(("ctx.pick-audio", 1), acts)

    def test_single_selected_track_still_live_submenu(self):
        by = self.by_label(flat({"track-list": [
            {"type": "audio", "id": 1, "selected": True}]}))
        self.assertIsNone(by["Audio"][1])       # a submenu, not disabled
        acts = [(a, t) for _l, a, t, _s in by["Audio"][3]]
        self.assertIn(("ctx.audio-off", None), acts)
        self.assertIn(("ctx.noop", None), acts)

    def test_subtitles_submenu_has_off_entry(self):
        by = self.by_label(flat(FULL))
        acts = [(a, t) for _l, a, t, _s in by["Subtitles"][3]]
        self.assertIn(("ctx.pick-sub", 1), acts)
        self.assertIn(("ctx.sub-off", None), acts)

    def test_image_tracks_excluded_from_pickers(self):
        snap = {"track-list": [{"type": "audio", "id": 1},
                               {"type": "audio", "id": 2,
                                "image": True}]}
        by = self.by_label(flat(snap))
        acts = [(a, t) for _l, a, t, _s in by["Audio"][3]]
        self.assertIn(("ctx.pick-audio", 1), acts)
        self.assertNotIn(("ctx.pick-audio", 2), acts)  # image: excluded

    def test_repeat_radio_three_state_targets(self):
        by = self.by_label(flat({}))
        self.assertEqual([(a, t) for _l, a, t, _s in by["Repeat"][3]],
                         [("ctx.repeat", "off"), ("ctx.repeat", "all"),
                          ("ctx.repeat", "one")])

    def test_playlist_int_targets_and_current_mark(self):
        by = self.by_label(flat(FULL))
        rows = by["Playlist"][3]
        self.assertEqual([(a, t) for _l, a, t, _s in rows],
                         [("ctx.goto-playlist", 0),
                          ("ctx.goto-playlist", 1)])
        self.assertTrue(rows[0][0].startswith("\u25b6"))
        self.assertFalse(rows[1][0].startswith("\u25b6"))

    def test_chapter_int_targets(self):
        by = self.by_label(flat(FULL))
        self.assertEqual([(a, t) for _l, a, t, _s in by["Chapters"][3]],
                         [("ctx.goto-chapter", 0),
                          ("ctx.goto-chapter", 1)])

    def test_tracks_selected_rows_disabled_others_pick(self):
        by = self.by_label(flat(FULL))
        rows = by["Tracks"][3]
        acts = {(a, t) for _l, a, t, _s in rows}
        self.assertIn(("ctx.noop", None), acts)      # selected -> disabled
        self.assertIn(("ctx.pick-audio", 2), acts)
        self.assertIn(("ctx.pick-sub", 1), acts)
        self.assertNotIn(("ctx.pick-video", 1), acts)  # selected: NOOP
        self.assertEqual(by["Tracks"][1], None)      # submenu, live
        self.assertTrue(any("no audio" in l for l, *_r in rows))


class TestTrackLabel(unittest.TestCase):
    def test_marker_and_fields(self):
        self.assertEqual(
            track_label({"type": "audio", "id": 4, "lang": "eng",
                         "codec": "aac"}),
            "\u25cb [audio] aac [eng] #4")
        self.assertEqual(
            track_label({"type": "sub", "id": 1, "selected": True,
                         "title": "Forced"}),
            "\u25cf [sub] Forced #1")

    def test_title_beats_codec(self):
        self.assertIn("Forced", track_label({"type": "sub", "id": 1,
                                             "title": "Forced",
                                             "codec": "ass"}))


if __name__ == "__main__":
    unittest.main()
