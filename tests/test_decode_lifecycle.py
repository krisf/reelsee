"""R7-B gates: per-video decode override LIFECYCLE.

The window methods run UNBOUND against a fake self (real Config, real
Chrome, real Watchdog, recording fake core) — the same recipe the R6
window tests use.  Gates: click-while-GPU forces CPU through the proven
apply_hwdec mechanism, suspends the watchdog (no demote AND no silent
promote/retry), the override is cleared by EVERY new file load, it is
never written to config (engine.decoder keeps pointing at the GPU
engine — a restart tries GPU), a CPU click re-attempts the configured
engine (live apply + fresh chain + engagement armed + watchdog re-armed),
and AUTOMATIC CPU marks "CPU (auto)", never the user override.
"""
import _bootstrap  # noqa: F401  (sys.path for src/)
import unittest
from types import SimpleNamespace

import engine as EN
import watchdog as WD
from chrome import Chrome
from window import ReelseeWindow


class RecCore:
    """Recording fake core: OBSERVED snapshot + apply_hwdec recorder."""

    def __init__(self, hwdec=None):
        self.snapshot = {"pause": False, "idle-active": False,
                         "eof-reached": False}
        if hwdec is not None:
            self.snapshot["hwdec-current"] = hwdec
        self.hwdec_calls = []
        self.commands = []

    def apply_hwdec(self, mode):
        self.hwdec_calls.append(mode)
        self.commands.append(("set", "hwdec", mode))

    def command(self, *a):
        self.commands.append(a)


GPU = "0000:13:00.0"


class FakeSelf:
    """Binds ONLY the decode-related window methods (unbound) — every
    other attribute is plain state; the recipe the R6 window tests use."""
    _toggle_decode = ReelseeWindow._toggle_decode
    _set_decode_flags = ReelseeWindow._set_decode_flags
    _clear_decode_override = ReelseeWindow._clear_decode_override
    _reattempt_engine = ReelseeWindow._reattempt_engine
    _arm_engine_watch = ReelseeWindow._arm_engine_watch
    _on_engagement = ReelseeWindow._on_engagement
    _watchdog_tick = ReelseeWindow._watchdog_tick


def make_self(hwdec=None, decoder=GPU, headless=False):
    """Fake `self` carrying exactly the window state the decode methods
    touch — REAL Config/Chrome/Watchdog, recording core."""
    cfg = _bootstrap.mini_cfg()
    cfg.set("engine.decoder", decoder)
    core = RecCore(hwdec=hwdec)
    win_stub = _bootstrap.mini_win()
    win_stub.core = core
    s = FakeSelf()
    s.cfg = cfg
    s.core = core
    s.chrome = Chrome(win_stub, cfg, on_decode=None)
    # wire Chrome's own snapshot view to the same recording core
    s.chrome.win = SimpleNamespace(core=core)
    s.watchdog = WD.Watchdog(clock=lambda: 0.0)
    s.watchdog.file_loaded()
    s._engagement = None
    s._decode_user = False
    s._decode_auto = False
    s._wd_sampler = WD.Sampler(core, simulate=False)
    s.app = SimpleNamespace(headless=headless, _engine_list_cached=[
        EN.Engine(id=GPU, label="iGPU", kind="vaapi", pci=GPU,
                  is_display_gpu=True),
        EN.Engine(id="cpu", label="CPU", kind="cpu")])
    s.toasts = []
    s.chrome.show_toast = lambda text: s.toasts.append(text)
    # record every config write in this fake's lifetime
    s.cfg_writes = []
    real_set = cfg.set

    def spy_set(key, value):
        s.cfg_writes.append((key, value))
        return real_set(key, value)
    cfg.set = spy_set
    return s


def snap(s, hwdec):
    s.core.snapshot["hwdec-current"] = hwdec
    s.chrome.update_snapshot(s.core.snapshot)
    return s.chrome.btn_decode.get_label()


