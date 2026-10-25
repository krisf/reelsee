"""R9-A gates: the repeat tri-state PERSISTS.

Four unit gates straight from the ROUND9 brief —
  1. `_set_repeat_mode` writes BOTH config keys for all three modes
     (and, PERSIST_NOW, they hit the disk immediately like mute);
  2. the pre-load push (`_push_all_settings`, spied) RE-APPLIES the
     persisted values — the exact path that used to erase the mode;
  3. `repeat_props()` / `repeat_mode()` are exact inverses, including
     through a config write/re-read roundtrip;
  4. an unrecognised stored value maps to "off" for display WITHOUT
     rewriting config (in memory and on disk).

Plus the closed native-key hole: `_sync_repeat` — the snapshot landing
point the raw `l`/`r` cycle reaches — persists the derived mode, while
the `_set_repeat_mode` path never double-writes; and the engine seeds
the mpv INIT options from config (restart without an "off" flicker).

No display, no mpv: real Config over a scratch dir + stub core, real
methods invoked unbound (the two-process restart proof lives in
scripts/smoke_repeatpersist.py).
"""
import _bootstrap  # noqa: F401  (sys.path for src/)

import json
import tempfile
import unittest

import config


class _Core:
    def __init__(self, snapshot=None):
        self.snapshot = dict(snapshot or {})
        self.props = []
        self.commands = []

    def set_property(self, name, value):
        self.props.append((name, value))
        self.snapshot[name] = value

    def command(self, *args):
        self.commands.append(args)


class _Chrome:
    def __init__(self):
        self.osd = []
        self.repeats = []

    def show_osd(self, text, *a, **k):
        self.osd.append(text)

    def set_repeat(self, mode):
        self.repeats.append(mode)


class _Action:
    def __init__(self):
        self.states = []

    def set_state(self, variant):
        self.states.append(variant.unpack())


class _Win:
    """bare object carrying exactly the attributes the repeat writers
    touch — the REAL methods run unbound."""

    def __init__(self, cfg, core, rep_mode="off"):
        self.cfg = cfg
        self.core = core
        self.chrome = _Chrome()
        self._rep_mode = rep_mode
        self._transient_muted = False    # _push_all_settings reads it
        self._ctx_repeat = _Action()
        self.set_calls = []
        real = cfg.set

        def spy_set(dotted, value, **kw):
            self.set_calls.append((dotted, value))
            return real(dotted, value, **kw)

        cfg.set = spy_set


class _Cfg:
    """dotted get() only — engine/loop seeding never touches the rest."""

    def __init__(self, d):
        self.d = d

    def get(self, key, default=None):
        node = self.d
        for part in key.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node


class _TmpCfgCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory(dir=str(_bootstrap.TMP_ROOT))
        import pathlib
        self.path = pathlib.Path(self.dir.name) / "config.json"
        self.cfg = config.Config(self.path)

    def tearDown(self):
        self.dir.cleanup()

    def disk(self):
        with open(self.path, encoding="utf-8") as fh:
            return json.load(fh)


class TestCycleWritesConfig(_TmpCfgCase):
    """Gate 1: the single writer persists BOTH keys in all three modes."""

    def setUp(self):
        super().setUp()
        from window import ReelseeWindow
        from chrome import REPEAT_MODES, repeat_props
        self.writer = ReelseeWindow._set_repeat_mode
        self.REPEAT_MODES = REPEAT_MODES
        self.repeat_props = repeat_props

    def test_cycle_writes_both_config_keys_all_modes(self):
        for mode in self.REPEAT_MODES:
            core = _Core()
            w = _Win(self.cfg, core)
            self.writer(w, mode)
            lf, lp = self.repeat_props(mode)
            self.assertEqual(self.cfg.get("playback.loop_file"), lf, mode)
            self.assertEqual(self.cfg.get("playback.loop_playlist"), lp,
                             mode)
            # the mpv commands stay written too (display truth unchanged)
            self.assertIn(("set", "loop-file", lf), core.commands)
            self.assertIn(("set", "loop-playlist", lp), core.commands)

    def test_disk_reaches_immediately_persist_now(self):
        # "survives a restart, like mute does" — no debounce race:
        self.writer(_Win(self.cfg, _Core()), "one")
        pb = self.disk()["playback"]
        self.assertEqual(pb["loop_file"], "inf")
        self.assertEqual(pb["loop_playlist"], "no")
        self.writer(_Win(self.cfg, _Core()), "all")
        pb = self.disk()["playback"]
        self.assertEqual((pb["loop_file"], pb["loop_playlist"]),
                         ("no", "inf"))
        self.writer(_Win(self.cfg, _Core()), "off")
        pb = self.disk()["playback"]
        self.assertEqual((pb["loop_file"], pb["loop_playlist"]),
                         ("no", "no"))

    def test_action_state_still_updated(self):
        w = _Win(self.cfg, _Core())
        from gi.repository import GLib
        self.writer(w, "one", action=w._ctx_repeat)
        self.assertEqual(w._ctx_repeat.states, ["one"])
        self.assertEqual(w._rep_mode, "one")


