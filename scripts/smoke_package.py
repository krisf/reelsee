#!/usr/bin/env python3
"""P10/P11 gate smoke — R4-P2 rewrite: the package gate must LAUNCH the
package, not just build it (FINDINGS-INSTALL-CRASH P3).

The 0.1.0 deb shipped, extracted, and scored SMOKE_ASSERTS_PASSED while the
installed app died on `FileNotFoundError: libmpv not found at
/usr/share/vendor/...` — because this gate only ever asserted dpkg-deb
builds and the tree extracts.  Now it also extracts the .deb into scratch
and RUNS the extracted tree like a real user would:

  * env -u LD_LIBRARY_PATH -u REELSEE_LIBMPV (clean loader env)
  * fresh HOME + XDG_CONFIG/STATE/CACHE/DATA dirs (first-run experience)
  * headless (REELSEE_HEADLESS=1, hwdec=no via REELSEE_HEADLESS engine
    defaults), inside headless-smoke.sh's Xvfb
  * repo vendor/ NOT reachable by construction: candidates derive from the
    extracted mpvlib.py location; the deb ships no vendor dir, so the
    chosen libmpv MUST be the system libmpv2 (the deb's Depends:)

and a NEGATIVE CONTROL proves the launch check fails (cannot pass
vacuously) on a host where neither a system libmpv nor a shipped vendor
dir exists — simulated with a bogus REELSEE_LIBMPV plus a PATH/ctypes-
neutered shim.

Checks (machine lines via smoke_harness.check):
  build_ok deb_exists control_fields data_paths desktop_entry      (old)
  wrapper_smoke wrapper_installed_tree no_vendor_in_deb            (old+)
  pkg_launch_ok          extracted tree reaches render-ready, renders
                         frames (pkg_probe SMOKE lines), exits 0
  pkg_uses_system_libmpv chosen candidate is the system libmpv2
  negctl_shim_effective  shim really hides every libmpv discovery route
  negctl_gate_fails      same launch FAILS when no libmpv is available
  negctl_friendly_error  failure is friendly: rc 2, remedies + searched
                         paths on stderr, no Python traceback (R4-P3)
"""
import configparser
import os
import shutil
import subprocess
import sys
from pathlib import Path

from smoke_harness import ROOT as REPO, SCRATCH, check, finish

import mpvlib

DEB = os.path.join("buildout", "reelsee_0.1.0_all.deb")
ROOT = os.path.join(SCRATCH, "pkg-root")
PY = "/usr/bin/python3"
PROBE = os.path.join(REPO, "scripts", "pkg_launch_probe.py")
MEDIA = os.path.join(REPO, "testdata", "smoke_360p.mp4")
os.environ.setdefault("TMPDIR", os.path.abspath(
    os.path.join(SCRATCH, "..", "dpkg-tmp")))
os.makedirs(os.environ["TMPDIR"], exist_ok=True)


def fresh_home(path: str) -> dict:
    """Empty HOME + XDG dirs — the deb's first-run environment."""
    shutil.rmtree(path, ignore_errors=True)
    home = Path(path)
    env = dict(os.environ)
    for k in ("LD_LIBRARY_PATH", "REELSEE_LIBMPV", "REELSEE_CONFIG_DIR",
              "REELSEE_STATE_DIR", "REELSEE_FAKE_LATENCY_MS",
              "REELSEE_YTDLP_BIN", "REELSEE_MPV_CLI", "REELSEE_DEBUG",
              "REELSEE_SMOKE_INTERP"):
        env.pop(k, None)
    for sub in (".config", ".local/state", ".local/share", ".cache"):
        (home / sub).mkdir(parents=True, exist_ok=True)
    env.update(HOME=str(home),
               XDG_CONFIG_HOME=str(home / ".config"),
               XDG_STATE_HOME=str(home / ".local/state"),
               XDG_CACHE_HOME=str(home / ".cache"),
               XDG_DATA_HOME=str(home / ".local/share"),
               REELSEE_HEADLESS="1")
    return env


