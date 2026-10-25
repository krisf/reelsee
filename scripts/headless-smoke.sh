#!/usr/bin/env bash
# Headless GUI/libmpv smoke runner (PLAN R1, §5.7).  Every GUI process and every
# libmpv-touching test runs through this: Xvfb + the proven looksee recipe +
# the vendored libmpv on LD_LIBRARY_PATH.
#
# usage: scripts/headless-smoke.sh <name> [--timeout SECS] -- CMD [ARGS...]
#
# Success: prints the captured log, then `SMOKE OK (mode=<name> elapsed=<n>s)`
#          and exits 0.  Failure: prints the captured output (stdout+stderr)
#          and exits with the child's rc.
# Smoke scripts print machine-checkable `SMOKE_CHECK:<name>=<value>` lines and
# exit non-zero on failed assertions (§5.7).
set -uo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$here"

[ $# -ge 1 ] || { echo "usage: $0 <name> [--timeout N] -- CMD..." >&2; exit 2; }
name="$1"; shift
timeout_s="${SMOKE_TIMEOUT:-60}"
if [ "${1:-}" = "--timeout" ]; then timeout_s="$2"; shift 2; fi
if [ "${1:-}" = "--" ]; then shift; fi
[ $# -ge 1 ] || { echo "no command given" >&2; exit 2; }

export LD_LIBRARY_PATH="$here/vendor/usr/lib/x86_64-linux-gnu${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
# Headless capture stability (P3 finding): with the default (cairo) renderer,
# GtkGLArea's GL->CPU readback under llvmpipe+Xvfb raced — the video area
# captured black in ~50% of runs.  Forcing the GL renderer over EGL (software
# llvmpipe via LIBGL_ALWAYS_SOFTWARE) made captures 8/8 deterministic.
export GSK_RENDERER="${GSK_RENDERER:-gl}" GDK_GL="${GDK_GL:-egl}"
export LIBGL_ALWAYS_SOFTWARE=1 GDK_BACKEND=x11 GSETTINGS_BACKEND=memory
export REELSEE_HEADLESS=1
unset WAYLAND_DISPLAY
mkdir -p scratch/xvfb-tmp scratch/smoke
export TMPDIR="$here/scratch/xvfb-tmp"
# R10 hygiene: window.json (the remembered window rect, R10-A) made
# start-up geometry LOAD-BEARING for every smoke.  Static scratch state
# dirs are reused across invocations on purpose (resume/ab/engines
# continuity is asserted by multi-process smokes), but a leftover rect
# restores run-N-1's window size/position into run-N and breaks
# fresh-window premises (measured: aspectlock restored (1067,600) and
# drag restored (180,180) from the previous battery run, failing 3 and 1
# gates deterministically).  Geometry is purged per invocation; the
# per-PID dirs used by smokes that TEST geometry persistence
# (r9rep-state-*, r10wg-state-*) don't match *-state and are untouched.
rm -f scratch/smoke/*-state/window.json 2>/dev/null || true
rm -f "${REELSEE_STATE_DIR:-$HOME/.local/state/reelsee}/window.json" \
    2>/dev/null || true

log="scratch/smoke/$name.log"
start=$SECONDS
xvfb-run -a --server-args="-screen 0 1600x1000x24" \
    timeout -s TERM "$timeout_s" "$@" >"$log" 2>&1
rc=$?
elapsed=$((SECONDS - start))

cat "$log"
if echo "$log" >/dev/null 2>&1; then :; fi
if [ $rc -eq 0 ]; then
    echo "SMOKE OK (mode=$name elapsed=${elapsed}s)"
else
    echo "--- smoke '$name' FAILED rc=$rc — captured output above; log: $log" >&2
fi
exit $rc