class TestLoadReapplies(_TmpCfgCase):
    """Gate 2: the pre-load PROPERTY_MAP push now RE-APPLIES the mode
    instead of erasing it (spy on every property write)."""

    def setUp(self):
        super().setUp()
        from window import ReelseeWindow
        self.push = ReelseeWindow._push_all_settings

    def test_pushes_persisted_one(self):
        self.cfg.set("playback.loop_file", "inf")
        self.cfg.set("playback.loop_playlist", "no")
        core = _Core()
        w = _Win(self.cfg, core)
        self.push(w)
        self.assertIn(("loop-file", "inf"), core.props)
        self.assertIn(("loop-playlist", "no"), core.props)

    def test_pushes_persisted_all(self):
        self.cfg.set("playback.loop_file", "no")
        self.cfg.set("playback.loop_playlist", "inf")
        core = _Core()
        self.push(_Win(self.cfg, core))
        self.assertIn(("loop-file", "no"), core.props)
        self.assertIn(("loop-playlist", "inf"), core.props)

    def test_defaults_push_no_no(self):
        # documents the (now harmless) clobber source: the push mirrors
        # CONFIG, so with defaults it pushes no/no — with the persisted
        # mode it pushes the mode (tests above).
        core = _Core()
        self.push(_Win(self.cfg, core))
        self.assertIn(("loop-file", "no"), core.props)
        self.assertIn(("loop-playlist", "no"), core.props)


class TestInverses(unittest.TestCase):
    """Gate 3: repeat_props()/repeat_mode() are exact inverses — in both
    directions on the canonical triples, and through a config roundtrip."""

    def test_mode_of_props_is_identity(self):
        from chrome import REPEAT_MODES, repeat_mode, repeat_props
        for m in REPEAT_MODES:
            self.assertEqual(repeat_mode(*repeat_props(m)), m, m)

    def test_props_of_mode_is_identity(self):
        from chrome import REPEAT_MODES, repeat_mode, repeat_props
        for m in REPEAT_MODES:
            lf, lp = repeat_props(m)
            self.assertEqual(repeat_props(repeat_mode(lf, lp)), (lf, lp), m)

    def test_config_roundtrip_is_lossless(self):
        # mode → config values → coerced push values → mode again
        from chrome import REPEAT_MODES, repeat_mode, repeat_props
        for m in REPEAT_MODES:
            lf, lp = repeat_props(m)
            push = tuple(config.coerce("strno", v) for v in (lf, lp))
            self.assertEqual(repeat_mode(*push), m, m)


class TestUnknownValue(_TmpCfgCase):
    """Gate 4: an unrecognised stored value shows "off" and rewrites
    NOTHING — config dict and the on-disk file both untouched."""

    def setUp(self):
        super().setUp()
        from window import ReelseeWindow
        self.sync = ReelseeWindow._sync_repeat

    def test_repeat_mode_maps_unknown_to_off(self):
        from chrome import repeat_mode
        self.assertEqual(repeat_mode("banana", "banana"), "off")
        self.assertEqual(repeat_mode("", None), "off")

    def test_sync_displays_off_without_config_rewrite(self):
        self.cfg.set("playback.loop_file", "banana")
        self.cfg.set("playback.loop_playlist", "banana")
        before = self.disk()
        core = _Core({"loop-file": "banana", "loop-playlist": "banana"})
        w = _Win(self.cfg, core)                 # _rep_mode "off"
        self.sync(w)
        self.assertEqual(self.cfg.get("playback.loop_file"), "banana")
        self.assertEqual(self.cfg.get("playback.loop_playlist"), "banana")
        self.assertEqual(self.disk(), before)    # file byte-for-byte same
        self.assertEqual(w._ctx_repeat.states, [])   # radio untouched
        self.assertEqual(w._rep_mode, "off")

    def test_unknown_value_seed_derives_off(self):
        # the chrome/radio seeds are this same pure derivation:
        from chrome import repeat_mode
        seed = repeat_mode(self.cfg.get("playback.loop_file"),
                           self.cfg.get("playback.loop_playlist"))
        self.assertEqual(seed, "off")


