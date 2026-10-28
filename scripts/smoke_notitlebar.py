#!/usr/bin/python3
"""R17 gate smoke (notitlebar): the WM window menu must be unreachable
because GTK's OWN click gesture is gone from both `Gtk.WindowHandle`s —
asserted STRUCTURALLY, at the controller level, not at a Python spy.

Why this gate exists (and why R14's gate was not enough)
--------------------------------------------------------
`Gtk.WindowHandle` pre-installs its own input gestures in C at
construction: a `Gtk.GestureClick` (button 0 = ANY button, bubble phase)
plus a `Gtk.GestureDrag`.  That click gesture implements the
`gtk-titlebar-{double,middle,right}-click` settings, and for button 3 its
C code calls `gdk_toplevel_show_window_menu()` — on X11 that sends the
`_GTK_SHOW_WINDOW_MENU` client message and mutter pops the WM window menu
over our own popover (the R14/R17 defect, operator-confirmed on a real
GNOME/XWayland desktop).

R14's fix was structurally vacuous: it monkeypatched
`Gdk.Toplevel.show_window_menu` in Python.  The C code dispatches through
the *vfunc* (`GDK_TOPLEVEL_GET_IFACE(toplevel)->show_window_menu`), which
never performs a Python attribute lookup, so the patch never ran on that
path and R14's own gate spy counted 0 SPURIOUSLY.  On top of that Xvfb has
no window manager, so even a real `_GTK_SHOW_WINDOW_MENU` message vanishes
into the void there.  This gate therefore asserts the STRUCTURE that makes
the message impossible: the C `Gtk.GestureClick` is removed from both
handles, the `Gtk.GestureDrag` that every drag feature depends on survives,
and every behaviour the C gesture used to carry is now app-owned.

What is asserted here (all structural — see the report for the wire-level
evidence in `smoke_rclickwm`):
  (a) BEFORE removal a fresh `Gtk.WindowHandle` really does carry the
      C-installed `GestureClick` (recorded count — the fixture fails loudly
      if GTK ever stops pre-installing it, rather than passing vacuously);
  (b) AFTER app construction NEITHER handle carries any `GestureClick`,
      both still carry their `GestureDrag`;
  (c) the app-owned replacements exist and are wired: video click/pause,
      video double-click fullscreen, window-move drag on both handles,
      header double-click maximise, right-click popover on both surfaces,
      and the three `gtk-titlebar-*-click` settings pinned to 'none'.
"""
from __future__ import annotations

import os
import sys

from smoke_harness import SCRATCH, check, finish

CFG = SCRATCH / "notitlebar-cfg"
CFG.mkdir(parents=True, exist_ok=True)
os.environ["REELSEE_CONFIG_DIR"] = str(CFG)
os.environ["REELSEE_STATE_DIR"] = str(SCRATCH / "notitlebar-state")

import gi  # noqa: E402
gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, Gtk  # noqa: E402

import reelsee  # noqa: E402  (after env, like the real entry)
import window as W  # noqa: E402

FILES = ["testdata/smoke_360p.mp4"]
state: dict = {}


# ---------------------------------------------------------------- helpers
def kinds(widget) -> list[str]:
    return W.describe_controllers(widget)


def count(widget, cls) -> int:
    return sum(1 for c in widget.observe_controllers() if isinstance(c, cls))


def first(widget, cls, button=None, phase=None):
    for c in widget.observe_controllers():
        if not isinstance(c, cls):
            continue
        if button is not None and c.get_button() != button:
            continue
        if phase is not None and c.get_propagation_phase() != phase:
            continue
        return c
    return None


def has(widget, text: str) -> bool:
    return any(k.startswith(text) for k in kinds(widget))


