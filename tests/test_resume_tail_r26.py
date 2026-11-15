"""ROUND 26 gates: a remembered position inside the LAST 10 % of the runtime
must NOT be restored — the file plays from 0 (references/ROUND26-BRIEF.md
§4 B1/B2).

The rule under test (``src/window.py``, ``_resume_restore``): applied AFTER
the existing ``playback.resume_min_s`` gate and BEFORE the seek,

    tail iff dur - pos < frac * dur,   frac = clamp(resume_tail_frac, 0, 0.5)

strictly less than, so exactly 10 % remaining still seeks.  On a tail the
stale entry is removed (``persist=False``), ONE INFO line is logged naming
pos/dur/frac, and no seek is issued.

Why the duration has to be waited for (§0, measured — do not re-derive):
``duration`` arrives through the ASYNC observed snapshot (``OBSERVED_BASE``,
``src/mpvcore.py:25``) and the ``file-loaded`` event callback can beat it, so
the tail rule runs on a bounded ``GLib.timeout_add(150, ...)`` retry over the
snapshot — never a synchronous mpv getter (R9, ``tests/test_no_blocking_mpv.py``).
If the duration never arrives the old behaviour (seek) stands, so this round
can never make resume worse.

Harness: the REAL window methods, unbound on a bare stub with a fake core /
cfg / resume store / chrome (the ``tests/test_monitor_geom_r22.py`` stub
idiom — every method the path calls is bound EXPLICITLY on the stub, so a
missing binding is a loud AttributeError here rather than a silently stale
value).  ``window.GLib`` is replaced by a recorder, so the countdown is
driven by the test itself: no main loop, no sleep, no display.
"""
from __future__ import annotations

import inspect
import textwrap
import unittest
from types import SimpleNamespace

import _bootstrap  # noqa: F401  (sys.path + writable TMP_ROOT)
import window as window_mod
from window import ReelseeWindow

from test_no_blocking_mpv import scan_source   # R9 lint, reused as-is

PATH = "/media/clip.mp4"
SEEK = ("seek", None, "absolute")


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------
class FakeCore:
    """Exactly the two members the restore path may touch: the OBSERVED
    snapshot dict and the async command writer.  There is deliberately NO
    synchronous getter on this object at all — a sync read would raise
    AttributeError, which is B2's no-sync-getter assertion."""

    def __init__(self, snapshot=None):
        self.snapshot = dict(snapshot or {})
        self.commands: list[tuple] = []

    def command(self, *args):
        self.commands.append(args)


class FakeStore:
    def __init__(self, entry=None):
        self.data = {PATH: dict(entry)} if entry is not None else {}
        self.ops: list[tuple] = []

    def get(self, key):
        return self.data.get(key)

    def set(self, key, value, *, persist=True):
        self.ops.append(("set", key, dict(value), persist))
        self.data[key] = dict(value)

    def remove(self, key, *, persist=True):
        self.ops.append(("remove", key, persist))
        self.data.pop(key, None)


class FakeChrome:
    def __init__(self):
        self.toasts: list[str] = []

    def show_toast(self, text):
        self.toasts.append(text)


class FakeCfg:
    """Live config reads, like the real Config.get.  Keys keep their dots, so
    pass them as a dict (``FakeCfg({"playback.resume": False})``)."""

    def __init__(self, over=None):
        self.vals = {"playback.resume": True,
                     "playback.resume_min_s": 300,
                     "playback.resume_tail_frac": 0.10}
        self.vals.update(over or {})
        self.reads: list[str] = []

    def get(self, key, default=None):
        self.reads.append(key)
        return self.vals[key] if key in self.vals else default


