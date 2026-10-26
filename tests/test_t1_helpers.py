"""§7 T1 helper units: URL detection, m3u build, property map, presets."""
import _bootstrap  # noqa: F401

import unittest

import config as C
import reelsee_io as IO
import settings_ui as S


class IsUrl(unittest.TestCase):
    def test_positive(self):
        for t in ("https://example.com/v.mp4", "http://a/b",
                  "rtmp://s/x", "magnet:?xt=1", "ytdl://yt/x",
                  "srt://h:9000"):
            self.assertTrue(IO.is_url(t), t)

    def test_negative(self):
        for t in ("", "/home/a.mkv", "./rel/x.mp4", "file:///a/b.mkv",
                  "D:\\x", "just text"):
            self.assertFalse(IO.is_url(t), t)


class M3U(unittest.TestCase):
    def test_build(self):
        out = IO.build_m3u(["/a/one.mkv", "https://x/y"])
        self.assertEqual(out, "#EXTM3U\n/a/one.mkv\nhttps://x/y\n")

    def test_empty(self):
        self.assertEqual(IO.build_m3u([]), "#EXTM3U\n")


class YtdlpArgv(unittest.TestCase):
    def test_argv(self):
        self.assertEqual(
            IO.UrlResolver.argv("yt-dlp", "https://x/y"),
            ["yt-dlp", "-g", "--no-playlist", "--no-warnings", "https://x/y"])


class IsDirectMedia(unittest.TestCase):
    """R2-P3: direct-media targets must skip yt-dlp entirely."""

    def test_extensions(self):
        for t in ("https://cdn.x/v.mp4", "/home/a/b.mkv",
                  "https://cdn.x/v.ts?token=9"):
            self.assertTrue(IO.is_direct_media(t), t)

    def test_stream_manifest_behind_query(self):
        self.assertTrue(IO.is_direct_media(
            "https://cdn.x/master.m3u8?token=abc"))
        self.assertTrue(IO.is_direct_media("https://cdn.x/dash/manifest.mpd"))

    def test_page_urls_need_the_resolver(self):
        for t in ("https://example.com/watch?v=abc",
                  "https://example.com/", "ytdl://yt/x",
                  "https://api.x/data.json"):
            self.assertFalse(IO.is_direct_media(t), t)


