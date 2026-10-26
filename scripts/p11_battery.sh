#!/usr/bin/env bash
# P11 gate: run the ENTIRE smoke battery and print one summary line per
# smoke.  Usable from any checkout dir (main tree or the clean clone).
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
fails=0
RUN_ID="$(date +%Y%m%d-%H%M%S)"
mkdir -p "scratch/smoke/$RUN_ID"
run() { # name timeout script
    if scripts/headless-smoke.sh "p11_$1" --timeout "$2" -- \
            /usr/bin/python3 "scripts/$3" >/dev/null 2>&1; then
        echo "BATTERY $1 PASS"
    else
        echo "BATTERY $1 FAIL (log: scratch/smoke/$RUN_ID/p11_$1.log)"
        fails=$((fails+1))
    fi
    if grep -q Traceback "scratch/smoke/$RUN_ID/p11_$1.log" 2>/dev/null; then
        echo "BATTERY $1 TRACEBACK-IN-LOG"
        fails=$((fails+1))
    fi
}
run render        60 smoke_render.py
run mpv_roundtrip 60 smoke_mpv_roundtrip.py
run close         45 smoke_close.py
run chrome        45 smoke_chrome.py
run adwchrome    120 smoke_adwchrome.py
# R12: the overlay-chrome contract (video owns the whole client area;
# chrome is an opacity-only overlay whose hide/reveal moves NOT a pixel;
# window controls wired; CSD + remembered resize intact).  3x like the
# other structural gates.
run overlaychrome 120 smoke_overlaychrome.py
run overlaychrome_b 120 smoke_overlaychrome.py
run overlaychrome_c 120 smoke_overlaychrome.py
# R13: the idle-stability gate — real window, fast-forwarded clock, ZERO
# input for 60 simulated seconds: exactly one hide, opacity 0 at every
# sample after it (the old dt>timeout*4 rule resurrected the chrome on
# a 12 s cycle), video allocation fixed, plus a motion positive control.
# Three runs, same as the other structural gates.
run idlechrome   90 smoke_idlechrome.py
run idlechrome_b 90 smoke_idlechrome.py
run idlechrome_c 90 smoke_idlechrome.py
# R8-A: the topmenu gate (ui.show_top_menu) was sanctioned-DELETED with
# the bar itself; nomenubar replaces it and, per brief §C, runs 3x in a
# row so a regression in the dead-key load / tree absence fails the
# battery, not CI luck.
run nomenubar    180 smoke_nomenubar.py
run nomenubar_b  180 smoke_nomenubar.py
run nomenubar_c  180 smoke_nomenubar.py
run repeat        90 smoke_repeat.py
# R9-C: the repeat-PERSISTENCE gate, 3x in a row like nomenubar — each
# run is three real processes (set one -> quit -> restart proves the
# seed + reload/next/prev/r-key persistence -> restart proves off
# persists too); three agreeing runs beat one lucky one.
run repeatpersist   360 smoke_repeatpersist.py
run repeatpersist_b 360 smoke_repeatpersist.py
run repeatpersist_c 360 smoke_repeatpersist.py
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
# R8-B: aspect-locked window resizing (observed dwidth/dheight,
# debounce+guard, work-area clamp, vertical video, toggle off = free).
run aspectlock   180 smoke_aspectlock.py
# R10-D: remembered window geometry + exact-size cascade placement over
# raw Xlib, plus the Wayland-style no-xid degrade.  One script drives all
# three phases (remember + disk record -> restore + cascade + right-edge
# flip -> fake-FakeSurface open path); three runs catch flakes the same
# way nomenubar/repeatpersist do.
run wingeom      300 smoke_wingeom.py
run wingeom_b    300 smoke_wingeom.py
run wingeom_c    300 smoke_wingeom.py
# R11-D: stateful crop-to-window (panscan cover): letterbox bars go away
# in pixels (tall window + wide clip, wide window + vertical clip), bars
# return on crop-off and across a file change, and the §C aspect dance —
# lock suspended while crop is on (free resize, zero corrections), one
# snap back to the video aspect when crop turns off.  Three runs.
run cropwin      300 smoke_cropwin.py
run cropwin_b    300 smoke_cropwin.py
run cropwin_c    300 smoke_cropwin.py
ln -sfn "$RUN_ID" scratch/smoke/latest 2>/dev/null || true
echo "BATTERY_SUMMARY fails=$fails logs=scratch/smoke/$RUN_ID"
[ "$fails" -eq 0 ]
