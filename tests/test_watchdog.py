"""P7 gate: watchdog rules (Appendix C) — every rule driven with a fake
clock and hand-built Samples; no mpv, no GTK."""
import _bootstrap  # noqa: F401  (src on sys.path)

import unittest

import watchdog as WD


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


def sample(clock_t, *, vf=30.0, cfps=30.0, drop=0, dec=0, hwdec="vaapi",
           playing=True, pause=False, seeking=False, idle=False, eof=False,
           paused_for_cache=False, cbs=None):
    """`cbs` = observed cache-buffering-state (None = never observed)."""
    return WD.Sample(t=clock_t, playing=playing, pause=pause,
                     seeking=seeking, idle=idle, eof=eof, vf_fps=vf,
                     container_fps=cfps, drop=drop, dec_drop=dec,
                     hwdec=hwdec, paused_for_cache=paused_for_cache,
                     cache_buffering_state=cbs)


class Base(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.wd = WD.Watchdog(clock=self.clock)
        self.wd.file_loaded()

    def feed(self, n=3, dt=1.0, **kw):
        """Feed a clean baseline then n OVERLOADED samples; returns the n
        actions (the baseline is needed: a first sample has no prev)."""
        vf = kw.pop("vf", 10.0)
        kw.setdefault("drop", 0)
        self.wd.feed(sample(self.clock.t, vf=30.0, **kw))
        acts = []
        for _ in range(n):
            self.clock.advance(dt)
            acts.append(self.wd.feed(sample(self.clock.t, vf=vf, **kw)))
        return acts


class GraceConfirm(Base):
    def test_startup_grace_ignores(self):
        # 3 bad samples inside the 4 s grace → no action ever
        self.assertIsNone(self.feed(3)[2])
        self.wd.file_loaded()                     # grace again
        self.assertIsNone(self.feed(5)[4])

    def test_action_after_grace_and_3_confirm(self):
        self.clock.advance(5.0)                   # out of grace
        acts = self.feed(3)
        self.assertIsNone(acts[0])
        self.assertIsNone(acts[1])
        self.assertEqual(acts[2]["kind"], "demote")
        self.assertEqual(acts[2]["from"], "vaapi")
        self.assertEqual(acts[2]["to"], "vaapi-copy")

    def test_single_sample_noise(self):
        self.clock.advance(5.0)
        acts = self.feed(2)
        self.assertIsNone(acts[1])
        self.clock.advance(1.0)
        clean = self.wd.feed(sample(self.clock.t, vf=30.0))  # clean resets
        self.assertIsNone(clean)
        acts = self.feed(2)                       # streak restarted
        self.assertIsNone(acts[1])
        self.clock.advance(1.0)
        self.assertEqual(self.wd.feed(
            sample(self.clock.t, vf=10.0))["kind"], "demote")

    def test_cooldown_3s(self):
        self.clock.advance(5.0)
        self.assertEqual(self.feed(3)[2]["kind"], "demote")
        # still overloaded but inside cooldown → nothing counted for < 3 s
        for _ in range(2):
            self.clock.advance(1.0)
            s = self.wd.feed(sample(self.clock.t, vf=10.0, hwdec="vaapi-copy"))
            self.assertIsNone(s)
        # after cooldown + 3 more confirms → second demote step
        acts = self.feed(3, hwdec="vaapi-copy")
        self.assertEqual(acts[2]["kind"], "demote")
        self.assertEqual(acts[2]["to"], "no")


class Thresholds(Base):
    def test_fps_floor_path_and_boundary(self):
        self.clock.advance(5.0)
        # exactly 90 % is NOT overloaded (strict <); 89.99 % is
        for vf in (27.0, 27.0, 27.0):
            self.clock.advance(1.0)
            self.assertIsNone(self.wd.feed(
                sample(self.clock.t, vf=vf, drop=0)))
        acts = self.feed(3, vf=26.99)
        self.assertEqual(acts[2]["kind"], "demote")
        self.assertLess(acts[2]["fps_ratio"], 0.9)

    def test_drop_ceiling_requires_bad_or_unknown_fps(self):
        """D2 (R7): the drop path ONLY complements a bad fps_ratio — never
        fires alone while fps is healthy (the real-world false positive was
        fps_ratio=1.00 with drop_ratio=0.97 demoting)."""
        self.clock.advance(5.0)
        self.wd.file_loaded()
        self.clock.advance(5.0)
        # healthy fps + MASSIVE drop-counter growth (97 % budget) → never
        self.wd.feed(sample(self.clock.t, vf=30.0, drop=0))
        d = 0
        for i in range(1, 6):
            self.clock.advance(1.0)
            d += 29                                  # 29/30 ≈ 0.97 dropped
            self.assertIsNone(self.wd.feed(
                sample(self.clock.t, vf=30.0, drop=d)))
        # fps UNKNOWN (estimated-vf-fps null) + drops over the ceiling →
        # the drop path still works, boundary intact (3 % ceiling)
        self.wd.file_loaded()
        self.clock.advance(5.0)
        self.wd.feed(sample(self.clock.t, vf=None, drop=0))
        self.assertIsNone(self.wd.feed(
            sample(self.clock.t + 1, vf=None, drop=0)))     # clean
        d = 0
        acts = []
        for i in range(1, 4):
            self.clock.advance(1.0)
            d += 2                               # 2/s over 30 = 6.67 % > 3 %
            acts.append(self.wd.feed(
                sample(self.clock.t, vf=None, drop=d)))
        self.assertEqual(acts[2]["kind"], "demote")
        self.assertGreater(acts[2]["drop_ratio"], 0.03)
        self.assertIsNone(acts[2]["fps_ratio"])
        self.assertEqual(acts[2]["raw"]["d_drop"], 2)
        # bad fps + real drops: the classic positive still demotes
        self.wd.file_loaded()
        self.clock.advance(5.0)
        acts = self.feed(3, vf=26.99, drop=0)
        self.assertEqual(acts[2]["kind"], "demote")

    def test_d2_action_carries_raw_counters(self):
        """Every positive decision carries the RAW counters (drop counts,
        deltas, dt) so it is diagnosable from the log alone."""
        self.clock.advance(5.0)
        acts = self.feed(3, vf=15.0, drop=0)
        act = acts[2]
        self.assertEqual(act["kind"], "demote")
        raw = act["raw"]
        self.assertLessEqual(
            {"drop", "dec_drop", "d_drop", "d_dec_drop", "dropped",
             "frame_delta", "dt"} - set(raw), set())


class Skips(Base):
    def test_skip_states_never_fire(self):
        self.clock.advance(5.0)
        for kw in (dict(pause=True), dict(seeking=True), dict(idle=True),
                   dict(eof=True), dict(cfps=0.0), dict(playing=False),
                   dict(vf=None)):
            with self.subTest(**kw):
                acts = self.feed(4, **kw)
                self.assertTrue(all(a is None for a in acts))

    def test_hwdec_no_is_terminal(self):
        self.clock.advance(5.0)
        acts = self.feed(5, hwdec="no", vf=None, cfps=0.0)
        self.assertTrue(all(a is None for a in acts))
        # dropping but terminal engine → one toast action, then silence
        acts = self.feed(4, hwdec="no")
        kinds = [a["kind"] if a else None for a in acts]
        self.assertIn("toast", kinds)
        self.assertIsNone(self.wd.feed(
            sample(self.clock.t + 1, vf=5.0, hwdec="no")))


class Chains(unittest.TestCase):
    def drive(self, start):
        clock = Clock()
        wd = WD.Watchdog(clock=clock)
        wd.file_loaded()
        clock.advance(5.0)
        mode = start
        seen = []
        prev = None
        steps = 0
        while steps < 40 and (mode in WD.CHAINS and WD.CHAINS[mode]):
            steps += 1
            clock.advance(1.0)
            s = sample(clock.t, vf=10.0, hwdec=mode,
                       drop=(prev.drop + 15) if prev else 0)
            prev = s
            act = wd.feed(s)
            if act:
                seen.append(act["to"])
                mode = act["to"]
                clock.advance(3.0)               # cooldown
        return seen

    def test_chain_vaapi(self):
        self.assertEqual(self.drive("vaapi"), ["vaapi-copy", "no"])

    def test_chain_vulkan(self):
        self.assertEqual(
            self.drive("vulkan"),
            ["vulkan-copy", "vaapi", "vaapi-copy", "no"])

    def test_chain_auto_safe(self):
        self.assertEqual(self.drive("auto-safe"), ["vaapi-copy", "no"])

    def test_chain_suffix_stripped(self):
        self.assertEqual(WD.mode_base("vaapi(h264)"), "vaapi")
        self.assertEqual(WD.mode_base(None), "no")

    def test_chain_cpu_terminal_toast_only(self):
        clock = Clock()
        wd = WD.Watchdog(clock=clock)
        wd.file_loaded()
        clock.advance(5.0)
        acts = []
        for i in range(8):
            clock.advance(1.0)
            acts.append(wd.feed(sample(clock.t, vf=5.0, hwdec="no",
                                       drop=0)))
        kinds = [a["kind"] if a else None for a in acts]
        self.assertNotIn("demote", kinds)
        self.assertEqual(kinds.count("toast"), 1)   # exactly one toast


class Switches(unittest.TestCase):
    def test_disabled_switch(self):
        clock = Clock()
        wd = WD.Watchdog(enabled=False, clock=clock)
        wd.file_loaded()
        clock.advance(5.0)
        for _ in range(10):
            clock.advance(1.0)
            self.assertIsNone(wd.feed(sample(clock.t, vf=1.0)))

    def test_retry_on_new_file(self):
        clock = Clock()
        wd = WD.Watchdog(clock=clock)
        wd.file_loaded()
        clock.advance(5.0)
        for i in range(4):
            clock.advance(1.0)
            act = wd.feed(sample(clock.t, vf=10.0, drop=0))
        self.assertEqual(act["kind"], "demote")
        self.assertTrue(wd.retry_pending(True))    # consumed once
        self.assertFalse(wd.retry_pending(True))   # exactly once

    def test_retry_flag_off_keeps_demoted(self):
        clock = Clock()
        wd = WD.Watchdog(clock=clock)
        wd.file_loaded()
        clock.advance(5.0)
        for _ in range(4):
            clock.advance(1.0)
            wd.feed(sample(clock.t, vf=10.0, drop=0))
        self.assertFalse(wd.retry_pending(False))  # config says no retry
        self.assertTrue(wd.retry_pending(True))    # later flip still retries


class SamplerFallback(unittest.TestCase):
    def test_frame_delta_fallback_uses_container_fps(self):
        # vf None → ratios() falls back to container_fps for frame_delta
        clock = Clock()
        a = sample(clock.t, vf=None, cfps=30.0, drop=0)
        b = sample(clock.t + 1, vf=None, cfps=30.0, drop=5)
        fps_r, drop_r = WD.ratios(b, a, 1.0)
        self.assertIsNone(fps_r)              # null vf → no floor signal
        self.assertAlmostEqual(drop_r, 5 / 30.0)

    def test_apply_action_shape(self):
        act = {"kind": "demote", "from": "vaapi", "to": "vaapi-copy",
               "fps_ratio": 0.5, "drop_ratio": 0.1}
        self.assertEqual(
            set(act) >= {"kind", "from", "to", "fps_ratio", "drop_ratio"},
            True)


class R2Engagement(unittest.TestCase):
    """R2-P2/I1 gates: engagement ladder driven by a FAKE snapshot
    sequence + a RECORDING apply_hwdec (mpv itself is never mocked) and a
    fake clock; plus the corrected watchdog skip rule."""

    def _watcher(self, mode):
        chain = WD.FallbackChain(mode)
        applied, acts = [], []
        w = WD.EngagementWatcher(chain, applied.append, acts.append,
                                 clock=lambda: 0.0)
        return w, chain, applied, acts

    def test_engage_on_first_try(self):
        w, chain, applied, acts = self._watcher("vaapi-copy")
        w.arm(now=0.0)
        a = w.feed("vaapi-copy", now=0.2)          # engaged right away
        self.assertEqual(a["kind"], "engaged")
        self.assertEqual(a["mode"], "vaapi-copy")
        self.assertFalse(a["fell_back"])
        self.assertEqual(applied, [])              # NO needless re-apply
        self.assertFalse(w.armed)
        self.assertIsNone(w.feed("vaapi-copy", now=5.0))

    def test_needs_one_fallback(self):
        w, chain, applied, acts = self._watcher("vaapi")
        w.arm(now=0.0)
        self.assertIsNone(w.feed("no", now=1.0))   # inside the 3 s window
        self.assertIsNone(w.feed("no", now=2.9))
        a = w.feed("no", now=3.1)
        self.assertEqual(a["kind"], "fallback")
        self.assertEqual((a["from"], a["to"]), ("vaapi", "vaapi-copy"))
        self.assertEqual(applied, ["vaapi-copy"])  # proven mechanism, once
        self.assertIsNone(w.feed("no", now=4.0))   # deadline re-armed
        b = w.feed("vaapi-copy(h264)", now=6.5)
        self.assertEqual(b["kind"], "engaged")
        self.assertTrue(b["fell_back"])
        self.assertEqual(b["from"], "vaapi")
        self.assertEqual(applied, ["vaapi-copy"])  # applied exactly once

    def test_exhausted_to_cpu(self):
        w, chain, applied, acts = self._watcher("vaapi")
        w.arm(now=0.0)
        self.assertEqual(w.feed("no", now=3.5)["kind"], "fallback")
        a = w.feed("no", now=7.0)
        self.assertEqual(a["kind"], "exhausted")
        self.assertEqual((a["from"], a["to"]), ("vaapi-copy", "no"))
        self.assertEqual(applied, ["vaapi-copy", "no"])
        self.assertTrue(chain.exhausted)
        self.assertFalse(w.armed)
        self.assertIsNone(w.feed("no", now=12.0))  # terminal, stays silent

    def _wd(self):
        clock = Clock()
        wd = WD.Watchdog(clock=clock)
        wd.file_loaded()
        clock.advance(5.0)                         # past grace
        return wd, clock

    @staticmethod
    def _overload(wd, clock, n=3, hwdec="no"):
        acts = []
        for _ in range(n):
            clock.advance(1.0)
            acts.append(wd.feed(sample(clock.t, vf=10.0, drop=30,
                                       hwdec=hwdec)))
        return acts

    def test_watchdog_retries_after_failed_engagement(self):
        # configured GPU engine, hardware decode NOT engaged, sustained
        # overload: the watchdog must ACT (next ladder step), never skip.
        wd, clock = self._wd()
        chain = WD.FallbackChain("vaapi")
        wd.set_engine(chain=chain, is_cpu=False)
        wd.feed(sample(clock.t, vf=30.0, hwdec="no"))      # baseline prev
        acts = self._overload(wd, clock)
        self.assertEqual(acts[2]["kind"], "demote")
        self.assertTrue(acts[2].get("retry"))
        self.assertEqual(acts[2]["to"], "vaapi-copy")
        acts = self._overload(wd, clock, n=5)              # still dropping
        fired = [a for a in acts if a]
        self.assertEqual(fired[-1]["to"], "no")            # ladder descends
        self.assertTrue(chain.exhausted)
        self.assertIsNone(self._overload(wd, clock)[2])    # now terminal

    def test_watchdog_skips_only_cpu_or_exhausted(self):
        # configured CPU: "no" samples are fully skipped (no toast, no act)
        wd, clock = self._wd()
        wd.set_engine(chain=None, is_cpu=True)
        wd.feed(sample(clock.t, vf=30.0, hwdec="no"))
        self.assertEqual(self._overload(wd, clock, n=6), [None] * 6)
        # legacy unwired watchdog keeps old terminal-toast behaviour
        wd2, clock2 = self._wd()
        wd2.feed(sample(clock2.t, vf=30.0, hwdec="no"))
        acts2 = self._overload(wd2, clock2, n=3)
        self.assertEqual(acts2[2]["kind"], "toast")

    def test_record_last_fallback_merges_engines_json(self):
        import json
        import tempfile
        import os as _os
        import engine as E
        with tempfile.TemporaryDirectory() as d:
            path = _os.path.join(d, "engines.json")
            with open(path, "w") as fh:
                json.dump({"0000:13:00.0|vaapi-copy|h264":
                           {"ok": True, "ts": 1}}, fh)
            self.assertTrue(E.record_last_fallback(
                {"from": "vaapi", "to": "vaapi-copy", "reason": "engagement"},
                path=path))
            data = json.load(open(path))
            self.assertEqual(data["last_fallback"]["to"], "vaapi-copy")
            self.assertIn("0000:13:00.0|vaapi-copy|h264", data)  # merge


class R17Buffering(unittest.TestCase):
    """R17-D truth table (ROUND17-BRIEF §D.3), driven by synthetic observed
    property feeds — mpv is never involved.  The rule under test: a sample
    in which the stream is buffering (`paused-for-cache` true OR
    `cache-buffering-state` < 100) is NOT COUNTED — the consecutive-overload
    streak neither increments nor resets across it, and the GPU-engagement
    deadline does not tick down."""

    def setUp(self):
        self.clock = Clock()
        self.wd = WD.Watchdog(clock=self.clock)
        self.wd.file_loaded()
        self.clock.advance(5.0)                   # past the startup grace

    def _watcher(self, mode="vaapi"):
        chain = WD.FallbackChain(mode)
        applied, acts = [], []
        w = WD.EngagementWatcher(chain, applied.append, acts.append,
                                 clock=self.clock)
        return w, applied, acts

    def _clean(self, **kw):
        return self.wd.feed(sample(self.clock.t, vf=30.0, **kw))

    def test_a_ten_buffering_samples_never_demote(self):
        """(a) 10 consecutive buffering samples with fps=0 and rising drop
        counters → no demotion, and the engagement deadline stays frozen."""
        self.assertIsNone(self._clean())          # clean baseline sample
        drop = 0
        for i in range(10):
            self.clock.advance(1.0)
            drop += 30                            # 30 frames dropped / second
            act = self.wd.feed(sample(self.clock.t, vf=0.0, drop=drop,
                                      paused_for_cache=True, cbs=40))
            self.assertIsNone(act, f"buffering sample {i} demoted")
        self.assertEqual(self.wd._streak, 0, "streak grew during buffering")
        self.assertIsNone(self.wd.last_action)
        # the same 10 s on the engagement watcher: nothing applied, and the
        # deadline is exactly WATCH_S away — it did not tick down at all.
        w, applied, acts = self._watcher()
        w.arm(now=self.clock.t)
        for _ in range(10):
            self.clock.advance(1.0)
            self.assertIsNone(w.feed("no", now=self.clock.t, buffering=True))
        self.assertEqual(applied, [])
        self.assertEqual(acts, [])
        self.assertAlmostEqual(w._deadline,
                               self.clock.t + WD.EngagementWatcher.WATCH_S)
        # ... and the frozen time is not lost: the timeout still fires once
        # buffering ends and WATCH_S of NON-buffering time elapses.
        self.clock.advance(2.9)
        self.assertIsNone(w.feed("no", now=self.clock.t))
        self.clock.advance(0.2)
        self.assertEqual(w.feed("no", now=self.clock.t)["kind"], "fallback")

    def test_b_healthy_buffering_healthy_no_demotion(self):
        """(b) healthy → buffering → healthy → no demotion (a clean sample
        after the gap resets the streak as it always did)."""
        self.assertIsNone(self._clean())
        for _ in range(2):                        # streak 2, no action yet
            self.clock.advance(1.0)
            self.assertIsNone(self.wd.feed(sample(self.clock.t, vf=10.0)))
        self.assertEqual(self.wd._streak, 2)
        self.clock.advance(1.0)                   # buffering: frozen
        self.assertIsNone(self.wd.feed(sample(self.clock.t, vf=0.0,
                                              paused_for_cache=True, cbs=10)))
        self.assertEqual(self.wd._streak, 2)
        self.clock.advance(1.0)                   # healthy again: reset
        self.assertIsNone(self.wd.feed(sample(self.clock.t, vf=30.0)))
        self.assertEqual(self.wd._streak, 0)
        for _ in range(2):                        # two more: still short
            self.clock.advance(1.0)
            self.assertIsNone(self.wd.feed(sample(self.clock.t, vf=10.0)))
        self.assertEqual(self.wd._streak, 2)
        self.assertIsNone(self.wd.last_action)

    def test_c_overload_across_buffering_gap_still_demotes(self):
        """(c) overload, a buffering gap in the middle, overload resumes →
        demotion still fires: the streak is CONSECUTIVE ACROSS the frozen
        gap (the fine line the brief draws)."""
        self.assertIsNone(self._clean())
        self.clock.advance(1.0)
        self.assertIsNone(self.wd.feed(sample(self.clock.t, vf=10.0)))
        for _ in range(2):                        # gap: neither counts nor resets
            self.clock.advance(1.0)
            self.assertIsNone(self.wd.feed(sample(self.clock.t, vf=0.0,
                                                  paused_for_cache=True,
                                                  cbs=50)))
        self.assertEqual(self.wd._streak, 1)
        self.clock.advance(1.0)
        self.assertIsNone(self.wd.feed(sample(self.clock.t, vf=10.0)))
        self.clock.advance(1.0)
        act = self.wd.feed(sample(self.clock.t, vf=10.0))
        self.assertIsNotNone(act, "overload across a buffering gap must demote")
        self.assertEqual(act["kind"], "demote")
        self.assertEqual((act["from"], act["to"]), ("vaapi", "vaapi-copy"))

    def test_d_full_cache_overload_still_demotes(self):
        """(d) the positive control: 3 consecutive overloaded samples with a
        FULL cache (cache-buffering-state 100, not paused-for-cache) demote
        exactly as before — the freeze must not swallow real overload."""
        self.assertIsNone(self._clean(cbs=100))
        acts = []
        for _ in range(3):
            self.clock.advance(1.0)
            acts.append(self.wd.feed(sample(self.clock.t, vf=10.0, cbs=100)))
        self.assertIsNone(acts[0])
        self.assertIsNone(acts[1])
        self.assertEqual(acts[2]["kind"], "demote")
        self.assertEqual(self.wd._streak, 0)      # consumed by the action

    def test_e_cpu_hwdec_buffering_changes_nothing(self):
        """(e) buffering observed but hwdec-current is already CPU: nothing
        changes — no demote, no toast, streak untouched; and when buffering
        ends the pre-existing terminal CPU path still behaves as before."""
        self.assertIsNone(self.wd.feed(sample(self.clock.t, vf=30.0,
                                              hwdec="no")))
        for _ in range(6):
            self.clock.advance(1.0)
            self.assertIsNone(self.wd.feed(sample(
                self.clock.t, vf=10.0, drop=30, hwdec="no",
                paused_for_cache=True, cbs=20)))
        self.assertEqual(self.wd._streak, 0)
        self.assertIsNone(self.wd.last_action)
        # buffering over: the legacy terminal-toast path is untouched
        acts = []
        for _ in range(3):
            self.clock.advance(1.0)
            acts.append(self.wd.feed(sample(self.clock.t, vf=10.0, drop=30,
                                            hwdec="no", cbs=100)))
        self.assertEqual(acts[2]["kind"], "toast")

    def test_is_buffering_matrix(self):
        """The classifier itself, including the two edges that must not
        freeze the watchdog forever: a never-observed percentage is NOT
        buffering, and 100 (ready) is NOT buffering."""
        self.assertTrue(WD.is_buffering(True, None))
        self.assertTrue(WD.is_buffering(True, 100))
        self.assertTrue(WD.is_buffering(False, 99))
        self.assertTrue(WD.is_buffering(False, 0))
        self.assertTrue(WD.is_buffering(False, "42"))     # mpv gives ints
        self.assertFalse(WD.is_buffering(False, None))    # not observed yet
        self.assertFalse(WD.is_buffering(False, 100))     # cache ready
        self.assertFalse(WD.is_buffering(False, 1000))    # over-filled
        self.assertTrue(WD.is_buffering(False, 0.0))      # 0 % filled = filling
        self.assertFalse(WD.is_buffering(None, None))
        self.assertFalse(WD.is_buffering(False, "nonsense"))

    def test_sample_reports_its_own_buffering_state(self):
        self.assertTrue(sample(0.0, paused_for_cache=True).buffering())
        self.assertTrue(sample(0.0, cbs=10).buffering())
        self.assertFalse(sample(0.0, cbs=100).buffering())
        self.assertFalse(sample(0.0).buffering())

    def test_sampler_reads_the_observed_properties(self):
        """R17-D plumbing: the sampler takes buffering state from the
        SNAPSHOT only (never a synchronous getter — repo rule)."""
        class Core:
            snapshot = {"time-pos": 1.0, "duration": 10.0, "pause": False,
                        "idle-active": False, "eof-reached": False,
                        "seeking": False, "estimated-vf-fps": 30.0,
                        "container-fps": 30.0, "frame-drop-count": 0,
                        "decoder-frame-drop-count": 0,
                        "hwdec-current": "vaapi", "paused-for-cache": True,
                        "cache-buffering-state": 33}

        s = WD.Sampler(Core()).sample()
        self.assertTrue(s.paused_for_cache)
        self.assertEqual(s.cache_buffering_state, 33)
        self.assertTrue(s.buffering())
        Core.snapshot = dict(Core.snapshot, **{"paused-for-cache": False,
                                               "cache-buffering-state": 100})
        self.assertFalse(WD.Sampler(Core()).sample().buffering())
        Core.snapshot = dict(Core.snapshot, **{"cache-buffering-state": None})
        self.assertFalse(WD.Sampler(Core()).sample().buffering())

    def test_engagement_freeze_survives_a_fallback_step(self):
        """The deadline is re-armed after each ladder step, so the freeze
        has to hold for the re-armed deadline too."""
        w, applied, _acts = self._watcher("vaapi")
        w.arm(now=0.0)
        self.clock.advance(3.1)
        self.assertEqual(w.feed("no", now=self.clock.t)["kind"], "fallback")
        self.assertEqual(applied, ["vaapi-copy"])         # deadline re-armed
        for _ in range(5):                                # 5 s of buffering
            self.clock.advance(1.0)
            self.assertIsNone(w.feed("no", now=self.clock.t, buffering=True))
        self.assertEqual(applied, ["vaapi-copy"])         # no second step
        self.assertAlmostEqual(w._deadline,
                               self.clock.t + WD.EngagementWatcher.WATCH_S)
        self.clock.advance(3.1)
        # last ladder step → kind is "exhausted" (R2-P2 ladder semantics)
        self.assertEqual(w.feed("no", now=self.clock.t)["kind"], "exhausted")
        self.assertEqual(applied, ["vaapi-copy", "no"])


if __name__ == "__main__":
    unittest.main()
