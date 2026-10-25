#!/usr/bin/env bash
# P0 PROOF-A (references/P0-SPIKE.md): runtime decoder re-init.
#
# Finding: `set hwdec <mode>` alone does NOT reinit the decoder; the video
# chain must be toggled: `set vid no` -> `set vid auto`. Proven in BOTH
# directions mid-playback on the LIVE X display (vo=null forces hwdec off,
# bare Xvfb has no GL for vo=gpu). A 200x120 window appears bottom-right
# for ~12 s.
set -uo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$here"
sock="$PWD/scratch/spike-ipc.sock"
log="$PWD/scratch/spike-hwdec.log"
mkdir -p scratch
rm -f "$sock"

export DISPLAY="${DISPLAY:-:0}"
unset WAYLAND_DISPLAY WAYLAND_SOCKET

"$HOME/bin/mpv.AppImage" --no-config --vo=gpu --ao=null \
  --geometry=200x120-0-0 --loop-file=inf --keep-open=no \
  --hwdec=vaapi-copy --vaapi-device=/dev/dri/renderD130 \
  --input-ipc-server="$sock" \
  testdata/perf_1080p30.mp4 >"$log" 2>&1 &
mpvpid=$!
cleanup() { kill "$mpvpid" 2>/dev/null; wait "$mpvpid" 2>/dev/null; }
trap cleanup EXIT

for _ in $(seq 1 60); do [ -e "$sock" ] && break; sleep 0.25; done
[ -e "$sock" ] || { echo "PROOF_A_FAIL: ipc socket never appeared"; tail -5 "$log"; exit 1; }

/usr/bin/python3 - "$sock" <<'PY'
import json, socket, sys, time
s = socket.socket(socket.AF_UNIX); s.settimeout(8); s.connect(sys.argv[1])
f = s.makefile("rwb", buffering=0)
rid = 0
def call(*args):
    global rid; rid += 1
    f.write((json.dumps({"command": list(args), "request_id": rid}) + "\n").encode())
    end = time.time() + 8
    while time.time() < end:
        line = f.readline()
        if not line: return None
        m = json.loads(line)
        if m.get("request_id") == rid:
            return m
    return None
def get(name): return call("get_property", name)
def setp(name, val): return call("set_property", name, val)

ok = True
def check(label, cond, detail=""):
    global ok
    print(f"{'PASS' if cond else 'FAIL'}: {label} {detail}")
    ok = ok and bool(cond)

time.sleep(2.5)
hw0 = get("hwdec-current")
check("starts on vaapi-copy", hw0 and hw0["data"] == "vaapi-copy", f"hwdec-current={hw0['data']}")
t0 = float(get("time-pos")["data"])

# DEMOTE: watchdog's primary action (R-7)
setp("hwdec", "no"); setp("vid", "no"); time.sleep(0.3); setp("vid", "auto")
time.sleep(1.5)
t1 = float(get("time-pos")["data"]); time.sleep(1.0); t2 = float(get("time-pos")["data"])
hw1 = get("hwdec-current")
check("demoted to CPU decode", hw1 and hw1["data"] == "no", f"hwdec-current={hw1['data']}")
check("playback continued", t2 > t1 > t0, f"{t0:.2f}->{t1:.2f}->{t2:.2f}")
check("position survived reinit", abs(float(get("time-pos")["data"]) - t2) < 1.5)

# RE-ENABLE: reverse direction
setp("hwdec", "vaapi-copy"); setp("vid", "no"); time.sleep(0.3); setp("vid", "auto")
time.sleep(1.5)
hw2 = get("hwdec-current")
check("re-enabled vaapi-copy", hw2 and hw2["data"] == "vaapi-copy", f"hwdec-current={hw2['data']}")

print(f"PROOF_A_{'PASS' if ok else 'FAIL'}")
call("quit")
sys.exit(0 if ok else 1)
PY
exit $?
