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
run_needle() { # name timeout script needle [args...]
    # Same as run(), plus: the script must have PRINTED `needle`, and it may
    # take arguments.  A harness that exits 0 without ever emitting its summary
    # line is a vacuous pass, and a self-test must not be able to pass
    # vacuously.  (Log path is the FLAT scratch/smoke/<name>.log that
    # headless-smoke.sh writes — not the per-run directory.)
    if scripts/headless-smoke.sh "p11_$1" --timeout "$2" -- \
            /usr/bin/python3 "scripts/$3" "${@:5}" >/dev/null 2>&1 \
            && grep -q "$4" "scratch/smoke/p11_$1.log" 2>/dev/null; then
        echo "BATTERY $1 PASS"
    else
        echo "BATTERY $1 FAIL (log: scratch/smoke/p11_$1.log)"
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
# R14-A: the GNOME-window-menu gate — right-click opens OUR menu wherever
# the pointer is (titlebar stub included), GTK's show_window_menu is
# refused, left-click play/pause, double-click fullscreen and the drag
# move are untouched.  The gate proves the menu path with a spy + the
# EWMH _NET_WM_MOVERESIZE recipe (this box has no WM to honour menus).
run rclickwm     420 smoke_rclickwm.py
run rclickwm_b   420 smoke_rclickwm.py
# R17-A: the WM window menu must be UNREACHABLE because GTK's own
# `Gtk.GestureClick` is removed from both `Gtk.WindowHandle`s — asserted
# STRUCTURALLY (controller inventory before/after, the retained
# `Gtk.GestureDrag`, and every app-owned replacement: video click, video
# double-click fullscreen, app-owned begin_move drag on both handles, header
# double-click maximise, right-click popover on both surfaces, the three
# gtk-titlebar-*-click settings pinned AND re-pinned).  Three runs: this is
# the gate R14's Python spy could never be, and a vacuous pass is exactly
# what round 14 shipped.
run notitlebar   180 smoke_notitlebar.py
run notitlebar_b 180 smoke_notitlebar.py
run notitlebar_c 180 smoke_notitlebar.py
# R14-B: real WM_NORMAL_HINTS with PAspect, read back from the X server
# at every edge (both clips, lock/crop/fullscreen toggles, 1 px at size,
# drag-moves-anyway, storm, Wayland-style no-hints fallback to R8).
run aspecthints  420 smoke_aspecthints.py
run aspecthints_b 420 smoke_aspecthints.py
# R15-A: drag-by-header — the top strip rides a Gtk.WindowHandle that IS
# the overlay child; a 7 px press-drag on empty bar moves the window
# (EWMH _NET_WM_MOVERESIZE to our xid, direction 8, exactly once per
# gesture), the three window buttons each claim their press (zero moves,
# action fires once), right-press opens OUR menu, fullscreen detaches the
# handle and hidden chrome cannot target (no invisible drag strip).  A
# staged fake WM + wire-level spy stand in for the absent window manager.
run headerdrag   420 smoke_headerdrag.py
run headerdrag_b 420 smoke_headerdrag.py
run headerdrag_c 420 smoke_headerdrag.py
# R15-B: repeat ships ON — fresh installs boot repeating (loop_file
# default "inf", action state "one"), the one-time migration flips a
# stored "no" exactly once and never re-fires, and the native l/r keys
# still persist through the single writer path.  Three spawned child
# phases (fresh / after-off / legacy) per run.
run repeatdefault 360 smoke_repeatdefault.py
run repeatdefault_b 360 smoke_repeatdefault.py
run repeatdefault_c 360 smoke_repeatdefault.py
# R15-C: header flush — the top chrome carries ZERO margins, so its
# background spans the FULL client width from y=0 (top/left/right edges
# touched by pixels: shown top row bright, no window-content rim above
# it, veil darkens the bar rows when hidden), while the video allocation
# stays byte-identical (R12 invariant).  Never enters fullscreen (xwd
# readback breaks after a fullscreen storm under Xvfb+EGL — see commit).
run topflush     240 smoke_topflush.py
run topflush_b   240 smoke_topflush.py
run topflush_c   240 smoke_topflush.py
# R16-A: adaptive tick cadence — the ladder measured in utime on a REAL
# window (10 ms while a fade/hold is running, 100 ms while visible and
# stable, the chrome source SUSPENDED while hidden; REELSEE_TICK_PIN_MS pins
# both sources to the pre-R16 10 ms and is how the baseline arm is produced),
# plus the pump source that keeps transport running while the chrome source is
# suspended.  Three runs: the measurement is a ratio, so one lucky run is not
# evidence.
run tickrate     240 smoke_tickrate.py
run tickrate_b   240 smoke_tickrate.py
run tickrate_c   240 smoke_tickrate.py
# R16-C: the CPU decode-thread budget follows the number of LIVE windows at
# RUNTIME, not just at window open — including the window that was already
# open when the second one arrived (the regression this closes).  The budget
# rides the observed hwdec-current edge, so it is asserted live, not mocked.
run threads      120 smoke_threads.py
run threads_b    120 smoke_threads.py
run threads_c    120 smoke_threads.py
# R16-B: hidden-video suppression — an unmapped/hidden window stops decoding
# (vid=no, no frames) and resumes on map, with the one-shot reassert after a
# file change and the user-override path intact.  22 checks across three
# phases; three runs because the unmap path races the load.
run hiddensuppress   300 smoke_hiddensuppress.py
run hiddensuppress_b 300 smoke_hiddensuppress.py
run hiddensuppress_c 300 smoke_hiddensuppress.py
# R16-D: the multi-decode bench is the OPERATOR's tool on the real desktop.
# Here it runs ONCE with --hwdec no purely as a self-test that the harness
# itself completes and reports plausible counters; the `ok=1` needle is what
# makes "completed" mean something.  It is NEVER a GPU capacity gate in CI —
# llvmpipe numbers have no relationship to the real iGPU.
run_needle bench_multidecode 180 bench_multidecode.py "ok=1" \
    --hwdec no --n 2 --duration 6
ln -sfn "$RUN_ID" scratch/smoke/latest 2>/dev/null || true
echo "BATTERY_SUMMARY fails=$fails logs=scratch/smoke/$RUN_ID"
[ "$fails" -eq 0 ]