def run_probe(env: dict, tag: str, timeout: int = 90,
              strip_lib_env: bool = True):
    """Launch the extracted tree through the PROBE.  The positive gate runs
    with env -u LD_LIBRARY_PATH -u REELSEE_LIBMPV (no library env at all,
    like a fresh install); the negative control needs its BOGUS
    REELSEE_LIBMPV to survive into the child (strip_lib_env=False)."""
    cmd = ["/usr/bin/env"]
    if strip_lib_env:
        cmd += ["-u", "LD_LIBRARY_PATH", "-u", "REELSEE_LIBMPV"]
    cmd += [PY, PROBE, "--tree", ROOT, "--media", MEDIA,
            "--capture", os.path.join(SCRATCH, f"pkg_launch_{tag}.xwd")]
    try:
        r = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True,
                           text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        class R:
            returncode, stdout, stderr = 124, "", "TIMEOUT"
        r = R()
    print(f"---- pkg launch ({tag}) ----")
    print(r.stdout, end="")
    if r.stderr:
        print("---- stderr ----")
        print("\n".join(r.stderr.splitlines()[-25:]))
    return r


def build_negctl_shim() -> dict:
    """Simulate a host with NO libmpv2 and NO shipped vendor dir:
    * PATH shim: cc/gcc/ld/ldconfig all fail (kills find_library's
      subprocess probes),
    * PYTHONPATH sitecustomize: patches ctypes.util.find_library -> None
      (Python 3.14 calls /sbin/ldconfig by ABSOLUTE path, so PATH alone
      does not neuter it) and masks the EXISTENCE of the exact system
      libmpv file paths from pathlib — the hardcoded fallback must not
      find a phantom library on the shimmed host."""
    base = Path(SCRATCH) / "negctl-shim"
    shutil.rmtree(base, ignore_errors=True)
    (base / "bin").mkdir(parents=True)
    for tool in ("cc", "gcc", "ld", "ldconfig", "objdump"):
        p = base / "bin" / tool
        p.write_text("#!/bin/sh\nexit 1\n")
        p.chmod(0o755)
    mask = os.pathsep.join(mpvlib.SYSTEM_PATHS)
    (base / "py").mkdir()
    (base / "py" / "sitecustomize.py").write_text(
        '"""Negative-control shim (smoke_package only): no libmpv exists."""\n'
        "import ctypes.util, os, pathlib\n"
        "ctypes.util.find_library = lambda name: None\n"
        f'_MASK = [m for m in "{mask}".split(os.pathsep) if m]\n'
        "_orig = pathlib.Path.exists\n"
        "def _exists(self, follow_symlinks=True):\n"
        "    s = os.path.abspath(str(self))\n"
        "    if any(s == m or s.startswith(m.rstrip('/') + '/') for m in _MASK):\n"
        "        return False\n"
        "    return _orig(self, follow_symlinks=follow_symlinks)\n"
        "pathlib.Path.exists = _exists\n")
    env = fresh_home(os.path.join(SCRATCH, "pkg-root", "home-negctl"))
    env.update(PATH=str(base / "bin"), PYTHONPATH=str(base / "py"))
    return env


