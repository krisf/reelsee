#!/usr/bin/env bash
# P0 PROOF-B/E driver: run the render spike headlessly (looksee recipe) for
# both vo variants and assert the frame is real.
set -uo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$here"

export LD_LIBRARY_PATH="$here/vendor/usr/lib/x86_64-linux-gnu"
export GSK_RENDERER=cairo LIBGL_ALWAYS_SOFTWARE=1 GDK_BACKEND=x11 GSETTINGS_BACKEND=memory
unset WAYLAND_DISPLAY
mkdir -p scratch/xvfb-tmp
export TMPDIR="$here/scratch/xvfb-tmp"

overall=0
# PROOF-E finding: vo=gpu-next + render API SEGFAULTS — vo must stay libmpv.
for variant in default; do
  echo "=== variant: $variant ==="
  out="$(xvfb-run -a --server-args="-screen 0 1600x1000x24" \
        timeout -s TERM 40 /usr/bin/python3 scripts/spike_render.py "$variant" 2>&1)"
  rc=$?
  echo "$out" | grep -E 'SPIKE_(CHECK|RESULTS|RC)|Fatal|Traceback' || true
  if [ $rc -ne 0 ]; then echo "VARIANT $variant rc=$rc FAILED"; overall=1; continue; fi
  # non-black frame analysis on the captured xwd
  # window maps at 0,0 sized 640x360 under WM-less Xvfb — crop it out of the
  # root capture before measuring (full-frame stats are diluted by black root).
  mean="$(convert scratch/spike-frame.xwd -crop 640x360+0+0 -colorspace gray -format '%[fx:mean*255]' info: 2>/dev/null)"
  uniq="$(convert scratch/spike-frame.xwd -crop 640x360+0+0 -resize 320x180 -unique-colors txt:- 2>/dev/null | tail -n +2 | wc -l)"
  echo "VARIANT $variant: mean_luma=$mean unique_colors=$uniq"
  python3 - "$variant" "$mean" "$uniq" "$out" <<'PY'
import re, sys
variant, mean, uniq, out = sys.argv[1], float(sys.argv[2]), int(sys.argv[3]), sys.argv[4]
m = re.search(r"'time_delta': '([\d.-]+)'", out)
delta = float(m.group(1)) if m else 0.0
f = re.search(r"'vf_fps': '([\d.-]+)'", out)
fpsv = float(f.group(1)) if f else 0.0
ok = mean > 8.0 and uniq > 200 and delta > 0.5 and fpsv > 20 and "SPIKE_RC=0" in out
print(f"PROOF_B_{'PASS' if ok else 'FAIL'}: variant={variant} mean={mean:.1f} colors={uniq} "
      f"time_delta={delta:.2f} vf_fps={fpsv:.1f}")
sys.exit(0 if ok else 1)
PY
  [ $? -ne 0 ] && overall=1
done
exit $overall
