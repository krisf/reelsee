#!/usr/bin/python3
"""R29 gate: the settle loop must yield the INSTANT a move-drag begins
(references/ROUND29-BRIEF.md §B2).

Measured defect (D2): `_geom_settle_start` (the R21 placement loop) re-issues
`x11pos.set_rect` every 150 ms for a 2.0 s budget at app open, and NOTHING in
the drag path aborts it, so the app issues X moves into the user's live WM
move grab — the title bar is not draggable for "a couple seconds".  Its only
user-yield is the observed-rect `user-grab` test, which the loop's own
set_rect masks (it reads back its own target).

The fix under test: `_on_move_drag_begin` (and `_begin_move`) abort the live
loop via `_geom_settle_finish("user-drag", self._geom_live_rect())`.

In-process is correct here (the defect is the app's own loop, not the
compositor): the REAL window methods are bound onto a bare stub — the
tests/test_window_placement_r21.py harness shape — with `x11pos.set_rect`
monkeypatched to False and `x11pos.get_rect` to a FIXED rect that is NOT the
target (a WM that ignores us, so the loop provably stays armed), and the real
GLib main loop pumped past GEOM_SETTLE_INTERVAL_MS * 3.

Checks (SMOKE_CHECK lines):
  settle_armed              — the loop is live after _geom_settle_start
  loop_still_arming_wm_ignore — sanity: with no drag the loop DOES re-issue
  drag_begin_aborts_settle  — after _on_move_drag_begin: _geom_settle None
                              (RED against pre-fix HEAD: the loop survives)
  drag_begin_reason_user_drag — _geom_settle_reason == "user-drag"
  no_set_rect_after_abort   — pumping 3 intervals issues NO further moves
  user_grab_path_intact     — regression: observed-rect change still ends
                              the loop with reason "user-grab" (R21 untouched)
  user_grab_no_further_moves — DELTA after that finish: pumping 3 more
                              intervals issues no further set_rect.  It is
                              measured against the count AT the finish, not
                              cumulatively — the settle loop legitimately
                              issues ~5 attempts (GEOM_SETTLE_ATTEMPTS=8,
                              one per 150 ms) before the observed-rect
                              finish fires, so a cumulative bound could
                              never hold even with a perfect fix (the
                              pre-fix RED log shows set_rect_calls=5).
  begin_move_aborts_settle  — the second seam: _begin_move also aborts
  cursor_blank_on_hide      — the D1 cursor witness at the GTK seam: a
                              stationary pointer in the bottom band ends in a
                              HIDE edge that calls _blank_cursor(True)
                              (RED pre-fix: hover pins, no hide edge exists)
  cursor_restore_on_show    — motion re-shows: _blank_cursor(False) on the
                              show edge
"""
from __future__ import annotations

import sys
import time

from smoke_harness import check, finish  # ROOT/SRC path side effect included

import gi  # noqa: E402
gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import GLib, Gtk  # noqa: E402

import window as W  # noqa: E402  (src/ path added by smoke_harness import)
from chrome import Chrome, ChromeState  # noqa: E402
from window import GEOM_SETTLE_INTERVAL_MS, ReelseeWindow  # noqa: E402

TARGET = (100, 100, 1280, 720)          # where we want the window
WM_IGNORED = (500, 300, 1280, 720)      # a WM that ignores every move
USER_GRAB = (1100, 640, 1280, 720)      # grabbed mid-settle and dragged


def pump(seconds: float) -> None:
    """Run the REAL GLib main loop for `seconds` (the settle loop schedules
    real GLib timeouts; a fake clock would not prove anything about the
    loop actually issuing moves into a drag)."""
    loop = GLib.MainLoop()
    GLib.timeout_add(max(1, int(seconds * 1000)), loop.quit)
    loop.run()


class _Gesture:
    """Enough of a Gtk.GestureDrag for _on_move_drag_begin / _begin_move."""

    def __init__(self):
        self.claimed = False

    def get_current_event_device(self):
        return None

    def get_current_button(self):
        return 1

    def get_current_event_time(self):
        return 1234

    def set_state(self, _state):
        self.claimed = True


