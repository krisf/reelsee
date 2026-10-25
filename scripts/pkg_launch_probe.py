#!/usr/bin/python3
"""R4-P2 helper: launch the EXTRACTED .deb tree like a real installed user.

Runs the package's OWN /usr/share/reelsee modules (never the repo src/)
against a clean environment and proves the installed layout reaches
render-ready, renders frames and exits 0 — the exact thing the 0.1.0 deb
failed at on the user's machine (FINDINGS-INSTALL-CRASH: dev-only libmpv
path + no system fallback).

Launched by scripts/smoke_package.py inside headless-smoke.sh (Xvfb) with
env -u LD_LIBRARY_PATH -u REELSEE_LIBMPV, fresh HOME/XDG_* dirs; the repo
vendor/ tree is NOT reachable by construction (candidates derive from the
extracted file location) and the deb ships no vendor dir, so the pick must
be the SYSTEM libmpv2 — reported via SMOKE_CHECK:libmpv_kind.

Checks (SMOKE_CHECK lines, exit 0 only if all pass):
  pkg_tree            tree + reelsee.py exist, no vendor dir reachable
  libmpv_path/kind    which candidate the installed code chose
  pkg_window_open     one window exists
  pkg_render_ready    video visible: xwd crop mean>8 + time-pos>0
  pkg_frames_moving   time-pos advanced between samples (frames rendered)
  pkg_file_loaded     (--open-only) observed mpv 'filename' == target's
                      basename, within PKG_OPEN_TIMEOUT seconds
Exit codes: 0 pass · 1 check failure · 2 reelsee.run() returned 2 (no
libmpv, friendly failure) · 3 reelsee.run raised (crash — regression).
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

failures: list[str] = []


def check(name: str, ok: bool, value="") -> bool:
    print(f"SMOKE_CHECK:{name}={value}", flush=True)
    if not ok:
        failures.append(name)
        print(f"SMOKE_FAIL:{name}", flush=True)
    return bool(ok)


def finish() -> None:
    if failures:
        print("PKG_PROBE_FAILURES: " + ",".join(failures), flush=True)
        sys.exit(1)
    print("PKG_PROBE_PASSED", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tree", required=True, help="dpkg-deb -x root")
    ap.add_argument("--media", required=True)
    ap.add_argument("--capture", default="/tmp/pkg_probe.xwd")
    ap.add_argument("--open-only", action="store_true",
                    help="prove the file LOADS (observed filename matches) "
                         "and quit — for huge/slow sources where rendering "
                         "is not the point (R4-P4 real user file)")
    a = ap.parse_args()

    tree = Path(a.tree).resolve()
    share = tree / "usr" / "share" / "reelsee"
    check("pkg_tree", (share / "reelsee.py").exists(), str(share))
    check("pkg_tree_no_vendor",
          not (share / "vendor").exists()
          and not (tree / "usr" / "share" / "vendor").exists(),
          "repo vendor unreachable")
    if failures:
        finish()
        return 1

    # THE installed-layout import: package modules win over everything.
    sys.path.insert(0, str(share))
    import mpvlib as M
    try:
        p, kind = M.resolve_libmpv()
        check("libmpv_path", p.exists(), str(p))
        check("libmpv_kind", kind == "system", f"{kind} @ {p}")
    except Exception as e:                    # negative control: NOTHING
        kind = "unresolved"                   # resolves — let the APP show
        print(f"SMOKE_CHECK:libmpv_unresolved={type(e).__name__}",
              flush=True)                     # its friendly failure

    import reelsee

    state: dict = {}

    def mean_luma(xwd: str) -> float:
        out = subprocess.run(
            ["convert", xwd, "-crop", "640x360+0+0", "+repage",
             "-colorspace", "gray", "-format", "%[fx:mean*255]", "info:"],
            capture_output=True, text=True)
        try:
            return float(out.stdout.strip())
        except ValueError:
            return -1.0

    def step1(app):
        from gi.repository import GLib
        wins = app.windows
        check("pkg_window_open", len(wins) == 1, str(len(wins)))
        if not wins:
            app.quit()
            return False
        win = wins[0]
        state["win"] = win
        cap_ok = False
        with open(a.capture, "wb") as fh:
            cap_ok = subprocess.run(["xwd", "-root", "-silent"],
                                    stdout=fh).returncode == 0
        mean = mean_luma(a.capture) if cap_ok else -1
        t1 = float(win.core.snapshot.get("time-pos") or -1)
        check("pkg_render_ready", cap_ok and mean > 8 and t1 > 0,
              f"cap={cap_ok} mean={mean:.1f} timepos={t1:.2f}")
        state["t1"] = t1
        GLib.timeout_add(1500, lambda: step2(app))
        return False

    def step2(app):
        win = state.get("win")
        t1 = state.get("t1", -1.0)
        t2 = float(win.core.snapshot.get("time-pos") or -1) if win else -1.0
        check("pkg_frames_moving", t1 > 0 and t2 > t1, f"{t1:.2f}->{t2:.2f}")
        app.quit()
        return False

    def open_step1(app):
        from gi.repository import GLib
        wins = app.windows
        check("pkg_window_open", len(wins) == 1, str(len(wins)))
        if not wins:
            app.quit()
            return False
        win = wins[0]
        want = Path(a.media).name
        deadline = time.monotonic() + float(
            os.environ.get("PKG_OPEN_TIMEOUT", "75"))

        def poll():
            fn = win.core.snapshot.get("filename") or ""
            if fn == want:
                check("pkg_file_loaded", True, f"ok {fn[:60]!r}")
                app.quit()
                return False
            if time.monotonic() > deadline:
                check("pkg_file_loaded", False,
                      f"got={fn[:40]!r} want={want[:40]!r}")
                app.quit()
                return False
            GLib.timeout_add(500, poll)
            return False

        GLib.timeout_add(400, poll)
        return False

    def on_app(app):
        from gi.repository import GLib
        if a.open_only:
            GLib.timeout_add(2200, lambda: open_step1(app))
        else:
            GLib.timeout_add(2200, lambda: step1(app))

    try:
        rc = reelsee.run([str(share / "reelsee.py"), a.media], on_app=on_app)
    except Exception as e:                       # crash = regression, no trace
        print(f"PKG_PROBE_CRASH: {type(e).__name__}: {e}", flush=True)
        return 3
    check("pkg_exit_zero", rc == 0, f"rc={rc}")
    if rc == 2:
        return 2
    finish()
    return 0


if __name__ == "__main__":
    sys.exit(main())