def main() -> int:
    r = subprocess.run(["bash", "packaging/build-deb.sh"],
                       capture_output=True, text=True)
    check("build_ok", r.returncode == 0,
          (r.stderr or r.stdout).strip().splitlines()[-1:] and
          (r.stderr or r.stdout).strip().splitlines()[-1][:120] or "rc=0")
    check("deb_exists", os.path.exists(DEB), DEB)
    if not os.path.exists(DEB):
        finish()
        return 1

    r = subprocess.run(["dpkg-deb", "-I", DEB], capture_output=True,
                       text=True)
    ctrl = {}
    for line in r.stdout.splitlines():
        line = line.strip()
        if ": " in line:
            k, v = line.split(": ", 1)
            ctrl.setdefault(k, v)
    check("control_fields",
          ctrl.get("Package") == "reelsee"
          and ctrl.get("Version") == "0.1.0"
          and ctrl.get("Architecture") == "all"
          and "libmpv2" in ctrl.get("Depends", "")
          and "python3-gi" in ctrl.get("Depends", "")
          and "Maintainer" in ctrl,
          f"pkg={ctrl.get('Package')} ver={ctrl.get('Version')} "
          f"arch={ctrl.get('Architecture')}")

    r = subprocess.run(["dpkg-deb", "-c", DEB], capture_output=True, text=True)
    paths = [ln.split()[-1] for ln in r.stdout.splitlines() if ln]
    want = ["./usr/bin/reelsee",
            "./usr/share/reelsee/reelsee.py",
            "./usr/share/reelsee/mpvlib.py",
            "./usr/share/reelsee/reelsee_io.py",
            "./usr/share/applications/pro.krisforbes.Reelsee.desktop",
            "./usr/share/icons/hicolor/scalable/apps/reelsee.svg",
            "./usr/share/doc/reelsee/copyright",
            "./usr/share/doc/reelsee/README.md"]
    missing = [w for w in want if w not in paths]
    check("data_paths", not missing and len(paths) >= 6,
          f"paths={len(paths)} missing={missing or 'none'}")
    # R4 contract: the deb relies on the SYSTEM libmpv2 (Depends:) and
    # ships NO vendor tree — the launch below proves that combination works.
    vendored = [p for p in paths if "/vendor/" in p]
    check("no_vendor_in_deb", not vendored, f"vendor_entries={len(vendored)}")

    cp = configparser.RawConfigParser()
    cp.optionxform = str            # desktop keys are case-sensitive (Name, Exec)
    cp.read("data/pro.krisforbes.Reelsee.desktop", encoding="utf-8")
    de = dict(cp["Desktop Entry"])
    check("desktop_entry",
          all(de.get(k) for k in ("Type", "Name", "GenericName", "Exec",
                                  "Icon", "MimeType"))
          and de["Exec"] == "reelsee %U"
          and de["Type"] == "Application"
          and de["Terminal"] == "false"
          and "video/mp4" in de["MimeType"]
          and "video/x-matroska" in de["MimeType"]
          and de.get("X-Reelsee-Version") == "1",
          f"name={de.get('Name')} exec={de.get('Exec')}")

    shutil.rmtree(ROOT, ignore_errors=True)
    os.makedirs(ROOT, exist_ok=True)
    r = subprocess.run(["dpkg-deb", "-x", DEB, ROOT], capture_output=True,
                       text=True)
    env = fresh_home(os.path.join(ROOT, "home"))
    env["REELSEE_SMOKE_INTERP"] = "/bin/true"
    w = subprocess.run([os.path.join(ROOT, "usr/bin/reelsee"),
                        "some-video.mp4", "--", "--start=1"],
                       env=env, capture_output=True, text=True)
    check("wrapper_smoke", w.returncode == 0, f"rc={w.returncode}")
    check("wrapper_installed_tree", os.path.exists(
        os.path.join(ROOT, "usr/share/reelsee/reelsee.py")), "extracted")

    # ------------------------------------------------------------- launch --
    # THE gate: run the extracted tree with clean env on the SYSTEM libmpv2.
    env = fresh_home(os.path.join(ROOT, "home"))
    p = run_probe(env, "main")
    out = p.stdout or ""
    # PKG_PROBE_PASSED is only printed when EVERY probe check passed
    launch_ok = (p.returncode == 0 and "PKG_PROBE_PASSED" in out
                 and "SMOKE_FAIL" not in out)
    check("pkg_launch_ok", launch_ok, f"rc={p.returncode}")
    kind_line = [ln for ln in out.splitlines()
                 if ln.startswith("SMOKE_CHECK:libmpv_path=")]
    chosen = kind_line[0].split("=", 1)[1] if kind_line else ""
    check("pkg_uses_system_libmpv",
          "SMOKE_CHECK:libmpv_kind=system" in out
          and chosen.startswith("/usr/lib")
          and str(REPO) + "/vendor" not in chosen
          and ROOT + "/usr/share/reelsee/vendor" not in chosen,
          f"chosen={chosen or 'none'}")

    # --------------------------------------------------- negative control --
    neg_env = build_negctl_shim()
    # shim sanity: ctypes discovery is really dead under the shim
    s = subprocess.run([PY, "-c",
                        "import ctypes.util; print(ctypes.util.find_library('mpv'))"],
                       env=neg_env, capture_output=True, text=True,
                       cwd="/")
    check("negctl_shim_effective", s.stdout.strip() == "None",
          f"find_library={s.stdout.strip()!r}")
    neg_env["REELSEE_LIBMPV"] = "/nonexistent/libmpv.so.2"   # bogus candidate 1
    n = run_probe(neg_env, "negctl", timeout=60, strip_lib_env=False)
    nout = (n.stdout or "") + (n.stderr or "")
    check("negctl_gate_fails", n.returncode != 0,
          f"rc={n.returncode} (launch check MUST fail with no libmpv)")
    # R4-P3 contract: friendly failure, not an Apport crash report
    friendly = (n.returncode == 2
                and "Traceback" not in nout
                and "libmpv not found" in nout
                and "libmpv2" in nout
                and "REELSEE_LIBMPV" in nout
                and any(str(sp) in nout for sp in mpvlib.SYSTEM_PATHS))
    check("negctl_friendly_error", friendly,
          f"rc={n.returncode} tb={'Traceback' in nout}")

    finish()
    return 0


if __name__ == "__main__":
    sys.exit(main())