# ------------------------------------------------- (a) fixture-level proof
def fixture_evidence() -> None:
    """A handle built by THIS process, outside the app: the C gesture is
    there to be found.  Recorded, not assumed — if GTK 4.x stops
    pre-installing it, `fresh_handle_clicks=0` makes this gate fail loudly
    instead of passing because there was nothing to remove."""
    h = Gtk.WindowHandle()
    h.set_child(Gtk.Box())
    desc = kinds(h)
    state["fresh_handle"] = desc
    state["fresh_clicks"] = count(h, Gtk.GestureClick)
    state["fresh_drags"] = count(h, Gtk.GestureDrag)
    check("fixture_fresh_handle_has_gestureclick",
          state["fresh_clicks"] >= 1, f"count={state['fresh_clicks']} {desc}")
    check("fixture_fresh_handle_has_gesturedrag",
          state["fresh_drags"] >= 1, f"count={state['fresh_drags']} {desc}")
    check("fixture_c_click_is_any_button_bubble",
          any(k == "GestureClick(button=0,phase=bubble)" for k in desc),
          str(desc))


def strip_helper_negative_control() -> None:
    """The audit dict must be MEASURED, not hardcoded: run the real strip
    helper on a handle we prepared ourselves and on a widget that carries
    no click gesture at all."""
    h = Gtk.WindowHandle()
    h.set_child(Gtk.Box())
    mine = Gtk.GestureClick()
    mine.set_button(3)
    h.add_controller(mine)                    # now: C click + our own click
    a = W.strip_handle_click_gestures(h, "probe")
    check("strip_helper_counts_every_click",
          a["before"] >= state["fresh_clicks"] + 1 and a["after"] == 0,
          f"before={a['before']} after={a['after']} {a['kinds_after']}")
    check("strip_helper_keeps_drag", a["drag_kept"] is True, str(a))
    box = Gtk.Box()                           # a widget with NO gestures
    b = W.strip_handle_click_gestures(box, "probe")
    check("strip_helper_reports_zero_when_absent",
          b["before"] == 0 and b["after"] == 0 and b["drag_kept"] is False,
          str(b))


# ------------------------------------------------------- settings hardening
def settings_checks() -> None:
    s = Gtk.Settings.get_default()
    if s is None:
        check("gtk_settings_default_exists", False, "None")
        return
    vals = {p: s.get_property(p) for p in W.TITLEBAR_CLICK_SETTINGS}
    check("titlebar_click_settings_pinned",
          all(v == W.TITLEBAR_CLICK_OFF for v in vals.values()), str(vals))
    # The re-pin is the point: GNOME's xsettings daemon re-pushes
    # Gtk/TitlebarRightClick at any time, so a startup-only write loses on a
    # real desktop.  Push a value in from OUTSIDE and prove it is undone.
    s.set_property("gtk-titlebar-right-click", "menu")
    after = s.get_property("gtk-titlebar-right-click")
    check("titlebar_right_click_repin_survives_xsettings",
          after == W.TITLEBAR_CLICK_OFF, f"after_external_push={after}")
    check("settings_hardening_reported_active",
          state["win"]._titlebar_pinned is True,
          f"_titlebar_pinned={state['win']._titlebar_pinned}")


# ------------------------------------------------------------- (b) structure
def structural_checks(win) -> None:
    for which, handle, audit in (("video", win._handle, win._handle_click_audit),
                                 ("header", win._top_handle,
                                  win._top_handle_click_audit)):
        desc = kinds(handle)
        state[f"{which}_kinds"] = desc
        check(f"audit_{which}_before_found_c_gestures",
              audit["before"] >= 1, f"{which}: {audit}")
        check(f"audit_{which}_after_zero_clicks", audit["after"] == 0,
              f"{which}: {audit}")
        check(f"no_gestureclick_on_{which}_handle",
              count(handle, Gtk.GestureClick) == 0, str(desc))
        check(f"gesturedrag_kept_on_{which}_handle",
              has(handle, "GestureDrag(button=1,phase=bubble)"), str(desc))
        check(f"app_move_drag_present_on_{which}_handle",
              has(handle, "GestureDrag(button=1,phase=capture)")
              and win._move_drags[which] in handle.observe_controllers(),
              str(desc))
    # header double-click maximise (replaces gtk-titlebar-double-click) —
    # on the STRIP, so the handle itself ends up with no GestureClick at all
    check("header_dblclick_gesture_present",
          win._hdr_dblclick in win.top_bar.observe_controllers()
          and win._hdr_dblclick.get_button() == 1,
          str(kinds(win.top_bar)))
    check("header_dblclick_gesture_not_on_handle",
          win._hdr_dblclick not in win._top_handle.observe_controllers(),
          str(state["header_kinds"]))
    # video-area click/pause + double-click fullscreen live on the GLArea
    area = win.view.area
    check("video_click_gesture_on_glarea",
          win._click_gestures[0] in area.observe_controllers()
          and win._click_gestures[0].get_button() == 1, str(kinds(area)))
    check("video_rclick_gesture_on_glarea",
          win._click_gestures[1] in area.observe_controllers()
          and win._click_gestures[1].get_button() == 3, str(kinds(area)))
    check("window_rclick_capture_gesture",
          first(win, Gtk.GestureClick, button=3,
                phase=Gtk.PropagationPhase.CAPTURE) is not None,
          str(kinds(win)))
    check("context_popover_wired_to_video_area",
          isinstance(win._ctx_pop, Gtk.PopoverMenu)
          and win._ctx_pop.get_parent() is area,
          f"{type(win._ctx_pop).__name__} parent={win._ctx_pop.get_parent()}")


