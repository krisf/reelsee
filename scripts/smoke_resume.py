#!/usr/bin/python3
"""P11 T1 resume gate (§7 resume position, own resume.json).

Runs TWO real app processes back-to-back inside this runner (same Xvfb
DISPLAY, shared REELSEE_STATE_DIR): #1 pauses at ~3.5 s and quits; #2 must
come up seeked back to that position.  Exercises the full disk roundtrip —
in-memory store → close-time persist() → fresh-process restore.
"""
import os
import subprocess
import sys

from smoke_harness import ROOT, SCRATCH, check, finish

PY = "/usr/bin/python3"
CFG = os.path.join(SCRATCH, "resume-cfg")
STATE = os.path.join(SCRATCH, "resume-state")


def main() -> int:
    for d in (CFG, STATE):
        os.makedirs(d, exist_ok=True)
    for name in ("config.json", "resume.json", "ab.json"):
        for base in (CFG, STATE):
            p = os.path.join(base, name)
            if os.path.exists(p):
                os.unlink(p)
    env = dict(os.environ, REELSEE_CONFIG_DIR=CFG, REELSEE_STATE_DIR=STATE)

    def run(script):
        try:
            r = subprocess.run([PY, f"scripts/{script}"], env=env, cwd=ROOT,
                               capture_output=True, text=True, timeout=75)
        except subprocess.TimeoutExpired:
            class R:
                returncode, stdout, stderr = 124, "", "TIMEOUT"
            return R()
        print(r.stdout, end="", flush=True)
        if r.stderr:
            print("---- stderr tail ----")
            print("\n".join(r.stderr.splitlines()[-15:]))
        return r

    a = run("smoke_resume_a.py")
    check("resume_writer_ok", a.returncode == 0
          and "SMOKE_ASSERTS_PASSED" in a.stdout, f"rc={a.returncode}")
    rp = os.path.join(STATE, "resume.json")
    import json
    entry_pos = None
    if os.path.exists(rp):
        with open(rp) as fh:
            data = json.load(fh)
        for _k, v in data.items():
            if isinstance(v, dict) and v.get("pos"):
                entry_pos = v["pos"]
    check("resume_json_written", isinstance(entry_pos, float)
          and 3.0 <= entry_pos <= 4.5, f"pos={entry_pos}")

    b = run("smoke_resume_b.py")
    check("resume_reader_ok", b.returncode == 0
          and "SMOKE_ASSERTS_PASSED" in b.stdout, f"rc={b.returncode}")
    finish()
    return 0


if __name__ == "__main__":
    sys.exit(main())