class FakeGLib:
    """GLib.timeout_add / source_remove recorder.  Nothing fires by itself:
    the test drives the countdown, so the deferred path is tested with no
    main loop and no sleep."""

    def __init__(self):
        self.armed: dict[int, tuple] = {}   # token -> (interval, fn, args)
        self.pending: list[int] = []
        self.removed: list[int] = []
        self.fired: list[int] = []
        self._next = 0

    def timeout_add(self, interval, fn, *args):
        self._next += 1
        self.armed[self._next] = (interval, fn, args)
        self.pending.append(self._next)
        return self._next

    def source_remove(self, token):
        self.removed.append(token)
        if token in self.pending:
            self.pending.remove(token)

    def fire(self, token=None):
        """Run one armed callback (the test's stand-in for the main loop).
        A token the code already source_remove()d can still be fired by the
        test — that is the "it fired anyway" belt assertion."""
        tok = token if token is not None else self.pending[0]
        if tok in self.pending:
            self.pending.remove(tok)
        self.fired.append(tok)
        _interval, fn, args = self.armed[tok]
        return fn(*args)


class ResumeStub:
    """The real R26 restore path on a bare object.  Bind EVERY method the
    path calls — these stubs bind methods explicitly, so forgetting one is a
    loud AttributeError inside the test, not a stale inherited value."""

    _resume_restore = ReelseeWindow._resume_restore
    _resume_attempt = ReelseeWindow._resume_attempt
    _resume_seek = ReelseeWindow._resume_seek
    _resume_tail_frac = ReelseeWindow._resume_tail_frac
    _resume_cancel_pending = ReelseeWindow._resume_cancel_pending

    def __init__(self, duration=None, pos=None, cfg=None):
        snap = {"duration": duration} if duration is not None else {}
        self.core = FakeCore(snap)
        self.app = SimpleNamespace(
            resume_store=FakeStore({"pos": pos} if pos is not None else None))
        self.chrome = FakeChrome()
        self.cfg = FakeCfg(cfg)
        self.target = PATH
        self._closing = False
        self._resume_last = -10.0
        self._resume_pending_id = None

    # ---- conveniences (test-side only, never the code under test) --------
    @property
    def seeks(self) -> list[tuple]:
        return [c for c in self.core.commands if c and c[0] == "seek"]

    @property
    def removed(self) -> list[tuple]:
        return [op for op in self.app.resume_store.ops if op[0] == "remove"]


def row(pos, dur, frac, min_s, resume=True):
    """One truth-table row with the duration ALREADY observed (no deferral)."""
    return ResumeStub(duration=dur, pos=pos,
                      cfg={"playback.resume_tail_frac": frac,
                           "playback.resume_min_s": min_s,
                           "playback.resume": resume})


def _source_of(name: str) -> str:
    """The def line + body of one window method, for the R9 lint."""
    lines, _start = inspect.getsourcelines(getattr(ReelseeWindow, name))
    return textwrap.dedent("".join(lines))


