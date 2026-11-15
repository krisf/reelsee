"""ROUND 28 gate: the ICCCM WM_NORMAL_HINTS arithmetic, PURE — no display,
no Xlib, no gi.

The defect (references/ROUND28-BRIEF.md §1, measured on the operator's
Mutter): ``x11pos._PASPECT`` was ``1 << 5``.  ICCCM §4.1.2.4 puts PAspect
at 128 and PMaxSize at 32, so every "publish the aspect" was publishing
PMaxSize over max words that read 0,0 — "never larger than 0x0" — and a
real WM answered by clamping the window to its content minimum.  Worse,
clear_aspect_ratio cleared that same wrong bit, i.e. it stripped GDK's
own PMaxSize flag.  Xvfb has no WM, so nothing below the waterline was
visible to any earlier gate; this file is the structural half of closing
that gap (the server-side half is scripts/smoke_hintsbits.py).

Negative control (rule: no vacuous pass): this file asserts the literal
bit values and calls the pure functions that did not exist pre-R28 — run
against the old x11pos.py it fails on import/attribute errors and on
_PASPECT == 32.  That is the point.
"""
from __future__ import annotations

import unittest

import _bootstrap  # noqa: F401  (sys.path)

import x11pos


# ICCCM §4.1.2.4 WM_SIZE_HINTS flag bits, spelled out independently of
# x11pos so the auditor never inherits the auditee's table.
ICCCM_BITS = {
    "US_POSITION": 1,
    "US_SIZE": 2,
    "PPOSITION": 4,
    "PSIZE": 8,
    "PMIN_SIZE": 16,
    "PMAX_SIZE": 32,
    "PRESIZE_INC": 64,
    "PASPECT": 128,
    "PBASE_SIZE": 256,
    "PWIN_GRAVITY": 512,
}

# A WM_NORMAL_HINTS array as GDK's X11 backend actually publishes it under
# scale 2 while a video plays (measured, ROUND28-BRIEF §0):
#   flags=80 (PMinSize|PResizeInc), min=100x100, max=0x0 (flag CLEAR),
#   inc=2x2, no aspect words, base 0x0, gravity 0.
GDK_BASELINE = [80, 0, 0, 0, 0, 100, 100, 0, 0, 2, 2, 0, 0, 0, 0, 0, 0, 0]


def foreign_words(out, baseline, skip=()):
    """Every word OUTSIDE flags and the four aspect words survives
    byte-exactly (merge-write discipline), except any index listed in
    `skip` (used where a test deliberately audits a sanitiser)."""
    foreign = [i for i in range(18)
               if i not in (0, 11, 12, 13, 14) and i not in skip]
    return all(out[i] == baseline[i] for i in foreign)


class BitTable(unittest.TestCase):
    """§3-A: the constants themselves, against the ICCCM table."""

    def test_paspect_is_bit_128(self):
        self.assertEqual(x11pos._PASPECT, 128)          # 1 << 7

    def test_pmax_size_is_bit_32_not_reused(self):
        self.assertEqual(x11pos._PMAX_SIZE, 32)         # 1 << 5
        self.assertNotEqual(x11pos._PMAX_SIZE, x11pos._PASPECT)

    def test_full_flag_table_matches_icccm(self):
        for name, value in ICCCM_BITS.items():
            self.assertEqual(getattr(x11pos, "_" + name), value,
                             f"_ {name} != ICCCM bit {value}")

    def test_word_indices(self):
        self.assertEqual((x11pos._SH_MIN_ASPECT, x11pos._SH_MAX_ASPECT),
                         (11, 13))
        self.assertEqual(x11pos._HINTS_WORDS, 18)
        self.assertEqual((x11pos._SH_MIN_W, x11pos._SH_MAX_W), (5, 7))
        self.assertEqual((x11pos._SH_BASE_W, x11pos._SH_GRAVITY), (15, 17))


