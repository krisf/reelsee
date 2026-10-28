#!/usr/bin/python3
"""R14-A + R17-A gate: OUR right-click menu survives the GNOME window menu.

The defect: with a CSD window, button-3 over the titlebar-side regions
(the stub strip, the top chrome, the bottom bar, the CSD edges) was routed
by GTK's own titlebar machinery into Gdk.Toplevel.show_window_menu — the
WM window menu ate ours before the video-area gesture ever saw the press.

R17 made the failure mode measurable, and exposed why R14's own gate could
not see it (three independent vacuities, all fixed here):

  1. `Gtk.WindowHandle` pre-installs a C `Gtk.GestureClick` (button 0 = ANY
     button) whose button-3 path calls `gdk_toplevel_show_window_menu()`
     through the *vfunc*.  A Python monkeypatch of
     `Gdk.Toplevel.show_window_menu` is never consulted on that path, so
     R14's spy counted 0 SPURIOUSLY.
  2. Xvfb has no window manager, so even a real `_GTK_SHOW_WINDOW_MENU`
     message had nowhere to go.
  3. The fake WM this gate stages advertised only `_NET_WM_MOVERESIZE` in
     `_NET_SUPPORTED` — GDK refuses to send `_GTK_SHOW_WINDOW_MENU` unless
     the screen advertises THAT atom, so the message was unsendable here
     even in principle.

Now the fake WM advertises both, and the gate asserts at three levels:

  * STRUCTURAL (R17-A): GTK's C `Gtk.GestureClick` is gone from BOTH
    `Gtk.WindowHandle`s (video + header) while the `Gtk.GestureDrag` every
    drag feature depends on survives, the app-owned replacements are
    present, and the three `gtk-titlebar-*-click` settings read 'none';
  * WIRE: no `_GTK_SHOW_WINDOW_MENU` client message naming our window for
    the whole run, with a NEGATIVE CONTROL that puts the message on the
    wire deliberately (a throwaway control window whose C click gesture is
    left intact, the pin temporarily disconnected, the setting set back to
    'menu') and then shows that removing exactly that one controller stops
    it — so "0 messages" can never again be a vacuous pass;
  * FUNCTIONAL: synthetic button-3 at six positions — video centre, 8 px
    from the top edge, over the top chrome, over the visible header strip,
    over the bottom bar and the bottom-right corner — opens exactly ONE of
    OUR popovers every time with its anchor within 40 px of the pointer.

Every position assertion is computed against the pointer in the popover's
own coordinate space (view.area), so 40 px is the real on-screen error.
"""
import os
import sys

from smoke_harness import SCRATCH, check, finish

CFG_DIR = SCRATCH / "rclickwm-cfg"
STATE_DIR = SCRATCH / "rclickwm-state"
for _d in (CFG_DIR, STATE_DIR):
    _d.mkdir(parents=True, exist_ok=True)
os.environ["REELSEE_CONFIG_DIR"] = str(CFG_DIR)
os.environ["REELSEE_STATE_DIR"] = str(STATE_DIR)
for _f in (CFG_DIR / "config.json", STATE_DIR / "resume.json",
           STATE_DIR / "window.json"):
    if _f.exists():
        _f.unlink()

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk  # noqa: E402

import reelsee  # noqa: E402
import window as W  # noqa: E402  — importing the module installs window.py's
# R14-A refusal of Gdk.Toplevel.show_window_menu at import time (the spy
# below must therefore see IT as the wrap target), and it exposes the R17-A
# helpers the structural assertions need: describe_controllers(),
# strip_handle_click_gestures(), TITLEBAR_CLICK_SETTINGS and
# TITLEBAR_PIN_IDS (the last one so this gate can disconnect the pin, drive
# GTK's C path on purpose, and put the pin back).

FILES = ["testdata/perf_1080p30.mp4"]
state: dict = {}


def install_menu_spy():
    """Spy the window-menu seam BEFORE any window is built.  Records every
    Python-level call; returns the name of what it wrapped (the fix's
    refusal must be what the spy found)."""
    state["menu_calls"] = 0
    orig = Gdk.Toplevel.show_window_menu
    state["wrapped"] = getattr(orig, "__name__", "?")

    def spy(self, *a, **k):
        state["menu_calls"] += 1
        return False
    Gdk.Toplevel.show_window_menu = spy


