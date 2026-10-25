#!/usr/bin/env python3
"""OPERATOR probe (not part of the DSH build): does the app's own engine block
actually engage hardware decode on the LIVE display?

Uses src/engine.mpv_init_options + src/mpvcore.MpvCore + src/render.RenderView —
the shipped code path, real GL surface, real VAAPI.  Prints SMOKE_CHECK lines.

usage: op_live_hwdec.py <engine-id> [file]
"""
from __future__ import annotations

import sys
import time

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk  # noqa: E402

sys.path.insert(0, "src")
import engine  # noqa: E402
import mpvlib  # noqa: E402
from mpvcore import MpvCore  # noqa: E402
from render import RenderView  # noqa: E402

ENGINE = sys.argv[1] if len(sys.argv) > 1 else "0000:13:00.0"
CLIP = sys.argv[2] if len(sys.argv) > 2 else "testdata/perf_1080p30.mp4"


class Cfg(dict):
    def get(self, k, d=None):
        return dict.get(self, k, d)


class App(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="pro.krisforbes.Reelsee.OpProbe")
        self.results = {}

    def do_activate(self):
        cfg = Cfg({
            "engine.decoder": ENGINE,
            "engine.cpu_threads": 0,
            "playback.keep_open": True,
            "mpv.options": {},
        })
        opts = engine.mpv_init_options(cfg, headless=False)
        if len(sys.argv) > 3:                 # operator override: hwdec mode
            opts["hwdec"] = sys.argv[3]
        lib = mpvlib.load_libmpv()
        self.lib = lib
        self.core = MpvCore(lib, opts)
        self.opts = opts

        win = Gtk.ApplicationWindow(application=self)
        win.set_default_size(640, 360)
        self.rv = RenderView(lib, self.core,
                             on_ready=lambda rv: self.core.loadfile(CLIP))
        win.set_child(self.rv.area)
        win.present()
        self.win = win

        GLib.timeout_add(10, self._poll)
        GLib.timeout_add_seconds(6, self._report)

    def _poll(self) -> bool:
        self.rv.poll()
        self.core.poll()
        return True

    def _report(self) -> bool:
        snap = dict(self.core.snapshot)
        direct = mpvlib.get_property_string(self.lib, self.core.h, "hwdec-current")
        print(f"SMOKE_CHECK:engine={ENGINE}")
        print(f"SMOKE_CHECK:requested_hwdec={self.opts.get('hwdec')}")
        print(f"SMOKE_CHECK:vaapi_device={self.opts.get('vaapi-device')}")
        print(f"SMOKE_CHECK:vo={self.opts.get('vo')} gpu_api={self.opts.get('gpu-api')}")
        print(f"SMOKE_CHECK:engaged_direct={direct!r}")
        print(f"SMOKE_CHECK:engaged_observed={snap.get('hwdec-current')!r}")
        print(f"SMOKE_CHECK:vf_fps={snap.get('estimated-vf-fps')!r}")
        print(f"SMOKE_CHECK:renders={self.rv.render_count}")
        print(f"SMOKE_CHECK:time_pos={snap.get('time-pos')!r}")
        self.rv.teardown()
        self.core.destroy()
        self.quit()
        return False


if __name__ == "__main__":
    sys.exit(App().run([]))