class MergeAspect(unittest.TestCase):
    """§3-A/§3-B/§3-C as pure arithmetic."""

    def test_publishes_bit_128(self):
        out = x11pos.hints_merge_aspect(GDK_BASELINE, 16, 9)
        self.assertIsNotNone(out)
        self.assertTrue(out[0] & 128, "PAspect (bit 128) not set")
        self.assertEqual((out[11], out[12], out[13], out[14]), (16, 9, 16, 9))

    def test_never_ships_pmax_over_dead_max(self):
        # THE collapse: flags carry PMaxSize (32) with max words 0,0 — the
        # pre-R28 signature, and the array a real WM clamps to the content
        # minimum.  Merging the aspect onto it must DROP the PMaxSize flag.
        legacy = list(GDK_BASELINE)
        legacy[0] = 112                       # 16|32|64 — what R14 shipped
        legacy[7] = legacy[8] = 0
        out = x11pos.hints_merge_aspect(legacy, 16, 9)
        self.assertIsNotNone(out)
        self.assertFalse(out[0] & 32, "a max-0,0 array shipped PMaxSize")
        self.assertEqual((out[7], out[8]), (0, 0))

    def test_live_pmax_is_preserved(self):
        # A client with a REAL maximum keeps flag and words: the strip is
        # scoped to the self-defeating 0,0 form only.
        arr = list(GDK_BASELINE)
        arr[0] |= 32
        arr[7], arr[8] = 1920, 1080
        out = x11pos.hints_merge_aspect(arr, 16, 9)
        self.assertIsNotNone(out)
        self.assertTrue(out[0] & 32, "a live PMaxSize was stripped")
        self.assertEqual((out[7], out[8]), (1920, 1080))

    def test_foreign_words_survive_byte_exactly(self):
        arr = [112, 3, 4, 5, 6, 100, 100, 0, 0, 2, 2, 7, 8, 9, 10, 640, 360, 10]
        out = x11pos.hints_merge_aspect(arr, 16, 9)
        self.assertIsNotNone(out)
        self.assertTrue(foreign_words(out, arr), f"foreign words lost: {out}")

    def test_second_identical_call_writes_nothing(self):
        # §3-C: the clobber watch re-applies on every PropertyNotify; once
        # the server holds exactly our array, the merge returns None and
        # the caller writes NOTHING (no announcement, no WM round-trip).
        first = x11pos.hints_merge_aspect(GDK_BASELINE, 16, 9)
        self.assertIsNotNone(first)
        self.assertIsNone(x11pos.hints_merge_aspect(first, 16, 9),
                          "a re-apply of the identical array must not write")

    def test_bad_ratio_is_none(self):
        for num, den in ((0, 9), (16, 0), (-2, 9), ("x", 9), (None, None)):
            self.assertIsNone(x11pos.hints_merge_aspect(GDK_BASELINE, num,
                                                        den))


class ClearAspect(unittest.TestCase):
    """§3-A: clear clears PAspect ONLY — never PMaxSize, never anything
    else of GDK's."""

    def test_clear_drops_only_paspect(self):
        # flags 208 = PMinSize|PResizeInc|PAspect, with a LIVE max too.
        arr = [208 | 32, 0, 0, 1200, 900, 100, 100, 1920, 1080,
               2, 2, 16, 9, 16, 9, 0, 0, 0]
        out = x11pos.hints_clear_aspect(arr)
        self.assertIsNotNone(out)
        self.assertFalse(out[0] & 128)
        self.assertTrue(out[0] & 32, "clear_aspect_ratio stripped PMaxSize")
        self.assertEqual((out[7], out[8]), (1920, 1080))
        self.assertEqual(out[0], arr[0] & ~128)
        self.assertTrue(foreign_words(out, arr))

    def test_heals_the_legacy_dead_max(self):
        # The pre-R28 bug wrote flags=112 over max 0,0; clearing the lock
        # must heal that self-defeating pair too (flag dropped, words left).
        legacy = [112, 0, 0, 0, 0, 100, 100, 0, 0, 2, 2, 16, 9, 16, 9,
                  0, 0, 0]
        out = x11pos.hints_clear_aspect(legacy)
        self.assertIsNotNone(out)
        self.assertFalse(out[0] & 128)
        self.assertFalse(out[0] & 32, "cleared array still ships max-0,0")

    def test_identical_clear_writes_nothing(self):
        self.assertIsNone(x11pos.hints_clear_aspect(GDK_BASELINE),
                          "bit already clear: no write allowed")


class _FakeProp:
    def __init__(self, arr):
        self.format = 32
        self.value = list(arr)


class FakeServer:
    """Just enough (display, window) for _read_hints/_write_hints — so the
    gate exercises the REAL glue around the pure arithmetic: the
    read-modify-write, the no-change skip, and the clear path."""

    def __init__(self, arr=None):
        self.prop = None if arr is None else _FakeProp(arr)
        self.writes = []
        self.syncs = 0

    # -- display side -----------------------------------------------------
    def intern_atom(self, name):
        return name

    def sync(self):
        self.syncs += 1

    # -- window side ------------------------------------------------------
    def get_full_property(self, prop, typ):
        return self.prop

    def change_property(self, prop, typ, fmt, value, mode):
        self.writes.append(list(value))
        self.prop = _FakeProp(value)