def schedule(app):
    from gi.repository import GLib, Gtk
    state["app"] = app

    def win():
        return app.windows[0]

    def guard(fn):
        def run(*a):
            try:
                return fn(*a)
            except Exception as exc:                # noqa: BLE001
                import traceback
                traceback.print_exc()
                check(fn.__name__ + "_crashed", False, repr(exc))
                app.quit()
                return False
        run.__name__ = fn.__name__
        return run

    def again(fn, ms):
        GLib.timeout_add(ms, fn)
        return False

    def wait_for(desc, pred, then, left=40):
        try:
            val = pred()
        except Exception:                           # noqa: BLE001
            val = None
        if val:
            check(desc, True, "ok" if val is True else str(val))
            return again(guard(then), 80)
        if left <= 0:
            check(desc, False, "timeout")
            app.quit()
            return False
        return again(lambda: wait_for(desc, pred, then, left - 1), 120)

    # ------------------------------------------------------ X plumbing
    def xopen():
        from Xlib import display as xdisplay
        state["xd"] = xdisplay.Display(os.environ.get("DISPLAY", ":0"))

    def xfind():
        from Xlib import X
        d = state["xd"]
        root = d.screen().root
        found = [None]
        big = [None, 0]

        def name_of(w):
            try:
                n = w.get_wm_name()
                return n.decode("utf-8", "replace") if isinstance(
                    n, bytes) else (n or None)
            except Exception:                       # noqa: BLE001
                return None

        def mapped(w):
            try:
                return w.get_attributes().map_state == X.IsViewable
            except Exception:                       # noqa: BLE001
                return False

        def walk(w, depth=0):
            if found[0] is not None or depth > 6:
                return
            nm = name_of(w)
            if nm and "REELSEE" in nm and mapped(w) and w != root:
                found[0] = w
                return
            if mapped(w) and w != root and depth <= 2:
                try:
                    g = w.get_geometry()
                    area = g.width * g.height
                except Exception:                   # noqa: BLE001
                    area = 0
                if g.width >= 800 and g.height >= 400 and (
                        big[0] is None or area > big[1]):
                    big[0], big[1] = w, area
            try:
                kids = w.query_tree().children
            except Exception:                       # noqa: BLE001
                kids = []
            for k in kids:
                walk(k, depth + 1)
        walk(root)
        return found[0] or big[0]

    def wpos():
        g = state["winx"].get_geometry()
        tr = state["winx"].translate_coords(state["xd"].screen().root,
                                            0, 0)
        return tr.x, tr.y, g.width, g.height


    def gtk_coords(px, py):
        """Root coords (where XTEST warped) -> the popover's own frame of
        reference (the coordinate space _open_context_menu receives and
        sets pointing_to in).  Mapping is an affine unit map root→surface
        →area; the two unknown constants are calibrated ONCE at runtime
        (state["delta"], from a throwaway open) — surface offset (CSD
        shadow) and area origin need no assumptions."""
        d = state["xd"]
        tr = state["winx"].translate_coords(d.screen().root, 0, 0)
        dx, dy = state["delta"]
        return px - tr.x - dx, py - tr.y - dy

    def device_xy():
        """Pointer position in SURFACE coordinates via GDK itself."""
        w = win()
        dev = w.get_display().get_default_seat().get_pointer()
        pos = w.get_surface().get_device_position(dev)
        if isinstance(pos[0], bool):                # (ok, x, y, mods)?
            pos = pos[1:]
        return float(pos[0]), float(pos[1])

    def fake_button(kind, x, y, button=1):
        from Xlib import X
        from Xlib.ext import xtest
        d = state["xd"]
        d.screen().root.warp_pointer(x, y)
        d.sync()
        et = X.ButtonPress if kind == "press" else X.ButtonRelease
        xtest.fake_input(d, et, button)
        d.sync()

    def click(x, y, button=1, then=None, gap=60):
        def release():
            fake_button("release", x, y, button)
            return again(then, 450)
        fake_button("press", x, y, button)
        return again(release, gap)

    def clicks_then(n, fn, x0, y0, gap=120):
        """n real click pairs, 90 ms apart — inside GDK's double-click
        window (the smoke_drag pattern)."""
        done = {"k": 0}

        def press():
            fake_button("press", x0, y0)
            return again(release, 90)

        def release():
            fake_button("release", x0, y0)
            done["k"] += 1
            if done["k"] < n:
                return again(press, max(30, gap - 90))
            return again(fn, 600)
        return again(guard(press), 30)

    def xkey(keysym):
        from Xlib import X, XK
        from Xlib.ext import xtest
        d = state["xd"]
        kc = d.keysym_to_keycode(getattr(XK, keysym))
        xtest.fake_input(d, X.KeyPress, kc)
        d.sync()
        xtest.fake_input(d, X.KeyRelease, kc)
        d.sync()

    def paused():
        v = win().core.snapshot.get("pause")
        return None if v is None else bool(v)

    # ------------------------------------------------------ wire plumbing
    def wire_on():
        """Select SubstructureNotify on root from the APP-side connection
        and intern the window-menu atom.  Done up front (not in the drag
        stage as before) because the R17 negative control needs client
        messages long before a drag happens."""
        from Xlib import X
        d = state["xd"]
        d.screen().root.change_attributes(
            event_mask=X.SubstructureNotifyMask)
        d.sync()
        state["smenu"] = d.intern_atom("_GTK_SHOW_WINDOW_MENU")
        state["sm_main"] = 0
        state["sm_other"] = 0

    def our_xids():
        """Both spellings of the app window's X id: the Xlib window found
        by name, and the id GDK reports for the surface (for a CSD window
        these are the same outer window, but never assume it)."""
        ids = set()
        if state.get("winx") is not None:
            ids.add(int(state["winx"].id))
        try:
            ids.add(int(win().get_surface().get_xid()))
        except Exception:                           # noqa: BLE001
            pass
        return ids

    def drain_wire(tag=""):
        """Count _GTK_SHOW_WINDOW_MENU client messages since the last drain,
        attributed to our window vs. any other.  Returns (ours, others)."""
        from Xlib import X
        d = state["xd"]
        ours = others = 0
        ids = our_xids()
        while d.pending_events():
            ev = d.next_event()
            if getattr(ev, "type", None) != X.ClientMessage:
                continue
            if getattr(ev, "client_type", None) != state.get("smenu"):
                continue
            wv = getattr(ev, "window", None)
            wid = getattr(wv, "id", wv)
            try:
                wid = int(wid)
            except Exception:                       # noqa: BLE001
                pass
            if wid in ids:
                ours += 1
                state["sm_main"] += 1
            else:
                others += 1
                state["sm_other"] += 1
            print(f"SMOKE_SHOWMENU {tag} window={wid}", flush=True)
        return ours, others

    # ------------------------------------------------ right-click battery
    def rclick_at(name, px, py, then):
        """One synthetic button-3 at (px, py): exactly ONE open call, its
        anchor within 40 px of the pointer, popover visible, spy silent."""
        spy_before = state["menu_calls"]
        n_before = len(state["open_calls"])

        def after():
            w = win()
            pop = w._ctx_pop
            calls = state["open_calls"]
            delta = len(calls) - n_before
            opened = delta == 1
            vis = pop.get_visible()
            dx = dy = None
            if opened:
                gx, gy = gtk_coords(px, py)
                cx, cy = calls[-1]
                dx, dy = round(cx - gx), round(cy - gy)
            ok = (opened and vis and dx is not None
                  and abs(dx) <= 40 and abs(dy) <= 40)
            check(f"rclick_{name}_opens_menu", ok,
                  f"opened={opened} visible={vis} off=({dx},{dy}) "
                  f"calls={calls[n_before:]}")
            check(f"rclick_{name}_single_open", delta == 1,
                  f"open_delta={delta}")
            check(f"rclick_{name}_spy_zero",
                  state["menu_calls"] == spy_before,
                  f"spy_calls={state['menu_calls']}")
            # R17: WIRE-level, not spy-level — the message GDK would send
            # for a WM window menu must not name our window.  The negative
            # control proves this counter is not vacuous.
            ours, others = drain_wire(f"rclick_{name}")
            check(f"rclick_{name}_no_showmenu_wire", ours == 0,
                  f"our_msgs={ours} other_msgs={others}")
            return again(then, 250)
        return click(px, py, 3, guard(after))

    def dismiss(then, desc=None):
        """Escape closes the popover; the NAMED check runs once (first
        dismiss) so the battery sees a unique line."""
        xkey("XK_Escape")

        def after():
            hidden = not win()._ctx_pop.get_visible()
            if desc:
                check(desc, hidden, "hidden")
            return again(then, 250)
        return again(guard(after), 350)

    def s_calibrate():
        """One throwaway right-click inside the surface: its recorded
        anchor pins root+X-origin -> popover coords (state["delta"]), so
        every later position check is an ABSOLUTE "menu at the pointer"
        assertion, not just a delta between clicks."""
        x0, y0, wd, ht = wpos()
        px, py = x0 + 40, y0 + ht - 40
        n0 = len(state["open_calls"])

        def after():
            calls = state["open_calls"]
            if len(calls) != n0 + 1 or not win()._ctx_pop.get_visible():
                check("calibrated", False,
                      f"throwaway open failed delta={len(calls) - n0}")
                app.quit()
                return False
            tr = state["winx"].translate_coords(
                state["xd"].screen().root, 0, 0)
            state["delta"] = ((px - tr.x) - calls[-1][0],
                              (py - tr.y) - calls[-1][1])
            dv = ""
            try:                                    # independent cross-read
                sx, sy = device_xy()
                dv = (f" device=({sx:.0f},{sy:.0f}) "
                      f"surf=({px - tr.x:.0f},{py - tr.y:.0f})")
            except Exception:                       # noqa: BLE001
                dv = " (device_xy unavailable)"
            check("calibrated", True,
                  f"delta=({state['delta'][0]:.1f},"
                  f"{state['delta'][1]:.1f}){dv}")
            return dismiss(guard(
                lambda: again(guard(lambda: positions(s_regress)), 150)))
        return click(px, py, 3, guard(after))

    def positions(then):
        x0, y0, wd, ht = wpos()
        cx, cy = x0 + wd // 2, y0 + ht // 2
        cps = [
            lambda k: rclick_at("video_center", cx, cy, k),
            lambda k: rclick_at("top_edge", x0 + wd // 2, y0 + 8, k),
            lambda k: rclick_at("top_chrome", x0 + wd // 3, y0 + 22, k),
            # R17-A: explicitly over the VISIBLE HEADER STRIP's empty area
            # (left of the window controls, on the title label) — the place
            # where GTK's removed C click gesture used to raise mutter's
            # menu.  Our popover must own it.
            lambda k: rclick_at("header_strip", x0 + 120, y0 + 18, k),
            lambda k: rclick_at("bottom_bar", x0 + wd // 2, y0 + ht - 30, k),
            lambda k: rclick_at("bottom_corner", x0 + wd - 16,
                                y0 + ht - 16, k),
        ]

        def run(i):
            if i >= len(cps):
                return again(guard(then), 200)

            def after():
                # dismiss THIS menu (Escape), then move to the next point;
                # the FIRST dismiss carries the named check (unique line)
                return dismiss(guard(lambda: again(
                    lambda: run(i + 1), 100)),
                    desc="escape_closes_menu" if i == 0 else None)
            return cps[i](guard(after))
        return run(0)

    # ------------------------------------------------------ the schedule
    def s_structural():
        w = win()
        if not w.get_realized() or state.get("winx") is None:
            return again(s_structural, 120)
        # GTK exposes NO public controller getter; this app keeps its
        # controllers addressable (the _click_gestures convention), so the
        # gate inspects the toplevel button-3 owner the same way.
        top = getattr(w, "_rclick_top", None)
        caps = [c for c in (top,) if c is not None
                and isinstance(c, Gtk.GestureClick)
                and c.get_button() == 3
                and c.get_propagation_phase()
                == Gtk.PropagationPhase.CAPTURE
                and c.get_widget() is w]
        check("toplevel_capture_controller", bool(caps),
              f"capture button-3 controllers on window: {len(caps)}")
        check("titlebar_stub_inert",
              w._titlebar_stub.get_can_target() is False,
              f"can_target={w._titlebar_stub.get_can_target()}")
        check("menu_refused_at_seam",
              getattr(Gdk.Toplevel, "_reelsee_menu_refused", False) is True
              and state["wrapped"] == "_refused_show_window_menu",
              f"flag={getattr(Gdk.Toplevel, '_reelsee_menu_refused', None)} "
              f"spy_wrapped={state['wrapped']!r}")
        btns = sorted(c.get_button() for c in w._click_gestures)
        check("area_rclick_fallback_kept", btns == [1, 3], str(btns))
        # ---- R17-A: the C-installed click gesture is GONE from both handles
        for which, handle, audit in (
                ("video", w._handle, w._handle_click_audit),
                ("header", w._top_handle, w._top_handle_click_audit)):
            kinds = W.describe_controllers(handle)
            check(f"r17_{which}_handle_had_c_click", audit["before"] >= 1,
                  f"before={audit['before']} {audit['kinds_before']}")
            check(f"r17_{which}_handle_click_free",
                  audit["after"] == 0
                  and not any(k.startswith("GestureClick") for k in kinds),
                  f"after={audit['after']} {kinds}")
            check(f"r17_{which}_handle_drag_kept",
                  audit["drag_kept"] is True
                  and any(k.startswith("GestureDrag(button=1,phase=bubble)")
                          for k in kinds), str(kinds))
        check("r17_app_move_drag_on_both_handles",
              all(w._move_drags[k] in h.observe_controllers()
                  for k, h in (("video", w._handle),
                               ("header", w._top_handle))),
              f"video={W.describe_controllers(w._handle)} "
              f"header={W.describe_controllers(w._top_handle)}")
        check("r17_header_dblclick_gesture_owned",
              w._hdr_dblclick in w.top_bar.observe_controllers(),
              str(W.describe_controllers(w.top_bar)))
        st = Gtk.Settings.get_default()
        vals = ({p: st.get_property(p) for p in W.TITLEBAR_CLICK_SETTINGS}
                if st is not None else {})
        check("r17_titlebar_click_settings_off",
              st is not None and all(
                  v == W.TITLEBAR_CLICK_OFF
                  for v in vals.values()), str(vals))
        check("r17_settings_pin_watches_installed",
              st is not None and len(W.TITLEBAR_PIN_IDS)
              == len(W.TITLEBAR_CLICK_SETTINGS), str(W.TITLEBAR_PIN_IDS))
        # ---- wire observation up front (the negative control needs it)
        wire_on()
        state["xid"] = state["winx"].id
        drain_wire("baseline")
        # record every menu popup's (x, y) — same spy r6menu uses
        state["open_calls"] = []
        orig = w._open_context_menu

        def wrap(x, y):
            state["open_calls"].append((x, y))
            return orig(x, y)
        w._open_context_menu = wrap
        check("video_loaded", w.core.snapshot.get("dwidth") == 1920,
              f"dwidth={w.core.snapshot.get('dwidth')}")
        return again(guard(s_neg_setup), 150)

    # ------------------------------------------------ R17-A negative control
    def s_neg_setup():
        """Prove this fixture can OBSERVE the failure at all.

        R14's gate asserted "show_window_menu was never called" and passed
        while the defect was live, because the C code reaches the menu
        through the vfunc (no Python attribute lookup) and Xvfb neither has
        a WM nor advertised _GTK_SHOW_WINDOW_MENU.  So before trusting any
        "0 messages" count, build a THROWAWAY window whose Gtk.WindowHandle
        keeps GTK's C click gesture, disconnect the app's settings pin, put
        gtk-titlebar-right-click back to 'menu' — and require the client
        message to appear.  A gate that cannot make the message appear
        cannot prove it is absent."""
        st = Gtk.Settings.get_default()
        if st is None:
            check("r17_negative_control_settings", False, "no Gtk.Settings")
            return again(guard(s_pause), 100)
        for _i in list(W.TITLEBAR_PIN_IDS):
            st.disconnect(_i)
        W.TITLEBAR_PIN_IDS.clear()
        for p in W.TITLEBAR_CLICK_SETTINGS:
            st.set_property(p, "menu")
        state["neg_setting"] = st.get_property("gtk-titlebar-right-click")
        cw = Gtk.Window()                    # NOT registered with the app:
        # reelsee's own window bookkeeping must never see this window
        cw.set_default_size(420, 260)
        h = Gtk.WindowHandle()
        bx = Gtk.Box()
        bx.set_size_request(420, 260)
        h.set_child(bx)
        cw.set_child(h)
        state["neg_win"], state["neg_handle"] = cw, h
        state["neg_kinds"] = W.describe_controllers(h)
        cw.present()
        return wait_for("neg_control_mapped", lambda: cw.get_mapped(),
                        s_neg_place)

    def s_neg_place():
        """No WM here, so place the control window with raw Xlib and take
        its id from GDK.  It is parked CLEAR OF THE MAIN WINDOW (top-right
        of the screen) — button events follow the pointer, and an overlap
        would silently steal the clicks this stage and every later stage
        makes.  If the screen is too narrow for that, fall back to the
        bottom-right of the main window and click our window at its own
        bottom-right instead (state["neg_clear"])."""
        from Xlib import X
        cw = state["neg_win"]
        try:
            xid = int(cw.get_surface().get_xid())
        except Exception as exc:                    # noqa: BLE001
            check("r17_negative_control_xid", False, repr(exc))
            return again(guard(s_neg_repin), 100)
        state["neg_xid"] = xid
        x0, y0, wd, ht = wpos()
        px, py = x0 + wd + 40, y0 + 40
        if px + 460 > 1600:                         # screen too narrow
            px, py = x0 + wd - 460, y0 + ht - 300
            state["neg_clear"] = (x0 + 60, y0 + 60)
        else:
            state["neg_clear"] = (x0 + wd // 2, y0 + ht // 2)
        state["neg_pt"] = (px + 210, py + 130)
        xw = state["xd"].create_resource_object("window", xid)
        xw.configure(x=px, y=py)
        xw.set_input_focus(X.RevertToPointerRoot, X.CurrentTime)
        state["xd"].flush()
        state["xd"].sync()
        check("r17_negative_control_has_c_click",
              any(k.startswith("GestureClick(button=0")
                  for k in state["neg_kinds"]), str(state["neg_kinds"]))
        check("r17_negative_control_setting_menu",
              state["neg_setting"] == "menu", str(state["neg_setting"]))
        drain_wire("neg-place")
        return click(*state["neg_pt"], 3, guard(s_neg_after_present), gap=140)

    def s_neg_after_present():
        """The message MUST be there — this is the negative control."""
        ours, others = drain_wire("neg-control")
        check("r17_negative_control_sees_wm_menu",
              others == 1 and ours == 0,
              f"ctl_msgs={others} our_msgs={ours}")
        removed = [c for c in state["neg_handle"].observe_controllers()
                   if isinstance(c, Gtk.GestureClick)]
        for c in removed:
            state["neg_handle"].remove_controller(c)
        check("r17_negative_control_removed_one", len(removed) == 1,
              f"removed={len(removed)} "
              f"{W.describe_controllers(state['neg_handle'])}")
        drain_wire("neg-strip")
        return click(*state["neg_pt"], 3, guard(s_neg_after_removed), gap=140)

    def s_neg_after_removed():
        """Same window, same setting, same pointer: removing exactly the one
        C GestureClick controller stops the message.  So the fix is the
        removal, not the settings pin."""
        ours, others = drain_wire("neg-control")
        check("r17_removal_stops_wm_menu", others == 0 and ours == 0,
              f"ctl_msgs={others} our_msgs={ours}")
        state["neg_open_n"] = len(state["open_calls"])
        return click(*state["neg_clear"], 3,
                     guard(s_neg_after_ours), gap=140)

    def s_neg_after_ours():
        """And OUR window, with the pin DISCONNECTED and the setting back on
        'menu', still sends nothing: the structural removal alone protects
        it.  (Our popover must still open — the menu is ours, not mutter's.)"""
        ours, others = drain_wire("neg-ours")
        check("r17_structural_removal_alone_protects", ours == 0,
              f"our_msgs={ours} other_msgs={others}")
        check("r17_our_menu_still_opens_unpinned",
              len(state["open_calls"]) == state["neg_open_n"] + 1
              and win()._ctx_pop.get_visible(),
              f"open_delta={len(state['open_calls']) - state['neg_open_n']}")
        return dismiss(guard(s_neg_repin))

    def s_neg_repin():
        """Put the pin back exactly as the app installed it, and destroy the
        control window before any later stage runs.  Focus is handed back
        explicitly: the control window was given input focus for its own
        clicks, and `RevertToPointerRoot` would otherwise leave focus at the
        root — every later stage drives input through the main window."""
        from Xlib import X
        cw = state.get("neg_win")
        if cw is not None:
            cw.destroy()
            state["neg_win"] = None
        ok = W.harden_titlebar_click_settings()
        st = Gtk.Settings.get_default()
        vals = ({p: st.get_property(p) for p in W.TITLEBAR_CLICK_SETTINGS}
                if st is not None else {})
        check("r17_pin_restored",
              ok and len(W.TITLEBAR_PIN_IDS) == len(W.TITLEBAR_CLICK_SETTINGS)
              and all(v == W.TITLEBAR_CLICK_OFF for v in vals.values()),
              f"hardened={ok} ids={len(W.TITLEBAR_PIN_IDS)} {vals}")
        w = win()
        w.present()
        try:
            state["winx"].set_input_focus(X.RevertToParent, X.CurrentTime)
        except Exception:                           # noqa: BLE001
            pass
        state["xd"].sync()
        drain_wire("neg-end")
        return again(guard(s_pause), 300)


    def s_pause():
        win().core.set_property("pause", True)
        return wait_for("paused_for_positions", lambda: paused() is True,
                        s_revealed)

    def s_revealed(left=40):
        w = win()
        ok = bool(w.top_bar.get_can_target()
                  and w.chrome.bottom.get_can_target())
        if ok:
            check("chrome_revealed", True, "top+bottom targetable")
            return again(guard(s_calibrate), 300)
        if left <= 0:
            check("chrome_revealed", False, "bars never became targetable")
            app.quit()
            return False
        # harmless wake: pointer MOTION over the video (a click here would
        # toggle playback and poison the later pause-state asserts)
        x0, y0, wd, ht = wpos()
        state["xd"].screen().root.warp_pointer(x0 + wd // 2, y0 + ht - 5)
        state["xd"].sync()
        return again(lambda: s_revealed(left - 1), 150)

    def s_regress():
        # the last dismiss already closed the menu; state it for the log
        check("menu_hidden_after_positions",
              not win()._ctx_pop.get_visible(), "hidden")
        # right-DRAG: press, move far, release -> still no menu (R6-A rule)
        x0, y0, wd, ht = wpos()
        cx, cy = x0 + wd // 2, y0 + ht // 2
        n0 = len(state["open_calls"])
        fake_button("press", cx, cy, button=3)

        def rd2():
            fake_button("release", cx + 90, cy + 40, button=3)

            def rd3():
                check("right_drag_no_menu",
                      len(state["open_calls"]) == n0
                      and not win()._ctx_pop.get_visible(),
                      f"open_delta={len(state['open_calls']) - n0}")
                return again(guard(s_left_single), 250)
            return again(guard(rd3), 450)
        return again(guard(rd2), 120)

    def s_left_single():
        # single left click RESUMES (we are paused); second click pauses.
        # R17-C regression guard: the app-owned move drag must not claim a
        # press that never travelled — a click without motion is a click.
        # The counter is re-read in s_double_check: the whole click battery
        # (single, single, double) must never initiate a window move.
        state["move_before_clicks"] = win()._move_initiated
        x0, y0, wd, ht = wpos()
        cx, cy = x0 + wd // 2, y0 + ht // 2
        return click(cx, cy, 1, guard(
            lambda: wait_for("left_click_resumes",
                             lambda: paused() is False,
                             s_left_pause2)))

    def s_left_pause2():
        x0, y0, wd, ht = wpos()
        cx, cy = x0 + wd // 2, y0 + ht // 2
        return click(cx, cy, 1, guard(
            lambda: wait_for("left_click_pauses",
                             lambda: paused() is True,
                             s_double)))

    def s_double():
        x0, y0, wd, ht = wpos()
        cx, cy = x0 + wd // 2, y0 + ht // 2
        return clicks_then(2, guard(s_double_check), cx, cy)

    def s_double_check():
        w = win()
        check("double_click_fullscreen_no_pause", bool(w._fs)
              and paused() is True,
              f"fs={w._fs} pause={paused()}")
        # R17-C: three presses in a row on the video area are clicks, not a
        # drag — none of them may have started a window move.
        check("clicks_never_move_the_window",
              w._move_initiated == state.get("move_before_clicks", 0),
              f"move_init {state.get('move_before_clicks')} -> "
              f"{w._move_initiated}")
        w._act_fullscreen_exit()
        return wait_for("fullscreen_exited", lambda: not w._fs, s_drag_move)

    def s_drag_move():
        # Left drag must STILL initiate a window move.  No WM here, so the
        # proof is the protocol itself: GDK sends _NET_WM_MOVERESIZE to the
        # ROOT window — exactly what a real WM acts on.  GDK refuses to
        # even try unless the screen advertises a WM, so a textbook-
        # minimal EWMH presence was staged before the app started (see
        # stage_wm_presence).  We select SubstructureNotify on root and
        # watch for the message.
        from Xlib import X
        d = state["xd"]
        root = d.screen().root
        # (EWMH presence was staged by stage_wm_presence() before the
        # app opened its display — GDK caches the check window.)
        # python-xlib 0.33 exposes no ChangeEventSelection wrapper;
        # ChangeWindowAttributes on root selects events just as well.
        root.change_attributes(event_mask=X.SubstructureNotifyMask)
        d.sync()
        while d.pending_events():
            d.next_event()                          # drain noise
        mt = d.intern_atom("_NET_WM_MOVERESIZE")
        our_xid = state["winx"].id
        x0, y0, wd, ht = wpos()
        cx, cy = x0 + wd // 2, y0 + ht // 2
        pause0 = paused()
        fake_button("press", cx, cy, button=1)

        def step(i):
            if i <= 6:
                root.warp_pointer(cx + 90 * i // 6, cy + 60 * i // 6)
                d.sync()
                return again(guard(lambda: step(i + 1)), 35)
            root.warp_pointer(cx + 90, cy + 60)
            d.sync()
            fake_button("release", cx + 90, cy + 60, button=1)
            return again(guard(s_drag_check), 450)
        state["mt"] = mt
        state["xid"] = our_xid
        state["pause0"] = pause0
        return again(guard(lambda: step(1)), 60)

    def s_drag_check():
        from Xlib import X
        d = state["xd"]
        seen_move = False
        while d.pending_events():
            ev = d.next_event()
            if (getattr(ev, "type", None) == X.ClientMessage
                    and getattr(ev, "client_type", None) == state["mt"]):
                # GDK names the window in the event's OWN window field
                # (gtk's send_moveresize packs data as [x, y, direction,
                # button, user_time]); this python-xlib build marshals it
                # as a Window object.
                wv = getattr(ev, "window", None)
                wid = getattr(wv, "id", wv)
                if wid == state["xid"]:
                    seen_move = True
        check("left_drag_moveresize_sent", seen_move,
              "_NET_WM_MOVERESIZE client message with our xid at root")
        check("left_drag_no_pause", paused() is state["pause0"],
              f"pause={paused()}")
        return again(guard(s_fin), 200)

    def s_fin():
        check("window_menu_never_called", state["menu_calls"] == 0,
              f"spy_calls={state['menu_calls']}")
        drain_wire("final")
        check("no_showmenu_wire_for_our_window", state["sm_main"] == 0,
              f"our_msgs={state['sm_main']} (negative control saw "
              f"{state['sm_other']} on the unstripped control window)")
        check("window_alive", win().get_realized(), "True")
        app.quit()
        return False

    def start(left=40):
        w = None
        try:
            w = app.windows[0]
        except IndexError:
            pass
        if w is None or not w.get_realized():
            return again(lambda: start(left - 1), 200)
        if "xd" not in state:
            xopen()
        x = xfind()
        if x is None:
            if left <= 0:
                check("x_window_found", False, "no REELSEE window")
                app.quit()
                return False
            return again(lambda: start(left - 1), 300)
        check("x_window_found", True, "mapped")
        state["winx"] = x
        w.present()
        state["xd"].sync()
        return again(guard(lambda: wait_for(
            "window_mapped", lambda: w.get_mapped(), s_structural)), 400)

    GLib.timeout_add(300, guard(start))


def stage_wm_presence():
    """Xvfb has no window manager; GDK refuses begin_move unless the
    screen advertises one (gdk_x11_screen_supports_net_wm_hint gates the
    whole path — confirmed by disassembly, and the WM-check window is
    cached at screen init).  Stage the textbook-minimal EWMH presence —
    an override-redirect check window named by root's
    _NET_SUPPORTING_WM_CHECK, and _NET_WM_MOVERESIZE in root's
    _NET_SUPPORTED — BEFORE the app opens its display.  Our connection
    stays open for the whole gate so the stub window survives.

    R17: `_GTK_SHOW_WINDOW_MENU` is advertised too.  GDK's titlebar click
    path checks THAT atom (gdk_x11_screen_supports_net_wm_hint on
    _GTK_SHOW_WINDOW_MENU) before sending the message, so a fake WM that
    lists only _NET_WM_MOVERESIZE makes the defect UNSHOWABLE here — which
    is one of the three reasons R14's gate passed vacuously.  With it
    advertised, the negative control below can put the message on the wire
    deliberately and the app's own right-clicks must produce none."""
    import os
    from Xlib import X
    from Xlib import display as xdisplay
    d = xdisplay.Display(os.environ.get("DISPLAY", ":0"))
    root = d.screen().root
    xa_atom = d.intern_atom("ATOM")
    xa_window = d.intern_atom("WINDOW")
    sup = d.intern_atom("_NET_SUPPORTING_WM_CHECK")
    supp = d.intern_atom("_NET_SUPPORTED")
    wmwin = root.create_window(0, 0, 1, 1, 0, X.CopyFromParent,
                               window_class=X.InputOutput,
                               override_redirect=1, event_mask=0)
    wmwin.map()
    wmwin.change_property(sup, xa_window, 32, [wmwin.id], X.PropModeReplace)
    root.change_property(sup, xa_window, 32, [wmwin.id], X.PropModeReplace)
    root.change_property(supp, xa_atom, 32,
                         [d.intern_atom("_NET_WM_MOVERESIZE"),
                          d.intern_atom("_GTK_SHOW_WINDOW_MENU")],
                         X.PropModeReplace)
    root.change_attributes(event_mask=X.SubstructureNotifyMask)
    d.flush()
    d.sync()
    return d, wmwin


def main() -> int:
    install_menu_spy()
    state["wmdisp"], state["wmwin"] = stage_wm_presence()
    rc = reelsee.run(["reelsee"] + FILES[:1], on_app=schedule)
    check("run_rc", rc == 0, str(rc))
    finish()
    return 0


if __name__ == "__main__":
    sys.exit(main())
