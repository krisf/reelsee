#!/usr/bin/env bash
# P11 gate: run the ENTIRE smoke battery and print one summary line per
# smoke.  Usable from any checkout dir (main tree or the clean clone).
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
fails=0
run() { # name timeout script
    if scripts/headless-smoke.sh "p11_$1" --timeout "$2" -- \
            /usr/bin/python3 "scripts/$3" >/dev/null 2>&1; then
        echo "BATTERY $1 PASS"
    else
        echo "BATTERY $1 FAIL (log: scratch/smoke/p11_$1.log)"
        fails=$((fails+1))
    fi
    if grep -q Traceback "scratch/smoke/p11_$1.log" 2>/dev/null; then
        echo "BATTERY $1 TRACEBACK-IN-LOG"
        fails=$((fails+1))
    fi
}
run render        60 smoke_render.py
run mpv_roundtrip 60 smoke_mpv_roundtrip.py
run close         45 smoke_close.py
run chrome        45 smoke_chrome.py
run adwchrome    120 smoke_adwchrome.py
run topmenu      180 smoke_topmenu.py
# D1 (R7): the debounced-flush race in this gate proved FLAKY once;
# fixed with a poll-the-disk deadline — the battery now runs it 3x in
# a row so any regression in that race fails the battery, not CI luck.
run topmenu_b    180 smoke_topmenu.py
run topmenu_c    180 smoke_topmenu.py
run repeat        90 smoke_repeat.py
run drag          150 smoke_drag.py
run abclear        90 smoke_abclear.py
run settings      45 smoke_settings.py
run ab            45 smoke_ab.py
run engine        60 smoke_engine.py
run watchdog      60 smoke_watchdog.py
run watchdog_real 90 smoke_watchdog_real.py
run signals       90 smoke_signal_shutdown.py
run multiwindow   90 smoke_multiwindow.py
run slowio        60 smoke_slowio.py
run package       240 smoke_package.py
run libmpv_error   90 smoke_libmpv_error.py
run install_paths  300 smoke_installed_paths.py
run t1           300 smoke_t1.py
run resume       150 smoke_resume.py
run r6menu       300 smoke_r6menu.py
run r6bar        150 smoke_r6_bar.py
run r6mute       150 smoke_r6_mute_restart.py
run r7decode     150 smoke_r7_decode.py
echo "BATTERY_SUMMARY fails=$fails"
[ "$fails" -eq 0 ]