class TestToggleToCpu(unittest.TestCase):
    def setUp(self):
        self.s = make_self(hwdec="vaapi(h264)")     # hardware REALLY on

    def test_forces_cpu_via_proven_mechanism(self):
        ReelseeWindow._toggle_decode(self.s)
        self.assertEqual(self.s.core.hwdec_calls, ["no"])
        self.assertTrue(self.s.toasts)               # toast shown

    def test_watchdog_suspended_while_override_lives(self):
        ReelseeWindow._toggle_decode(self.s)
        self.assertTrue(self.s.watchdog.suspended)
        # neither the demote path NOR the silent retry/promote path acts
        self.s.watchdog.set_engine(chain=WD.FallbackChain("vaapi"),
                                   is_cpu=False)
        clock = _Clock(100.0)
        self.s.watchdog.clock = clock
        self.s.watchdog.file_loaded()
        clock.advance(5.0)                           # out of grace
        prev = sample(clock.t, vf=5.0, hwdec="no")   # overloaded at "no"
        for _ in range(6):
            clock.advance(1.0)
            cur = sample(clock.t, vf=5.0, hwdec="no",
                         drop=prev.drop + 20)
            self.assertIsNone(self.s.watchdog.feed(cur))
            prev = cur

    def test_override_cleared_on_new_file_load(self):
        ReelseeWindow._toggle_decode(self.s)
        self.assertTrue(self.s._decode_user)
        ReelseeWindow._clear_decode_override(self.s)
        self.assertFalse(self.s._decode_user)
        self.assertFalse(self.s._decode_auto)
        self.assertFalse(self.s.watchdog.suspended)
        # next video: observed back to "no" reads plain CPU (not auto)
        self.assertEqual(snap(self.s, "no"), "CPU")

    def test_never_written_to_config(self):
        ReelseeWindow._toggle_decode(self.s)
        ReelseeWindow._clear_decode_override(self.s)
        ReelseeWindow._toggle_decode(self.s)         # and back? no CPU obs
        self.s.core.snapshot["hwdec-current"] = "no"
        ReelseeWindow._toggle_decode(self.s)         # re-attempt
        self.assertEqual(self.s.cfg_writes, [])      # ZERO config writes
        # engine.decoder STILL the configured GPU engine (restart → GPU)
        self.assertEqual(self.s.cfg.get("engine.decoder"), GPU)

    def test_engagement_disarmed_under_override(self):
        self.s._engagement = WD.EngagementWatcher(
            WD.FallbackChain("vaapi"), self.s.core.apply_hwdec)
        self.s._engagement.arm()
        ReelseeWindow._toggle_decode(self.s)
        self.assertFalse(self.s._engagement.armed)


class TestReattempt(unittest.TestCase):
    def setUp(self):
        self.s = make_self(hwdec="no")
        ReelseeWindow._set_decode_flags(self.s, True, False)
        self.s.watchdog.suspended = True

    def test_reapplies_configured_mode_and_rearms(self):
        clock = _Clock(0.0)
        self.s.watchdog.clock = clock
        ReelseeWindow._toggle_decode(self.s)
        # R2-P1/B1 default: the configured GPU engine maps to vaapi-copy
        self.assertEqual(self.s.core.hwdec_calls, ["vaapi-copy"])
        self.assertFalse(self.s.watchdog.suspended)
        self.assertFalse(self.s._decode_user)
        self.assertTrue(self.s._engagement is not None
                        and self.s._engagement.armed)     # verified again
        # re-armed watchdog: fresh 4 s grace, and after it the machinery
        # can fire again on a clean full 3-confirm (nothing survived the
        # suspended period)
        self.assertAlmostEqual(self.s.watchdog._grace_until, 4.0)
        prev = None
        acts = []
        for _ in range(6):
            clock.advance(1.0)
            cur = sample(clock.t, vf=5.0, hwdec="no",
                         drop=(prev.drop + 20) if prev else 0)
            acts.append(self.s.watchdog.feed(cur))
            prev = cur
        self.assertTrue(all(a is None for a in acts[:4]))   # grace+confirm
        self.assertEqual(acts[5]["kind"], "demote")
        self.assertEqual(acts[5]["to"], "no")        # fresh chain, downward

    def test_headless_reattempt_applies_cpu_only(self):
        s = make_self(hwdec="no", decoder="cpu", headless=True)
        ReelseeWindow._toggle_decode(s)
        self.assertEqual(s.core.hwdec_calls, ["no"])  # R13: no hardware
        self.assertFalse(s.watchdog.suspended)
        self.assertIsNone(s._engagement)

    def test_cpu_auto_click_is_a_reattempt_not_a_pin(self):
        # CPU reached automatically: click must TRY THE ENGINE AGAIN,
        # never pin CPU (no override flag, watchdog stays unsuspended).
        ReelseeWindow._set_decode_flags(self.s, False, True)
        self.s.watchdog.suspended = False
        snap(self.s, "no")
        self.assertEqual(self.s.chrome.btn_decode.get_label(), "CPU (auto)")
        ReelseeWindow._toggle_decode(self.s)
        self.assertFalse(self.s._decode_user)
        self.assertFalse(self.s._decode_auto)
        self.assertEqual(self.s.core.hwdec_calls, ["vaapi-copy"])