class UrlResolverQueue(unittest.TestCase):
    """R2-P3/I2: results travel on a queue drained by the MAIN thread
    (poll()); worker touches no GLib.  Driven with fake yt-dlp scripts —
    the real binary is never required and mpv is never involved."""

    def _fake(self, td, body):
        import os
        path = os.path.join(td, "fake-ytdlp")
        with open(path, "w") as fh:
            fh.write("#!/bin/sh\n" + body + "\n")
        os.chmod(path, 0o755)
        return path

    def _drain(self, r, left=10.0):
        import time
        end = time.monotonic() + left
        while time.monotonic() < end:
            got = r.poll()               # caller thread == "main"
            if got is not None:
                return got
            time.sleep(0.02)
        return None

    def test_success_delivers_direct_url(self):
        import tempfile
        import threading
        with tempfile.TemporaryDirectory() as td:
            fake = self._fake(td, 'echo "https://cdn.direct/x.mp4"')
            r = IO.UrlResolver(binary=fake)
            r.resolve("https://site/watch?v=1")
            got = self._drain(r)
            # delivery happens in the polling (owner) thread — in the app
            # that is the GTK main loop, never the yt-dlp worker (I2)
            self.assertIs(threading.current_thread(), threading.main_thread())
            self.assertEqual(got, ("https://site/watch?v=1",
                                   "https://cdn.direct/x.mp4", None))

    def test_failure_carries_resolver_message(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            fake = self._fake(td, 'echo "ERROR: unable to reach" 1>&2;'
                                  " exit 1")
            r = IO.UrlResolver(binary=fake)
            r.resolve("https://dead.invalid/watch")
            got = self._drain(r)
            self.assertIsNotNone(got)
            url, direct, err = got
            self.assertEqual(direct, url)        # raw URL still offered
            self.assertIn("unable to reach", err or "")

    def test_timeout_is_bounded_by_config_timeout(self):
        import tempfile
        import time
        with tempfile.TemporaryDirectory() as td:
            fake = self._fake(td, "sleep 30")
            r = IO.UrlResolver(binary=fake, timeout_s=0.5)
            t0 = time.monotonic()
            r.resolve("https://slow.invalid/watch")
            got = self._drain(r, left=5.0)
            elapsed = time.monotonic() - t0
            self.assertIsNotNone(got)
            self.assertIn("timed out", got[2] or "")
            self.assertLess(elapsed, 2.0)        # old hard-coded 25 s gone

    def test_missing_binary_reports_honestly(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            r = IO.UrlResolver(binary=td + "/no-such-ytdlp")
            r.resolve("https://any/watch")
            got = self._drain(r)
            self.assertEqual("yt-dlp not found", got[2])

    def test_cancel_drops_result_and_supersede_is_generation_safe(self):
        import tempfile
        import time
        with tempfile.TemporaryDirectory() as td:
            slow = self._fake(td, 'sleep 0.4; echo "https://stale/x.mp4"')
            r = IO.UrlResolver(binary=slow)
            r.resolve("https://site/one")
            r.cancel()                            # Esc mid-flight
            time.sleep(0.8)
            self.assertIsNone(r.poll())           # cancelled gen dropped
            fast = self._fake(td, 'echo "https://fresh/x.mp4"')
            r.binary = fast
            r.resolve("https://site/two")
            got = self._drain(r)
            self.assertEqual(got[1], "https://fresh/x.mp4")


class PropertyMap(unittest.TestCase):
    def test_keys_in_schema(self):
        leaves = set(C.schema_leaf_keys())
        for key in C.PROPERTY_MAP:
            self.assertIn(key, leaves, key)

    def test_every_mapped_key_has_a_widget(self):
        bound = set(S.binding_keys())
        for key in C.PROPERTY_MAP:
            self.assertIn(key, bound, key)

    def test_kinds_valid(self):
        for key, (_prop, kind) in C.PROPERTY_MAP.items():
            self.assertIn(kind, ("bool", "boolnum", "float", "str",
                                 "strno"), key)

    def test_crop_to_window_rides_panscan(self):
        # R11: the cover/fill toggle drives mpv's own cover zoom.
        self.assertEqual(C.PROPERTY_MAP["video.crop_to_window"],
                         ("panscan", "boolnum"))
        self.assertEqual(C.coerce("boolnum", True), 1.0)
        self.assertEqual(C.coerce("boolnum", False), 0.0)

    def test_sub_margin_is_y_not_v(self):
        self.assertEqual(C.PROPERTY_MAP["subtitles.margin_y"][0],
                         "sub-margin-y")

    def test_coerce(self):
        self.assertIs(C.coerce("bool", 1), True)
        self.assertEqual(C.coerce("float", "2.5"), 2.5)
        self.assertEqual(C.coerce("strno", ""), "no")
        self.assertEqual(C.coerce("strno", None), "no")
        self.assertEqual(C.coerce("strno", "1"), "1")
        self.assertIsNone(C.coerce("str", ""))
        self.assertIsNone(C.coerce("float", "junk"))


class ScalePresets(unittest.TestCase):
    def test_shape(self):
        self.assertTrue(C.SCALE_PRESETS)
        for name, pack in C.SCALE_PRESETS.items():
            self.assertTrue(pack, name)
            for prop, val in pack.items():
                self.assertIn(prop, ("scale", "cscale", "dscale", "tscale"))
                self.assertIsInstance(val, str)

    def test_default_preset_named(self):
        self.assertIn(str(C.DEFAULTS["video"]["scale_preset"]),
                      C.SCALE_PRESETS)


class YtdlpTimeoutConfig(unittest.TestCase):
    """R2-P3: bounded resolve — config default 5 s, not the old 25 s."""

    def test_default_is_five_seconds(self):
        self.assertEqual(C.DEFAULTS["playback"]["ytdlp_timeout"], 5.0)
        self.assertEqual(IO.UrlResolver.DEFAULT_TIMEOUT_S, 5.0)


class SaveTextAsync(unittest.TestCase):
    """End-to-end pin test: GI borrows the buffer across the async call;
    without the holder pin the bytes land freed (garbage on disk)."""

    def test_roundtrip(self):
        import tempfile
        from gi.repository import GLib
        with tempfile.TemporaryDirectory() as td:
            path = td + "/out.m3u"
            loop = GLib.MainLoop()
            results = []
            # large payload + churn so allocator reuse would corrupt it
            payload = IO.build_m3u(["/x/%04d.mkv" % i for i in range(400)])
            IO.save_text_async(path, payload,
                               lambda ok: (results.append(ok), loop.quit()))
            # churn in the size class right away: if the buffer is not
            # pinned, the async copy reads freed/recycled memory
            junk = [bytearray(payload[i:i + 64].encode())
                    for i in range(0, 4096, 8)]
            del junk
            GLib.timeout_add(5000, loop.quit)
            loop.run()
            with open(path, "rb") as fh:
                self.assertEqual(fh.read().decode("utf-8"), payload)
            self.assertEqual(results, [True])


if __name__ == "__main__":
    unittest.main()