# ------------------------------------------------- (c) wiring, app-level seam
def wiring_checks(win) -> None:
    """Emit on the app's OWN controllers: the handler must answer, with the
    right device/button.  Spying is legitimate here because OUR code is the
    one that initiates (unlike the C-internal show_window_menu path)."""
    calls: list = []
    orig = Gdk.Toplevel.begin_move
    Gdk.Toplevel.begin_move = lambda self, *a, **k: calls.append(a)

    def _max_calls():
        return state.setdefault("max_calls", 0)

    def _count_max(*_a):
        state["max_calls"] = _max_calls() + 1

    orig_max = win._on_maxrestore_clicked
    orig_fs = win._act_fullscreen
    state["max_calls"] = 0
    fs_calls: list = []
    win._on_maxrestore_clicked = _count_max
    win._act_fullscreen = lambda *_a: fs_calls.append(1)
    try:
        # ---- R17-C: the move fires at the THRESHOLD CROSSING, not on press.
        # On this GTK build Gtk.GestureDrag::drag-begin is emitted on the
        # PRESS itself (measured), so a handler that moved there swallowed
        # every click in the video area.  These two checks are the negative
        # control for that regression: press alone must not move.
        check("move_threshold_honours_desktop_setting",
              win._move_threshold >= W.MOVE_DRAG_MIN_PX,
              f"threshold={win._move_threshold}")
        win._move_drags["header"].emit("drag-begin", 123.0, 11.0)
        check("drag_begin_alone_does_not_move", len(calls) == 0, str(calls))
        check("drag_begin_records_the_press_origin",
              win._move_pending.get("header") is not None
              and abs(win._move_pending["header"]["x"] - 123.0) < 0.01,
              str(win._move_pending.get("header")))
        win._move_drags["header"].emit("drag-update", 1.0, 1.0)
        check("below_threshold_does_not_move", len(calls) == 0, str(calls))
        win._move_drags["header"].emit("drag-update", 40.0, 30.0)
        check("header_drag_initiates_begin_move", len(calls) == 1, str(calls))
        ok_dev = bool(calls) and isinstance(calls[0][0], Gdk.Device)
        check("begin_move_uses_event_device", ok_dev,
              f"device={calls[0][0] if calls else None}")
        check("begin_move_uses_primary_button",
              bool(calls) and calls[0][1] == 1, str(calls))
        check("begin_move_uses_drag_origin",
              bool(calls) and abs(calls[0][2] - 123.0) < 0.01
              and abs(calls[0][3] - 11.0) < 0.01, str(calls))
        check("begin_move_timestamp_forwarded",
              bool(calls) and isinstance(calls[0][4], int), str(calls))
        check("move_initiated_counter_advanced",
              win._move_initiated == 1, str(win._move_initiated))
        # further motion in the same drag must not start a SECOND move
        win._move_drags["header"].emit("drag-update", 90.0, 70.0)
        check("one_move_per_drag", len(calls) == 1, str(calls))
        # released before the threshold => never a move (a click survives)
        calls.clear()
        win._move_drags["video"].emit("drag-begin", 50.0, 50.0)
        win._move_drags["video"].emit("drag-end", 2.0, 1.0)
        win._move_drags["video"].emit("drag-update", 90.0, 0.0)
        check("drag_released_early_never_moves", len(calls) == 0, str(calls))
        # a right-button drag must NOT move the window (R6-A): the app
        # gesture is button-1 only, so the controller never fires.
        # video-area drag-anywhere (R5-D) still moves, past the threshold
        win._move_drags["video"].emit("drag-begin", 50.0, 50.0)
        win._move_drags["video"].emit("drag-update", 0.0, 25.0)
        check("video_drag_also_initiates_move", len(calls) == 1, str(calls))

        # header double-click => maximise/restore
        win._hdr_dblclick.emit("pressed", 2, 5.0, 5.0)
        check("header_doubleclick_maximizes", _max_calls() == 1,
              f"calls={_max_calls()}")
        win._hdr_dblclick.emit("pressed", 1, 5.0, 5.0)
        check("header_singleclick_does_not_maximize", _max_calls() == 1,
              f"calls={_max_calls()}")
        # ... but a press inside a window control button belongs to the
        # button (double-clicking Close must close, not maximise)
        check("header_strip_is_the_handles_child",
              win.top_bar.get_parent() is win._top_handle,
              str(win.top_bar.get_parent()))
        ok, r = win.btn_close.compute_bounds(win.top_bar)
        check("close_button_has_bounds_for_hit_test", bool(ok) and r is not None,
              f"ok={ok} r={r}")
        if r is not None:
            cx = r.get_x() + r.get_width() / 2.0
            cy = r.get_y() + r.get_height() / 2.0
            win._hdr_dblclick.emit("pressed", 2, cx, cy)
            check("header_doubleclick_over_button_not_maximized",
                  _max_calls() == 1, f"calls={_max_calls()} bounds=({cx:.0f},{cy:.0f})")
        # video double-click => fullscreen, through the GLArea controller
        # (never through the removed handle gesture)
        win._click_gestures[0].emit("pressed", 2, 200.0, 200.0)
        check("video_doubleclick_fullscreen_wired", len(fs_calls) == 1,
              f"calls={fs_calls}")
        check("fullscreen_path_independent_of_handle_click",
              count(win._handle, Gtk.GestureClick) == 0
              and win._click_gestures[0] in win.view.area.observe_controllers(),
              "fullscreen gesture is not on the handle")
    finally:
        Gdk.Toplevel.begin_move = orig
        win._on_maxrestore_clicked = orig_max
        win._act_fullscreen = orig_fs