class TestKeyHoleClosed(_TmpCfgCase):
    """The raw `l`/`r` cycle never calls _set_repeat_mode; the snapshot
    landing point `_sync_repeat` must persist whatever mpv cycled to,
    and the button/radio path must NOT double-write from the same
    change."""

    def setUp(self):
        super().setUp()
        from window import ReelseeWindow
        self.writer = ReelseeWindow._set_repeat_mode
        self.sync = ReelseeWindow._sync_repeat

    def test_cycle_loop_file_persists_one(self):
        core = _Core({"loop-file": "1", "loop-playlist": "no"})
        w = _Win(self.cfg, core)
        self.sync(w)                             # l pressed: no -> 1
        self.assertEqual(w._rep_mode, "one")
        self.assertEqual(w._ctx_repeat.states, ["one"])
        self.assertEqual(self.cfg.get("playback.loop_file"), "inf")
        self.assertEqual(self.cfg.get("playback.loop_playlist"), "no")
        pb = self.disk()["playback"]
        self.assertEqual((pb["loop_file"], pb["loop_playlist"]),
                         ("inf", "no"))

    def test_cycle_loop_playlist_persists_all(self):
        core = _Core({"loop-file": "no", "loop-playlist": "1"})
        w = _Win(self.cfg, core)
        self.sync(w)                             # r pressed: no -> 1
        self.assertEqual(w._rep_mode, "all")
        pb = self.disk()["playback"]
        self.assertEqual((pb["loop_file"], pb["loop_playlist"]),
                         ("no", "inf"))

    def test_writer_path_does_not_double_persist(self):
        w = _Win(self.cfg, _Core())
        self.writer(w, "one")                    # button/radio path
        n = len(w.set_calls)
        self.assertEqual(n, 2)                   # exactly the two keys
        w.core.snapshot = {"loop-file": "inf", "loop-playlist": "no"}
        self.sync(w)                             # the echo of that write
        self.assertEqual(len(w.set_calls), n)    # no second write

    def test_repeat_count_canonicalises_on_disk(self):
        # mpv cycles to "2" — display already "one", disk stores "inf"
        core = _Core({"loop-file": "inf", "loop-playlist": "no"})
        w = _Win(self.cfg, core)
        self.sync(w)
        core.snapshot["loop-file"] = "2"         # l again: inf -> 2
        self.sync(w)
        self.assertEqual(w._rep_mode, "one")     # unchanged, still one
        self.assertEqual(self.cfg.get("playback.loop_file"), "inf")


class TestInitOptionSeed(unittest.TestCase):
    """Restart without the flicker: the persisted value is an mpv INIT
    option, so the very first observed property already matches the
    seeded icon/radio; invalid stored values are skipped, never forced."""

    def test_valid_loop_value(self):
        import engine
        self.assertEqual(engine.valid_loop_value("inf"), "inf")
        self.assertEqual(engine.valid_loop_value("force"), "force")
        self.assertEqual(engine.valid_loop_value("3"), "3")
        self.assertIsNone(engine.valid_loop_value("no"))
        self.assertIsNone(engine.valid_loop_value(""))
        self.assertIsNone(engine.valid_loop_value(None))
        self.assertIsNone(engine.valid_loop_value("banana"))
        self.assertIsNone(engine.valid_loop_value(-1))

    def _opts(self, playback, mpv_opts=None):
        import engine
        cfg = _Cfg({"engine": {"decoder": "cpu"},
                    "playback": playback,
                    "mpv": {"options": mpv_opts or {}}})
        cpu = engine.Engine(id="cpu", label="CPU", kind="cpu")
        return engine.mpv_init_options(cfg, True, 1,
                                       "/nonexistent-sys", "/nonexistent-dev",
                                       engine_override=cpu)

    def test_persisted_one_seeds_init_option(self):
        o = self._opts({"loop_file": "inf", "loop_playlist": "no"})
        self.assertEqual(o["loop-file"], "inf")
        self.assertNotIn("loop-playlist", o)     # "no" == mpv default

    def test_unrecognised_stored_value_is_skipped(self):
        o = self._opts({"loop_file": "banana", "loop_playlist": "-1"})
        self.assertNotIn("loop-file", o)
        self.assertNotIn("loop-playlist", o)

    def test_raw_mpv_options_still_win(self):
        o = self._opts({"loop_file": "inf"},
                       {"loop-file": "no"})
        self.assertEqual(o["loop-file"], "no")


if __name__ == "__main__":
    unittest.main()