class SettleWin:
    """The REAL R21/R29 placement + drag methods on a bare object (the
    tests/test_window_placement_r21.py SettleStub shape, but with the REAL
    GLib instead of FakeClock because this gate pumps real wall time)."""
    _geom_settle_start = ReelseeWindow._geom_settle_start
    _geom_settle_step = ReelseeWindow._geom_settle_step
    _geom_settle_tick = ReelseeWindow._geom_settle_tick
    _geom_settle_schedule = ReelseeWindow._geom_settle_schedule
    _geom_settle_finish = ReelseeWindow._geom_settle_finish
    _geom_settle_stop = ReelseeWindow._geom_settle_stop
    _geom_settle_disarm = ReelseeWindow._geom_settle_disarm
    _geom_settle_aborted = ReelseeWindow._geom_settle_aborted
    _geom_arm_guard = ReelseeWindow._geom_arm_guard
    _geom_live_rect = ReelseeWindow._geom_live_rect
    _geom_windowed = ReelseeWindow._geom_windowed
    _on_move_drag_begin = ReelseeWindow._on_move_drag_begin
    _on_move_drag_update = ReelseeWindow._on_move_drag_update

    def __init__(self):
        self._geom_rect = TARGET
        self._geom_settle = None
        self._geom_settle_moves = 0
        self._geom_settle_reason = None
        self._closing = False
        self._fs = False
        self._aspect_size = None
        self._aspect_applying = False
        self._geom_guard_id = None
        self._move_pending = {}
        self._move_threshold = 8.0
        self._move_initiated = 0

    def is_fullscreen(self):
        return self._fs

    def is_maximized(self):
        return False

    def get_width(self):
        return 0

    def get_height(self):
        return 0

    def get_surface(self):
        return None               # no toplevel: _begin_move early-returns


class PatchedX:
    """x11pos stand-in.  `fixed` mode: get_rect ALWAYS answers WM_IGNORED —
    a WM that ignores us, so the loop stays armed by construction (the real
    Xvfb battery has no WM at all, this is exactly that situation).
    `script` mode: replay a list, then the tail — the user-grab leg."""

    def __init__(self, fixed=WM_IGNORED, script=None):
        self.fixed = fixed
        self.script = list(script) if script is not None else None
        self.set_calls = 0
        self.reads = []

    def set_rect(self, win, rect):
        self.set_calls += 1
        return False                    # the brief: set_rect → False

    def get_rect(self, win):
        if self.script is not None:
            r = self.script.pop(0) if self.script else self.fixed
        else:
            r = self.fixed
        self.reads.append(r)
        return r


def install_x(px):
    """Monkeypatch the module-level seam (window.py calls x11pos.set_rect /
    x11pos.get_rect by attribute at call time)."""
    W.x11pos.set_rect = px.set_rect
    W.x11pos.get_rect = px.get_rect


class _FakeBar:
    """Enough of a reveal widget for Chrome._fade_to/_set_chrome_visible."""

    def __init__(self):
        self.opacity = 1.0
        self.can_target = True

    def get_opacity(self):
        return self.opacity

    def set_opacity(self, v):
        self.opacity = float(v)

    def set_can_target(self, v):
        self.can_target = bool(v)


class RevealSeam:
    """Chrome's REAL visibility-application seam on a bare object: the exact
    code path `_set_chrome_visible` runs (fade group + the `changed` edge →
    _blank_cursor).  The cursor spy REPLACES _blank_cursor — the brief's
    witness: called with True on the hide edge, False on the show edge."""

    _set_chrome_visible = Chrome._set_chrome_visible
    _fade_to = Chrome._fade_to

    def __init__(self, state):
        self.state = state            # _fade_to reads state.clock()
        self._bars_visible = True
        self._reveal_widgets = [_FakeBar()]
        self._fade_ids = {}
        self._fade_target = {}
        self.blank_calls = []

    def _blank_cursor(self, blank):
        self.blank_calls.append(bool(blank))


def cursor_leg(name_red: str) -> None:
    """D1's downstream symptom at the GTK seam (the brief §B1: the cursor
    witness lives HERE, not over XFixes): drive the REAL ChromeState with a
    stationary pointer in the bottom band and apply every decision through
    the REAL reveal seam.  Post-fix the countdown expires IN THE BAND ⇒ a
    hide edge ⇒ _blank_cursor(True); motion ⇒ show edge ⇒ _blank_cursor
    (False).  Pre-fix the hover branch pins (True, "hover") forever: there is
    NO hide edge, so no blank call — this check is RED pre-fix too."""
    st = ChromeState(3000.0)
    seam = RevealSeam(st)
    st.on_pointer_motion(0.0)              # t0: motion, chrome visible
    st.set_bottom_hover(True, 0.0)         # pointer parks IN THE BAND
    applied = seam._bars_visible
    t = 0.0
    blanked = False
    while t <= 3000.0 + 6 * 10:            # healthy 10 ms tick stream
        vis, _r = st.tick(t)
        if vis is not applied:
            seam._set_chrome_visible(vis)  # the real apply path, edges only
            applied = vis
            if not vis and not blanked:
                blanked = seam.blank_calls == [True]
        t += 10.0
    check(f"{name_red}_hide_edge_blanks_cursor", blanked,
          f"blank_calls={seam.blank_calls} visible={vis} reason={st.reason}")
    # motion re-shows (and un-blanks) — the fix must not just disable chrome
    st.on_pointer_motion(6000.0)
    vis, _r = st.tick(6000.0)
    if vis is not applied:
        seam._set_chrome_visible(vis)
        applied = vis
    check(f"{name_red}_show_edge_restores_cursor",
          seam.blank_calls == [True, False],
          f"blank_calls={seam.blank_calls} visible={vis}")
    pump(0.4)                              # let the fade timers retire cleanly