# --------------------------------------------------------------------------
# B1 — the truth table, duration already observed
# --------------------------------------------------------------------------
class B1TruthTable(unittest.TestCase):
    """pos / dur / frac / min_s → seek, or no-seek + entry removed."""

    def assert_seeked(self, stub, pos, why=""):
        self.assertEqual(stub.seeks, [("seek", str(float(pos)), "absolute")],
                         f"exactly one seek to the remembered pos {why}")
        self.assertEqual(stub.removed, [], "the seek path must not remove")
        self.assertTrue(stub.chrome.toasts, "a seek still toasts")
        self.assertIsNone(stub._resume_pending_id, "no timer left armed")
        self.assertEqual(stub.app.resume_store.get(PATH) is not None, True,
                         "the entry survives for the next quit (unchanged)")

    def assert_tail(self, stub, why=""):
        self.assertEqual(stub.seeks, [], f"tail must NOT seek {why}")
        self.assertEqual(len(stub.removed), 1,
                         f"tail removes the stale entry {why}")
        _op, key, persist = stub.removed[0]
        self.assertEqual(key, PATH)
        self.assertFalse(persist, "persist=False: the flush happens on close")
        self.assertIsNone(stub.app.resume_store.get(PATH), "entry is gone")
        self.assertEqual(stub.chrome.toasts, [], "no 'Resumed at' toast")
        self.assertIsNone(stub._resume_pending_id, "no timer left armed")

    # row 1: pos 1000 / dur 10000 / 0.10 / 300 → 9000 s left ≥ 1000 ⇒ seek
    def test_row_90pct_remains_seeks(self):
        s = row(1000, 10000, 0.10, 300); s._resume_restore()
        self.assert_seeked(s, 1000, "(90 % remains)")

    # row 2: 990 s left < 1000 s ⇒ tail
    def test_row_just_inside_tail_no_seek(self):
        s = row(9001, 10000, 0.10, 300); s._resume_restore()
        self.assert_tail(s, "(990 < 1000)")

    # row 3: STRICTLY less than — exactly 10 % remaining is NOT a tail
    def test_row_exact_boundary_seeks(self):
        s = row(9000, 10000, 0.10, 300); s._resume_restore()
        self.assert_seeked(s, 9000, "(boundary: 1000 !< 1000)")

    # row 4: 500 s left < 1000 s ⇒ tail
    def test_row_deep_tail_no_seek(self):
        s = row(9500, 10000, 0.10, 300); s._resume_restore()
        self.assert_tail(s, "(500 left)")

    # row 5: pos 500 / dur 1000 / 0.10 ⇒ remaining 500 s vs threshold
    # 0.10*1000 = 100 s ⇒ NOT a tail ⇒ seek.  (The brief's table prints "no
    # seek (500 < 1000 remaining)" for this row; that parenthetical compares
    # the remaining time against the WHOLE duration, which no reading of
    # §2's rule — "tail iff dur - pos < frac * dur" — supports, and the
    # neighbouring row (400/1000 ⇒ "600 > 100 ⇒ seek") uses the correct
    # comparison.  The rule text is authoritative, so this row is a seek.)
    def test_row_half_remaining_seeks(self):
        s = row(500, 1000, 0.10, 300); s._resume_restore()
        self.assert_seeked(s, 500, "(500 remaining > 100 threshold)")

    # row 6: min_s passes and 600 s left > 100 s ⇒ seek
    def test_row_min_s_passes_and_not_tail_seeks(self):
        s = row(400, 1000, 0.10, 300); s._resume_restore()
        self.assert_seeked(s, 400, "(min_s passes, 600 > 100)")

    # row 7: resume_min_s still decides FIRST, and it is NOT the tail rule
    def test_row_below_min_s_skips_without_removing(self):
        s = row(299, 10000, 0.10, 300); s._resume_restore()
        self.assertEqual(s.seeks, [], "min_s gate still skips the seek")
        self.assertEqual(s.removed, [],
                         "min_s skip must not remove the entry (unchanged)")
        self.assertIsNotNone(s.app.resume_store.get(PATH),
                             "entry survives: the min_s rule is untouched")
        self.assertIsNone(s._resume_pending_id, "min_s skip defers nothing")

    # row 8: frac 0 disables the rule entirely
    def test_row_frac_zero_disables_rule(self):
        s = row(900, 1000, 0.0, 300); s._resume_restore()
        self.assert_seeked(s, 900, "(frac 0 disables the rule)")

    # row 9: 0.9 clamps to 0.5 ⇒ 100 s left < 500 s ⇒ tail
    def test_row_frac_clamps_to_half(self):
        s = row(900, 1000, 0.9, 300); s._resume_restore()
        self.assert_tail(s, "(frac clamps to 0.5)")

    # row 9b: a negative value clamps to 0 — a bad config must never make
    # every video restart
    def test_row_frac_negative_clamps_to_zero(self):
        s = row(900, 1000, -3.0, 300); s._resume_restore()
        self.assert_seeked(s, 900, "(negative frac clamps to 0 ⇒ rule off)")

    # row 9c: a non-numeric value falls back to the default (0.10)
    def test_row_frac_garbage_falls_back_to_default(self):
        s = row(9500, 10000, "0.1.0", 300); s._resume_restore()
        self.assert_tail(s, "(garbage frac ⇒ default 0.10)")

    # row 10: corrupt entry (dur < pos) counts as a tail
    def test_row_corrupt_entry_dur_less_than_pos_is_tail(self):
        s = row(5000, 1000, 0.10, 300); s._resume_restore()
        self.assert_tail(s, "(corrupt entry: dur < pos)")

    # row 11: dur = 0 ⇒ no guess: defer (B2 owns the deferred matrix)
    def test_row_no_duration_defers_instead_of_guessing(self):
        s = row(900, 0, 0.10, 300); s._resume_restore()
        self.assertEqual(s.seeks, [], "no duration ⇒ no immediate seek")
        self.assertEqual(s.removed, [], "no duration ⇒ no removal")
        self.assertIsNotNone(s._resume_pending_id, "retry armed on the snapshot")

    def test_resume_off_skips_everything(self):
        s = row(9500, 10000, 0.10, 300, resume=False); s._resume_restore()
        self.assertEqual(s.core.commands, [])
        self.assertEqual(s.removed, [])
        self.assertIsNone(s._resume_pending_id)

    def test_no_entry_skips_everything(self):
        s = ResumeStub(duration=10000); s._resume_restore()
        self.assertEqual(s.core.commands, [])
        self.assertEqual(s.removed, [])
        self.assertIsNone(s._resume_pending_id)

    def test_tail_frac_is_read_live_from_config(self):
        s = row(9500, 10000, 0.10, 300); s._resume_restore()
        self.assertIn("playback.resume_tail_frac", s.cfg.reads)

    def test_min_s_is_still_read_live_from_config(self):
        s = row(9500, 10000, 0.10, 300); s._resume_restore()
        self.assertIn("playback.resume_min_s", s.cfg.reads)


