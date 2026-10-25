#!/usr/bin/env /usr/bin/python3
"""P1 live gate: create + initialize + tear down a real vendored libmpv handle.

Runs headless (vo=libmpv, hwdec=no — R13) via scripts/headless-smoke.sh.
Prints SMOKE_CHECK lines (§5.7) and exits non-zero on any failure.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import locale  # noqa: E402

import mpvlib  # noqa: E402

INIT_OPTS = {
    "vo": "libmpv",            # PLAN §0.2 — mandatory for the render API
    "gpu-api": "opengl",       # PLAN §0.2
    "hwdec": "no",             # PLAN R13 — headless gates never use hw decode
    "terminal": "no",
    "msg-level": "all=warn",
    "config": "no",            # never read ~/.config/mpv
    "keep-open": "yes",
    "ao": "null",
    "osc": "no",
    "osd-level": "0",
}


def check(name: str, ok: bool, value: str = "") -> None:
    print(f"SMOKE_CHECK:{name}={value if value else ('true' if ok else 'false')}")
    if not ok:
        print(f"SMOKE_FAIL:{name}", file=sys.stderr)
        sys.exit(1)


def main() -> int:
    locale.setlocale(locale.LC_NUMERIC, "C")  # §0.1
    lib = mpvlib.load_libmpv()
    check("libmpv_loaded", True)

    major, minor = mpvlib.client_api_version(lib)
    check("client_api", (major, minor) >= (2, 0), f"{major}.{minor}")

    locale.setlocale(locale.LC_NUMERIC, "C")  # at the creation site (§0.1)
    h = mpvlib.ctypes.c_void_p(lib.mpv_create())
    if not h:
        check("mpv_create", False, "null handle")
    check("mpv_create", True)
    try:
        for k, v in INIT_OPTS.items():
            rc = lib.mpv_set_option_string(h, k.encode(), v.encode())
            if rc < 0:
                check(f"set_option_{k}", False, mpvlib.error_string(lib, rc))
        rc = lib.mpv_initialize(h)
        check("mpv_initialize", rc >= 0, str(rc))

        # Round-trip: worker-context sync getter is fine HERE — this is a
        # plain script, not the GTK main loop (R9 discipline lives in the
        # app). P9 moved pointer marshalling into mpvlib.get_property_string.
        ver = mpvlib.get_property_string(lib, h, "mpv-version")
        check("mpv_version", bool(ver), ver or "")

        # Round-trip of the ASYNC command path the app actually uses (R9):
        # submit "set volume 44" asynchronously, wait for its COMMAND_REPLY,
        # then confirm via a sync getter — this loop is a plain script, not
        # the GTK main loop (the R9 discipline lives in the app).
        rc = lib.mpv_command_async(h, 11, mpvlib.make_command("set", "volume", "44"))
        check("command_async_submit", rc >= 0, str(rc))
        t0 = time.time()
        replied = None
        while time.time() - t0 < 5.0:
            ev = lib.mpv_wait_event(h, 0.05)
            if ev and ev.contents.event_id == mpvlib.MPV_EVENT_COMMAND_REPLY \
                    and ev.contents.reply_userdata == 11:
                replied = ev.contents.error
                break
        check("command_async_reply", replied is not None and replied >= 0,
              str(replied))
        vol = mpvlib.get_property_string(lib, h, "volume")
        vol_f = float(vol) if vol else -1.0
        check("async_write_visible", abs(vol_f - 44.0) < 0.01, vol or "")
    finally:
        lib.mpv_terminate_destroy(h)
    check("roundtrip", True)
    print("MPV_ROUNDTRIP_OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