class ServerGlue(unittest.TestCase):
    """set_aspect_ratio/clear_aspect_ratio against a fake server: the
    DECISIONS (write / do not write) and the exact bytes written."""

    class Win:
        pass

    def setUp(self):
        # Patch ONLY the xid/display seam — _read_hints/_write_hints and
        # the pure merge/clear run for real.
        self._saved = (x11pos._window, )
        self.server = None

    def tearDown(self):
        x11pos._window = self._saved[0]

    def arm(self, arr):
        srv = FakeServer(arr)
        self.server = srv

        def fake_window(win):
            return (srv, srv)
        x11pos._window = fake_window
        return srv

    def test_first_call_writes_second_does_not(self):
        srv = self.arm(list(GDK_BASELINE))
        self.assertTrue(x11pos.set_aspect_ratio(self.Win(), 16, 9))
        self.assertEqual(len(srv.writes), 1)
        self.assertTrue(srv.writes[0][0] & 128)
        self.assertFalse(srv.writes[0][0] & 32, "published dead max-0,0")
        # identical re-apply (the watch echo): NOTHING written
        self.assertTrue(x11pos.set_aspect_ratio(self.Win(), 16, 9))
        self.assertEqual(len(srv.writes), 1, "churn: identical array re-written")
        # a DIFFERENT ratio is a change and must go through
        self.assertTrue(x11pos.set_aspect_ratio(self.Win(), 9, 16))
        self.assertEqual(len(srv.writes), 2)
        self.assertEqual(srv.writes[1][11:15], [9, 16, 9, 16])

    def test_legacy_dead_max_healed_on_write(self):
        legacy = [112, 0, 0, 0, 0, 100, 100, 0, 0, 2, 2, 0, 0, 0, 0, 0, 0, 0]
        srv = self.arm(legacy)
        self.assertTrue(x11pos.set_aspect_ratio(self.Win(), 16, 9))
        self.assertEqual(len(srv.writes), 1)
        self.assertTrue(srv.writes[0][0] & 128)
        self.assertFalse(srv.writes[0][0] & 32)

    def test_clear_leaves_live_pmax_and_skips_noop(self):
        live = [208 | 32, 0, 0, 0, 0, 100, 100, 1920, 1080,
                2, 2, 16, 9, 16, 9, 0, 0, 0]
        srv = self.arm(live)
        self.assertTrue(x11pos.clear_aspect_ratio(self.Win()))
        self.assertEqual(len(srv.writes), 1)
        self.assertFalse(srv.writes[0][0] & 128)
        self.assertTrue(srv.writes[0][0] & 32, "server PMaxSize flag stripped")
        # already clear now: a second clear must not touch the server
        self.assertTrue(x11pos.clear_aspect_ratio(self.Win()))
        self.assertEqual(len(srv.writes), 1, "clear churned a no-op write")

    def test_clear_absent_property_writes_nothing(self):
        srv = self.arm(None)
        self.assertTrue(x11pos.clear_aspect_ratio(self.Win()))
        self.assertEqual(srv.writes, [])

    def test_bad_ratio_never_writes(self):
        srv = self.arm(list(GDK_BASELINE))
        self.assertFalse(x11pos.set_aspect_ratio(self.Win(), 0, 9))
        self.assertFalse(x11pos.set_aspect_ratio(self.Win(), "a", 9))
        self.assertEqual(srv.writes, [])

    def test_get_aspect_ratio_uses_the_real_bit(self):
        srv = self.arm([112, 0, 0, 0, 0, 100, 100, 0, 0, 2, 2,
                        16, 9, 16, 9, 0, 0, 0])
        # flags 112 carries bit 32 (PMaxSize), NOT PAspect: read-back must
        # report NOTHING locked (the pre-R28 seam would have called 32 a
        # ratio and "found" 16:9 on an array that never advertised one).
        self.assertIsNone(x11pos.get_aspect_ratio(self.Win()))
        self.assertTrue(x11pos.set_aspect_ratio(self.Win(), 16, 9))
        self.assertEqual(x11pos.get_aspect_ratio(self.Win()), (16, 9))


if __name__ == "__main__":
    unittest.main()