# --------------------------------------------------------------------------
# B2 — the deferred-duration path
# --------------------------------------------------------------------------
class B2DeferredDuration(unittest.TestCase):
    """``file-loaded`` can beat the ``duration`` observation, so the tail
    rule waits on the observed snapshot — bounded, and never a sync getter."""

    def setUp(self):
        self.real_glib = window_mod.GLib
        self.glib = FakeGLib()
        window_mod.GLib = self.glib

    def tearDown(self):
        window_mod.GLib = self.real_glib

    def test_seek_only_after_duration_arrives_and_exactly_once(self):
        stub = ResumeStub(pos=1000)              # duration NOT yet observed
        stub._resume_restore()
        self.assertEqual(stub.seeks, [], "must not seek before duration")
        self.assertEqual(self.glib.pending, [1], "one bounded retry armed")
        self.assertEqual(self.glib.armed[1][0], 150, "150 ms cadence")

        stub.core.snapshot["duration"] = 10000.0
        self.assertFalse(self.glib.fire(1), "callback is one-shot")
        self.assertEqual(stub.seeks, [("seek", "1000.0", "absolute")],
                         "seeks once the duration lands")
        self.assertEqual(self.glib.pending, [], "no further retries")
        self.assertIsNone(stub._resume_pending_id)
        self.assertEqual(len(stub.seeks), 1, "exactly one seek for this load")

    def test_tail_rule_evaluated_against_the_real_duration(self):
        stub = ResumeStub(pos=9500)              # tail, once duration lands
        stub._resume_restore()
        self.assertEqual(stub.seeks, [])
        stub.core.snapshot["duration"] = 10000.0
        self.glib.fire()
        self.assertEqual(stub.seeks, [], "tail found late: still no seek")
        self.assertEqual(len(stub.removed), 1, "stale entry dropped")
        self.assertIsNone(stub.app.resume_store.get(PATH))
        self.assertEqual(self.glib.pending, [])

    def test_non_tail_after_deferral_toasts_once(self):
        # the deferred path must keep today's user-visible behaviour
        stub = ResumeStub(pos=1000)
        stub._resume_restore()
        stub.core.snapshot["duration"] = 10000.0
        self.glib.fire()
        self.assertEqual(len(stub.chrome.toasts), 1, "one 'Resumed at' toast")
        self.assertEqual(stub._resume_last, 1000.0,
                         "_resume_last seeds the 5 s tick gate (unchanged)")

    def test_budget_expiry_falls_back_to_a_single_seek(self):
        # duration NEVER arrives: today's behaviour must stand (this round
        # may never make resume worse for files mpv reports no duration for)
        stub = ResumeStub(pos=1000)
        stub._resume_restore()
        fired = 0
        while self.glib.pending and fired < 100:
            self.glib.fire()
            fired += 1
        self.assertEqual(stub.seeks, [("seek", "1000.0", "absolute")],
                         "expiry falls back to exactly one seek")
        self.assertEqual(self.glib.pending, [], "budget exhausted: nothing left")
        self.assertIsNone(stub._resume_pending_id)
        self.assertLessEqual(fired, 14, "bounded retry (~2 s at 150 ms)")
        self.assertGreaterEqual(fired, 8, "retry is not a one-shot guess")
        self.assertEqual({iv for iv, _fn, _a in self.glib.armed.values()},
                         {150}, "every retry on the 150 ms cadence")

    def test_closing_cancels_the_pending_attempt(self):
        stub = ResumeStub(pos=1000)
        stub._resume_restore()
        tok = stub._resume_pending_id
        self.assertIsNotNone(tok)
        stub._closing = True                     # window on its way out
        stub._resume_cancel_pending()            # what close-request does
        self.assertIn(tok, self.glib.removed, "timer removed from GLib")
        self.assertIsNone(stub._resume_pending_id)
        self.glib.fire(tok)                      # belt: a fired-anyway tick
        self.assertEqual(stub.seeks, [], "never seeks after the window is gone")
        self.assertEqual(self.glib.pending, [])

    def test_second_restore_call_cancels_the_first_attempt(self):
        stub = ResumeStub(pos=1000)
        stub._resume_restore()
        first = stub._resume_pending_id
        # (a) a second call while the duration is STILL unknown: the old
        # attempt is cancelled and exactly one fresh attempt is armed.
        stub._resume_restore()
        self.assertIn(first, self.glib.removed, "old attempt cancelled")
        self.assertEqual(len(self.glib.armed), 2, "exactly one fresh attempt")
        self.assertEqual(self.glib.pending, [2])
        # (b) the duration lands and a third call decides immediately: the
        # armed timer dies, so the old attempt can never seek a second time.
        stub.core.snapshot["duration"] = 10000.0
        stub._resume_restore()
        self.assertIn(2, self.glib.removed, "second attempt cancelled too")
        self.assertEqual(self.glib.pending, [])
        self.assertEqual(stub.seeks, [("seek", "1000.0", "absolute")])
        self.assertEqual(len(stub.seeks), 1, "never seek twice for one load")

    def test_second_file_load_cancels_the_pending_attempt(self):
        # _on_file_loaded calls _resume_restore(); a load while a previous
        # attempt is still waiting must leave exactly one decision pending.
        stub = ResumeStub(pos=1000)
        stub._resume_restore()
        first = stub._resume_pending_id
        stub.target = "/media/other.mp4"
        stub.app.resume_store.set("/media/other.mp4", {"pos": 800.0},
                                  persist=False)
        stub._resume_restore()                   # == the new file's file-loaded
        self.assertIn(first, self.glib.removed)
        self.assertEqual(len(self.glib.armed), 2)
        self.assertEqual(self.glib.pending, [2])
        stub.core.snapshot["duration"] = 10000.0
        self.glib.fire()
        self.assertEqual(stub.seeks, [("seek", "800.0", "absolute")],
                         "the NEW file's pos is the one restored")
        self.assertEqual(len(stub.seeks), 1)

    def test_no_synchronous_getter_on_the_restore_path(self):
        # R9: the restore path reads the OBSERVED snapshot only.  (a) the
        # fake core has no getter at all, so any sync read would have raised
        # in every test above; (b) the lint from tests/test_no_blocking_mpv.py
        # sees no sync mpv call in the source of the new methods.
        stub = ResumeStub(pos=1)
        for attr in ("get_property", "get_time", "get_duration",
                     "get_property_string", "get_property_string_sync"):
            self.assertFalse(hasattr(stub.core, attr),
                             f"fake core must not offer {attr}()")
        for name in ("_resume_restore", "_resume_attempt", "_resume_seek",
                     "_resume_tail_frac", "_resume_cancel_pending"):
            self.assertEqual(scan_source(_source_of(name), name), [],
                             f"{name} must not call a sync mpv getter")


if __name__ == "__main__":
    unittest.main()