def main() -> int:
    # ---------------- 1/2: arm the loop against a WM that ignores us ------
    px = PatchedX(fixed=WM_IGNORED)
    install_x(px)
    win = SettleWin()
    armed = win._geom_settle_start(TARGET)
    check("settle_armed", armed and win._geom_settle is not None,
          f"armed={armed} settle={win._geom_settle is not None} "
          f"set_rect_calls={px.set_calls}")
    check("loop_issues_move_at_arm", px.set_calls == 1, str(px.set_calls))

    # sanity (non-vacuity): pump one interval with NO drag — the loop DOES
    # re-issue against a fixed wrong live rect (that is the defect's engine)
    pump(GEOM_SETTLE_INTERVAL_MS / 1000.0 * 1.6)
    pre_drag_calls = px.set_calls
    check("loop_still_arming_wm_ignore",
          win._geom_settle is not None and pre_drag_calls >= 2,
          f"settle={'live' if win._geom_settle else 'gone'} "
          f"set_rect_calls={pre_drag_calls}")

    # ---------------- 3: the user presses on a move handle ----------------
    g = _Gesture()
    win._on_move_drag_begin(g, 10.0, 10.0, "top")

    # ---------------- 4: the loop must be GONE, reason user-drag ----------
    # THE pre-fix RED: on HEAD a4930ad the settle dict survives the press.
    check("drag_begin_aborts_settle", win._geom_settle is None,
          f"settle={'live' if win._geom_settle is not None else 'None'}")
    check("drag_begin_reason_user_drag",
          win._geom_settle_reason == "user-drag",
          str(win._geom_settle_reason))

    # ---------------- 5: no further set_rect calls after the abort --------
    pump(GEOM_SETTLE_INTERVAL_MS / 1000.0 * 3 + 0.25)
    check("no_set_rect_after_abort", px.set_calls == pre_drag_calls,
          f"before={pre_drag_calls} after={px.set_calls}")

    # ---------------- 5b: a plain press that never travels ALSO aborts ----
    # (brief rule: do not wait for the threshold crossing — the touch is the
    # signal; and the pending origin is still recorded for the click path)
    win2 = SettleWin()
    win2._geom_settle_start(TARGET)
    g2 = _Gesture()
    win2._on_move_drag_begin(g2, 10.0, 10.0, "header")
    check("press_without_travel_still_aborts",
          win2._geom_settle is None
          and win2._geom_settle_reason == "user-drag"
          and "header" in win2._move_pending,
          f"settle={win2._geom_settle} reason={win2._geom_settle_reason} "
          f"pending={'header' in win2._move_pending}")

    # ---------------- 5c: the second seam — _begin_move aborts too --------
    # The abort must sit at the TOP of _begin_move (brief: mirror the same
    # two lines), so it fires even when the seam below refuses: this stub's
    # get_surface() is None, the normal "no toplevel" early return — the
    # settle must already be finished by then.
    win3 = SettleWin()
    win3._geom_settle_start(TARGET)
    ReelseeWindow._begin_move(win3, _Gesture(), {}, "video")
    check("begin_move_aborts_settle", win3._geom_settle is None,
          f"settle={'live' if win3._geom_settle is not None else 'None'} "
          f"reason={win3._geom_settle_reason}")
    check("begin_move_reason_user_drag",
          win3._geom_settle_reason == "user-drag",
          str(win3._geom_settle_reason))

    # ---------------- 6: the R21 user-grab observed path is UNTOUCHED -----
    px2 = PatchedX(fixed=USER_GRAB, script=[WM_IGNORED, USER_GRAB])
    install_x(px2)
    win4 = SettleWin()
    win4._geom_settle_start(TARGET)
    pump(GEOM_SETTLE_INTERVAL_MS / 1000.0 * 1.6)
    check("user_grab_path_intact",
          win4._geom_settle is None
          and win4._geom_settle_reason == "user-grab"
          and win4._geom_rect == tuple(USER_GRAB),
          f"settle={win4._geom_settle} reason={win4._geom_settle_reason} "
          f"rect={win4._geom_rect}")
    # DELTA, not cumulative: the legitimate attempt count BEFORE the
    # observed-rect finish varies with pump timing (~2-5 moves); only
    # "no moves AFTER the finish" is the contract.  The original
    # `set_calls <= 2` cumulative bound could never be satisfied by a
    # correct implementation — gate-correctness fix, see the commit msg.
    calls_at_finish = px2.set_calls
    pump(GEOM_SETTLE_INTERVAL_MS / 1000.0 * 3)
    check("user_grab_no_further_moves", px2.set_calls == calls_at_finish,
          f"at_finish={calls_at_finish} after={px2.set_calls}")

    # ---------------- 7: the cursor witness (see cursor_leg) --------------
    cursor_leg("cursor")

    # keep Gtk imported for signature parity with the other smokes
    assert Gtk.MAJOR_VERSION >= 4
    finish()
    return 0


if __name__ == "__main__":
    sys.exit(main())