# ----------------------------------------------------------------- driver
def probe_schedule(app):
    from gi.repository import GLib
    state["app"] = app

    def win():
        return app.windows[0]

    def ready(tries=0):
        w = win()
        if w is None or not w.get_realized():
            if tries >= 60:                     # 6 s ceiling
                check("window_realized", False, str(w))
                app.quit()
                return False
            GLib.timeout_add(100, ready, tries + 1)
            return False
        check("window_realized", True, f"{w.get_width()}x{w.get_height()}")
        state["win"] = w
        run_checks()
        app.quit()
        return False

    def run_checks():
        try:
            # GTK widgets need the app's display, so the fixture-level
            # evidence runs here (inside the main loop), not at import.
            fixture_evidence()
            strip_helper_negative_control()
            settings_checks()
            structural_checks(state["win"])
            wiring_checks(state["win"])
        except Exception as exc:                # noqa: BLE001
            import traceback
            traceback.print_exc()
            check("structural_checks_raised", False, repr(exc))

    GLib.timeout_add(300, ready)
    return False


def main() -> int:
    rc = reelsee.run(["reelsee"] + FILES[:1], on_app=probe_schedule)
    check("app_rc", rc == 0, str(rc))
    for k in ("fresh_handle", "video_kinds", "header_kinds"):
        print(f"R17_INVENTORY {k}={state.get(k)}", flush=True)
    finish()
    return 0


if __name__ == "__main__":
    sys.exit(main())