class TestAutomaticCpuMarker(unittest.TestCase):
    def test_engagement_fallback_marks_auto_not_user(self):
        s = make_self(hwdec="no")
        ReelseeWindow._on_engagement(
            s, {"kind": "exhausted", "from": "vaapi", "to": "no"})
        self.assertTrue(s._decode_auto)
        self.assertFalse(s._decode_user)
        self.assertEqual(snap(s, "no"), "CPU (auto)")
        # a user toggle meanwhile outranks the marker
        ReelseeWindow._set_decode_flags(s, True, True)
        self.assertEqual(snap(s, "no"), "CPU")

    def test_engagement_gaveup_marks_auto(self):
        s = make_self(hwdec="no")
        ReelseeWindow._on_engagement(s, {"kind": "gave-up", "mode": "vaapi"})
        self.assertTrue(s._decode_auto)

    def test_watchdog_demote_to_no_marks_auto(self):
        s = make_self(hwdec="no")
        action = {"kind": "demote", "from": "vaapi-copy", "to": "no",
                  "fps_ratio": 0.5, "drop_ratio": 0.1}
        # drive the real tick path (apply_action writes last_fallback —
        # config, allowed: that is the watchdog's own record, not the
        # user override)
        s._wd_id = None
        s._closing = False
        s.watchdog.feed = lambda _sample: action
        self.assertTrue(ReelseeWindow._watchdog_tick(s))
        self.assertTrue(s._decode_auto)
        self.assertFalse(s._decode_user)
        self.assertEqual(snap(s, "no"), "CPU (auto)")


class _Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


def sample(t, *, vf, cfps=30.0, drop=0, dec=0, hwdec="vaapi"):
    return WD.Sample(t=t, playing=True, pause=False, seeking=False,
                     idle=False, eof=False, vf_fps=vf, container_fps=cfps,
                     drop=drop, dec_drop=dec, hwdec=hwdec)


class TestSuspendedSemantics(unittest.TestCase):
    """Pure-watchdog half of the B gate: suspended ⇒ no demote, no
    retry-promote, baseline kept fresh, and feeding again after release
    starts clean (no stale streak)."""

    def test_suspended_blocks_everything_then_rearms(self):
        clock = _Clock(100.0)
        wd = WD.Watchdog(clock=clock)
        wd.file_loaded()
        clock.advance(5.0)
        wd.suspended = True
        prev = sample(clock.t, vf=5.0, hwdec="vaapi")
        for _ in range(8):
            clock.advance(1.0)
            cur = sample(clock.t, vf=5.0, hwdec="vaapi",
                         drop=prev.drop + 20)
            self.assertIsNone(wd.feed(cur))
            prev = cur
        wd.suspended = False
        # fresh 3-confirm needed (stale streak gone): first two post-
        # release samples do not act
        acts = []
        for _ in range(3):
            clock.advance(1.0)
            cur = sample(clock.t, vf=5.0, hwdec="vaapi",
                         drop=prev.drop + 20)
            acts.append(wd.feed(cur))
            prev = cur
        self.assertIsNone(acts[0])
        self.assertIsNone(acts[1])
        self.assertEqual(acts[2]["kind"], "demote")


if __name__ == "__main__":
    unittest.main()
