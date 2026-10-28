#!/usr/bin/python3
"""R15-A gate: press-and-drag the (flush) header MOVES the window.

MEASUREMENT NOTE — there is no window manager under the headless
harness.  GtkWindowHandle drags used to be a C-side call chain
(gesture -> gdk_toplevel_begin_move) that a Python monkeypatch COULD
NOT observe (probe-proven in R15: the spy stayed at 0 through real
drags).  The observable, wire-level proxy is the EWMH protocol:
begin_move against a recognised WM sends a `_NET_WM_MOVERESIZE`
ClientMessage to the root window carrying OUR xid.  The gate therefore
stages a minimal WM presence (1x1 override-redirect window +
_NET_SUPPORTING_WM_CHECK on root and that window + _NET_SUPPORTED ∋
_NET_WM_MOVERESIZE), watches the root for ClientMessages, and counts
DIRECTION WORDS: one gesture emits direction 8 (MOVE) plus direction 11
(CANCEL when the grab ends), so "exactly one begin_move" == exactly one
direction-8 message.
A POSITIVE CONTROL (drag the video, which has always been draggable)
proves the whole spy pipeline fires — every zero-elsewhere in this
gate is meaningful only because that control moves.

R17 UPDATE — the move is now app-owned (R17-C): the app's own drag
handler calls `Gdk.Toplevel.begin_move` at the threshold, so the Python
spy above became the TRUE seam and is asserted, with device/button/time.
Two new attributions: A1b proves a click WITHOUT motion never moves the
window, and A1c blocks the app call itself to prove it is the SOLE
initiator — the retained C drag stays silent once the app-owned gesture
claims the sequence (wire silent while blocked, one dir-8 when restored).

Known environment facts that shape the numbers: the handle honours
gtk-dnd-drag-threshold (measured 8 in this env), the app floors that
setting at window.MOVE_DRAG_MIN_PX = 4 px, and GTK coalesces 1 px
pointer warps (a 7 px drag in 7 steps delivered drag-update offsets of
only 1, 2, 3 before the release).  The three "no-move" cases and the
positive control run at the stock threshold; the empty-bar MOVE case
lowers it to the 4 px floor and travels 24 px in 6 px steps, so the
app's effective threshold is unambiguous and the gesture really does
cross it (an earlier draft lowered it to 2 and dragged 7 px, which the
floor rejected — the retained C drag then moved the window, and the
stage still went green because its assertion was not a delta).  The
BUTTON-CLAIM case restores the stock 8: at threshold 2 GTK cancels the
button's claim past the threshold, at 8 the 7 px release still lands
inside the button, exactly like a real desktop drag off a title button.

Checks:
  structural  WindowHandle in the overlay child list, carrying the
              bar; the video's own handle untouched; 6 overlay kids
              (the handle REPLACES the bar slot).
  A1          press + 24 px drag on the empty header -> ONE move msg, and
              the app-owned begin_move call spied at the seam (a DELTA of
              calls and of the app's own _move_initiated counter, with the
              origin asserted to be the header press — not the video
              centre the positive control dragged from), carrying the
              pointer device, the primary button and an event time.
  A1b         press + release on the empty header WITHOUT motion ->
              zero move messages and zero begin_move calls (a click
              stays a click; this is what keeps click-to-pause alive).
  A1c         same drag with begin_move BLOCKED (recorded, not called)
              -> exactly one app call and ZERO move messages; same drag
              with the spy restored -> exactly one move message.
  A2          press + 7 px drag starting on EACH of the three
              buttons -> ZERO move msgs AND that button's action
              fired EXACTLY once (spied at the window method).
  A3          right-press on the header -> OUR context menu opens
              (wrapped _open_context_menu +1, popover visible), zero
              move messages, zero _GTK_SHOW_WINDOW_MENU requests.
  A4          fullscreen -> no handle mounted; header drags move
              nothing; the swap restores on exit.
  A5          chrome faded (playing, pointer parked, seeded short
              timeout) -> bar AND handle can_target False, and a drag
              at the header position moves nothing (the video handle
              is detached for this phase — with the chrome hidden the
              video handle itself would move the window BY DESIGN and
              would mask what is being measured; documented).
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))
from smoke_harness import SCRATCH, check, finish  # noqa: E402
import shutil  # noqa: E402

CFG = SCRATCH / f"r15hdr-cfg-{os.getpid()}"
STATE = SCRATCH / f"r15hdr-state-{os.getpid()}"
CLIP = "testdata/perf_1080p30.mp4"
# window.MOVE_DRAG_MIN_PX — the app refuses to honour a desktop dnd
# threshold below this many pixels (tests/test_move_drag.py::Threshold).
# Stage A1 sets the setting TO this value so the app's effective threshold
# is unambiguous.  Hard-coded rather than imported: this gate runs in a
# subprocess whose sys.path holds scripts/, not src/.
MOVE_FLOOR_PX = 4.0
S: dict = {}


def stage_wm_presence():
    """Minimal fake WM: enough for GTK's begin_move to stop no-oping
    (gdk checks _NET_SUPPORTING_WM_CHECK / _NET_SUPPORTED).  With a WM
    present GDK requests fullscreen via EWMH instead of its no-WM
    fallback resize, so this also answers _NET_WM_STATE(FULLSCREEN)
    messages: it sets the state property (add → [FULLSCREEN],
    remove → [HIDDEN], a filler atom either way so GDK's own re-post
    of the notify cannot ping-pong) and configures the window to the
    screen rect / back to 950x530.  A separate connection; the spy
    keeps its own."""
    from Xlib import X
    from Xlib import display as xdisplay
    d = xdisplay.Display(os.environ.get("DISPLAY", ":0"))
    root = d.screen().root
    sup = d.intern_atom("_NET_SUPPORTING_WM_CHECK")
    supp = d.intern_atom("_NET_SUPPORTED")
    wmwin = root.create_window(0, 0, 1, 1, 0, X.CopyFromParent,
                               window_class=X.InputOutput,
                               override_redirect=1, event_mask=0)
    wmwin.map()
    wmwin.change_property(sup, d.intern_atom("WINDOW"), 32, [wmwin.id],
                          X.PropModeReplace)
    root.change_property(sup, d.intern_atom("WINDOW"), 32, [wmwin.id],
                         X.PropModeReplace)
    root.change_property(supp, d.intern_atom("ATOM"), 32,
                         [d.intern_atom("_NET_WM_MOVERESIZE")],
                         X.PropModeReplace)
    d.flush()
    d.sync()

    import threading

    def wm_loop():
        try:
            wd = xdisplay.Display(os.environ.get("DISPLAY", ":0"))
        except Exception:                           # noqa: BLE001
            return
        wroot = wd.screen().root
        nmstate = wd.intern_atom("_NET_WM_STATE")
        nfull = wd.intern_atom("_NET_WM_STATE_FULLSCREEN")
        nhidden = wd.intern_atom("_NET_WM_STATE_HIDDEN")
        scr = wd.screen()
        state_fs = [False]
        saved = []

        def apply_state(msg):
            try:
                data = msg.data
                action = data[1][0]
                a1 = data[1][1]
                a2 = data[1][2]
                wid = getattr(msg.window, "id", msg.window)
            except Exception:                       # noqa: BLE001
                return
            want = nfull in (a1, a2)
            if not want:
                return
            if action in (1, 2):                    # ADD / TOGGLE on
                if state_fs[0]:
                    return
                try:
                    g = wd.create_resource_object("window", wid)
                    gg = g.get_geometry()
                    saved.append((gg.x, gg.y, gg.width, gg.height))
                    g.configure(width=scr.width_in_pixels,
                                height=scr.height_in_pixels, x=0, y=0)
                    g.change_property(nmstate, "ATOM", 32, [nfull],
                                      X.PropModeReplace)
                    state_fs[0] = True
                    wd.flush()
                except Exception:                   # noqa: BLE001
                    pass
            elif action == 0:                       # REMOVE off
                if not state_fs[0]:
                    return
                try:
                    g = wd.create_resource_object("window", wid)
                    rect = saved.pop() if saved else (5, 5, 950, 530)
                    g.configure(x=rect[0], y=rect[1],
                                width=rect[2], height=rect[3])
                    g.change_property(nmstate, "ATOM", 32, [nhidden],
                                      X.PropModeReplace)
                    state_fs[0] = False
                    wd.flush()
                except Exception:                   # noqa: BLE001
                    pass

        wroot.change_attributes(
            event_mask=X.SubstructureNotifyMask | X.PropertyChangeMask)
        wd.sync()
        while True:
            try:
                ev = wd.next_event()
            except Exception:                       # noqa: BLE001
                break
            if getattr(ev, "type", None) == X.ClientMessage \
                    and ev.client_type == nmstate:
                apply_state(ev)
        return d, wmwin

    threading.Thread(target=wm_loop, daemon=True).start()
    return d, wmwin


def main() -> int:
    for d in (CFG, STATE):
        shutil.rmtree(d, ignore_errors=True)
        d.mkdir(parents=True)
    (CFG / "config.json").write_text(
        '{"ui": {"chrome_timeout_ms": 900}}', encoding="utf-8")
    os.environ["REELSEE_CONFIG_DIR"] = str(CFG)
    os.environ["REELSEE_STATE_DIR"] = str(STATE)

    import gi
    gi.require_version("Gtk", "4.0")
    from gi.repository import GLib, Gdk, Gtk

    import reelsee

    wmdisp, wmwin = stage_wm_presence()

    def schedule(app):
        S["app"] = app

        def win():
            return app.windows[0]

        def guard(fn):
            def run(*a):
                try:
                    return fn(*a)
                except Exception as exc:            # noqa: BLE001
                    import traceback
                    traceback.print_exc()
                    check(fn.__name__ + "_crashed", False, repr(exc))
                    finish_and_quit()
                    return False
            run.__name__ = fn.__name__
            return run

        def again(fn, ms=80):
            GLib.timeout_add(ms, fn)
            return False

        def wait_for(desc, pred, then, left=60):
            try:
                val = pred()
            except Exception:                       # noqa: BLE001
                val = None
            if val:
                check(desc, True, "ok" if val is True else str(val))
                return again(then)
            if left <= 0:
                check(desc, False, "timeout")
                finish_and_quit()
                return False
            return again(lambda: wait_for(desc, pred, then, left - 1), 120)

        def finish_and_quit():
            if S.get("done"):
                return
            S["done"] = True
            try:
                wmdisp.close()
            except Exception:                       # noqa: BLE001
                pass
            app.quit()

        # ----------------------------------------------------- X plumbing
        def xopen():
            import Xlib.display
            S["xd"] = Xlib.display.Display(os.environ.get("DISPLAY", ":0"))
            from Xlib import X
            S["mt"] = S["xd"].intern_atom("_NET_WM_MOVERESIZE")
            S["gtkmenu"] = S["xd"].intern_atom("_GTK_SHOW_WINDOW_MENU")
            S["xd"].screen().root.change_attributes(
                event_mask=X.SubstructureNotifyMask)
            S["xd"].sync()

        def xfind():
            from Xlib import X
            d = S["xd"]
            root = d.screen().root
            found = [None]
            big = [None, 0]

            def name_of(w):
                try:
                    n = w.get_wm_name()
                    return n.decode("utf-8", "replace") if isinstance(
                        n, bytes) else (n or None)
                except Exception:                   # noqa: BLE001
                    return None

            def mapped(w):
                try:
                    return w.get_attributes().map_state == X.IsViewable
                except Exception:                   # noqa: BLE001
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
                    except Exception:               # noqa: BLE001
                        area = 0
                    if g.width >= 800 and g.height >= 400 and (
                            big[0] is None or area > big[1]):
                        big[0], big[1] = w, area
                try:
                    kids = w.query_tree().children
                except Exception:                   # noqa: BLE001
                    kids = []
                for k in kids:
                    walk(k, depth + 1)
            walk(root)
            return found[0] or big[0]

        def wpos():
            g = S["winx"].get_geometry()
            tr = S["winx"].translate_coords(S["xd"].screen().root, 0, 0)
            return tr.x, tr.y, g.width, g.height

        def fake_button(kind, x, y, button=1):
            from Xlib import X
            from Xlib.ext.xtest import fake_input
            d = S["xd"]
            d.screen().root.warp_pointer(int(x), int(y))
            d.sync()
            et = X.ButtonPress if kind == "press" else X.ButtonRelease
            fake_input(d, et, button)
            d.sync()

        def xkey(name):
            from Xlib import X, XK
            from Xlib.ext.xtest import fake_input
            d = S["xd"]
            ks = getattr(XK, name if name.startswith("XK_")
                         else "XK_" + name)
            kc = d.keysym_to_keycode(ks)
            fake_input(d, X.KeyPress, kc)
            d.sync()
            fake_input(d, X.KeyRelease, kc)
            d.sync()

        def drain8():
            """(n_move, n_cancel, n_showmenu) since last drain; the
            EWMH direction word decides: 8 = begin move, 11 = cancel.
            Only messages addressed to OUR xid count."""
            from Xlib import X
            d = S["xd"]
            n8 = n11 = nsm = 0
            while d.pending_events():
                ev = d.next_event()
                if getattr(ev, "type", None) != X.ClientMessage:
                    continue
                if ev.client_type == S["mt"]:
                    wv = getattr(ev, "window", None)
                    wid = getattr(wv, "id", wv)
                    if wid == S["winx"].id:
                        try:
                            direction = ev.data[1][2]
                        except Exception:           # noqa: BLE001
                            direction = "?"
                        S.setdefault("dirs", []).append(direction)
                        if direction == 8:
                            n8 += 1
                        elif direction == 11:
                            n11 += 1
                elif ev.client_type == S["gtkmenu"]:
                    nsm += 1
            if n8 or n11 or nsm:
                print(f"XMSG: n8={n8} n11={n11} nsm={nsm} "
                      f"dirs={S.get('dirs')}")
                S["dirs"] = []
            return n8, n11, nsm

        def drag(px, py, dx, dy, steps=7, button=1, done=None):
            """press at (px,py); warp in `steps` px steps; release."""
            px, py = int(px), int(py)
            drain8()
            fake_button("press", px, py, button)

            def step(i):
                if i <= steps:
                    S["xd"].screen().root.warp_pointer(
                        px + dx * i // max(1, steps),
                        py + dy * i // max(1, steps))
                    S["xd"].sync()
                    return again(guard(lambda: step(i + 1)), 35)
                fake_button("release", px + dx, py + dy, button)
                return again(guard(done), 450)
            return again(guard(lambda: step(1)), 60)

        def overlay_kids():
            kids = []
            c = win().overlay.get_first_child()
            while c is not None:
                kids.append(c)
                c = c.get_next_sibling()
            return kids

        def acc(widget):
            w = win()
            if widget is w.overlay:
                return 0.0, 0.0
            r = widget.compute_bounds(w.overlay)
            ok, rect = r[0], r[1]
            if not ok or rect is None:
                return 0.0, 0.0
            return float(rect.get_x()), float(rect.get_y())

        def bar_point():
            """Root pixel on the header's EMPTY area (over the title
            label, far left of the three buttons)."""
            w = win()
            bx, by = acc(w.top_bar)
            wx, wy = S["wo"]
            return (int(bx + 30 + wx),
                    int(by + w.top_bar.get_height() / 2.0 + wy))

        def wp(widget):
            x, y = acc(widget)
            w = win()
            wx, wy = S["wo"]
            return (int(wx + x + widget.get_width() / 2.0),
                    int(wy + y + widget.get_height() / 2.0))

        def set_thr(value):
            st = win().get_settings()
            st.set_property("gtk-dnd-drag-threshold", value)
            return int(st.get_property("gtk-dnd-drag-threshold"))

        # ------------------------------------------------ calibration
        def calib(then):
            """Motion spy on the bar pins widget (0,0) in root coords
            without circular device-position assumptions."""
            w = win()
            if "mc" not in S:
                mc = Gtk.EventControllerMotion()
                mc.connect("motion", lambda c, x, y: S.__setitem__(
                    "bar_motion", (float(x), float(y))))
                w.top_bar.add_controller(mc)
                S["mc"] = mc
            S.pop("bar_motion", None)
            S["try_y"] = 10
            S["xd"].screen().root.warp_pointer(70, S["try_y"])
            S["xd"].sync()
            return again(guard(lambda: calib_step(then)), 220)

        def calib_step(then, left=30):
            if "bar_motion" in S:
                w = win()
                mx, my = S["bar_motion"]
                bx, by = acc(w.top_bar)
                S["wo"] = (70.0 - mx - bx, float(S["try_y"]) - my - by)
                print(f"CALIB: widget0_in_root={S['wo']!r} "
                      f"bar_acc={(bx, by)}")
                check("calibrated", True, f"{S['wo']!r}")
                return again(guard(then), 150)
            if left <= 0:
                check("calibrated", False, "pointer never inside bar")
                finish_and_quit()
                return False
            S["try_y"] += 8
            S["xd"].screen().root.warp_pointer(70, S["try_y"])
            S["xd"].sync()
            return again(lambda: calib_step(then, left - 1), 200)

        # ------------------------------------------------------ phases
        def s_boot(left=60):
            try:
                w = win()
            except IndexError:
                w = None
            if w is None or not w.get_mapped():
                if left <= 0:
                    check("window_realized", False, "never realized")
                    finish_and_quit()
                    return False
                return again(lambda: s_boot(left - 1), 200)
            check("window_realized", True)
            xopen()
            x = xfind()
            if x is None:
                return again(lambda: s_boot(left - 1), 300)
            S["winx"] = x
            gtkv = (Gtk.get_major_version(), Gtk.get_minor_version(),
                    Gtk.get_micro_version())
            print(f"ENV: gtk={gtkv[0]}.{gtkv[1]}.{gtkv[2]} "
                  f"dnd_threshold="
                  f"{w.get_settings().get_property('gtk-dnd-drag-threshold')}")
            S["thr0"] = int(w.get_settings().get_property(
                "gtk-dnd-drag-threshold"))
            # spies (instance attributes — dynamic dispatch, proven
            # pattern from overlaychrome/rclickwm)
            S["min_calls"] = S["max_calls"] = S["close_calls"] = 0
            S["orig_min"], S["orig_close"] = w.minimize, w.close
            S["orig_max"], S["orig_unmax"] = w.maximize, w.unmaximize
            S["orig_open"] = w._open_context_menu
            w.minimize = lambda *a: S.__setitem__(
                "min_calls", S["min_calls"] + 1)
            w.close = lambda *a: S.__setitem__(
                "close_calls", S["close_calls"] + 1)
            w.maximize = lambda *a: S.__setitem__(
                "max_calls", S["max_calls"] + 1)
            w.unmaximize = lambda *a: S.__setitem__(
                "max_calls", S["max_calls"] + 1)
            S["open_calls"] = []
            orig_open = w._open_context_menu

            def wrap(x_, y_):
                S["open_calls"].append((x_, y_))
                return orig_open(x_, y_)
            w._open_context_menu = wrap
            # R17-B/C: the PYTHON begin_move spy is now the TRUE seam.  In
            # R15 it was informational only (C-originated drags bypass
            # Python), which the report documented; since R17 the app's own
            # drag handler calls Gdk.Toplevel.begin_move, so this spy sees
            # the real move initiation.  It still calls through, so the
            # wire-level (_NET_WM_MOVERESIZE) assertions keep their meaning.
            S["bm_calls"] = 0
            S["bm_args"] = []
            orig_bm = Gdk.Toplevel.begin_move

            def bm(self, *a, **k):
                S["bm_calls"] += 1
                S["bm_args"].append(a)
                return orig_bm(self, *a, **k)
            Gdk.Toplevel.begin_move = bm
            S["orig_bm"] = orig_bm
            w._load_path(CLIP)
            return again(guard(lambda: calib(s_pause_reveal)), 500)

        def s_pause_reveal():
            w = win()
            w.core.set_property("pause", True)
            return again(guard(lambda: wait_for(
                "revealed_for_struct",
                lambda: (w.core.snapshot.get("pause") is True
                         and w.top_bar.get_opacity() > 0.95),
                s_struct)), 100)

        def s_struct():
            w = win()
            kids = overlay_kids()
            handles = [c for c in kids
                       if isinstance(c, Gtk.WindowHandle)
                       and c is not w._handle]
            check("s_handle_is_overlay_child", len(handles) == 1,
                  f"{len(handles)} top WindowHandle(s) in overlay kids")
            h = handles[0] if handles else getattr(w, "_top_handle", None)
            check("s_handle_carries_the_bar",
                  h is not None and h.get_child() is w.top_bar,
                  f"child_is_bar="
                  f"{h.get_child() is w.top_bar if h else 'no handle'}")
            check("s_bar_parent_is_handle",
                  w.top_bar.get_parent() is h,
                  f"parent={type(w.top_bar.get_parent()).__name__}")
            check("s_video_base_untouched",
                  w.overlay.get_child() is w._handle
                  and w._handle.get_child() is w.view.area,
                  f"base={type(w.overlay.get_child()).__name__}")
            check("s_overlay_child_count", len(kids) == 6,
                  f"{len(kids)} kids (handle must REPLACE the bar)")
            check("s_handle_top_of_bar", h is not None
                  and h.get_valign() == Gtk.Align.START
                  and h.get_halign() == Gtk.Align.FILL,
                  f"valign="
                  f"{h.get_valign().value_nick if h else 'no handle'} "
                  f"halign="
                  f"{h.get_halign().value_nick if h else 'no handle'}")
            # ---- R17 structural: the C click gestures are gone from BOTH
            # handles (the round-17 root cause), and the app owns the move
            # drag + the header double-click.
            def _clicks(widget):
                return [c for c in widget.observe_controllers()
                        if isinstance(c, Gtk.GestureClick)]
            check("r17_video_handle_has_no_click_gesture",
                  not _clicks(w._handle),
                  f"{[type(c).__name__ for c in _clicks(w._handle)]}")
            check("r17_header_handle_has_no_click_gesture",
                  not _clicks(h) if h is not None else False,
                  f"{[type(c).__name__ for c in _clicks(h)] if h else 'no h'}")
            check("r17_app_move_drag_on_both_handles",
                  w._move_drags.get("video") in w._handle.observe_controllers()
                  and (h is None or w._move_drags.get("header")
                       in h.observe_controllers()),
                  f"drags={sorted(w._move_drags)}")
            check("r17_header_dblclick_owned_on_the_bar",
                  w._hdr_dblclick in w.top_bar.observe_controllers(),
                  "app-owned double-click maximise gesture")
            return again(guard(s_positive), 150)

        def s_positive():
            """POSITIVE CONTROL: dragging the video (window-handle-backed
            since always) MUST produce a move message — every zero in
            this gate is only meaningful because this one is not zero."""
            x0, y0, gw, gh = wpos()
            return drag(x0 + gw // 2, y0 + gh // 2, 40, 20, steps=8,
                        done=s_positive_check)

        def s_positive_check():
            n8, n11, _ = drain8()
            check("pos_video_drag_moves", n8 >= 1, f"n8={n8} n11={n11}")
            # R17: the move now goes THROUGH our own Python handler, so the
            # spy must see it — a zero here means the app-owned drag is not
            # the thing moving the window any more.
            check("pos_begin_move_spy_fired", S["bm_calls"] >= 1,
                  f"begin_move calls={S['bm_calls']}")
            return again(guard(a1_drag), 250)

        # ------------------------------------------------------- A1
        # R17-B/C: a header drag must be initiated by OUR code, spied at its
        # true seam (Gdk.Toplevel.begin_move).  Two measured facts shape the
        # numbers here:
        #   * the app refuses a desktop dnd threshold below
        #     window.MOVE_DRAG_MIN_PX (4 px, see tests/test_move_drag.py),
        #     so lowering the setting to 2 does NOT lower the app's
        #     threshold — an earlier draft of this stage did exactly that
        #     and the gesture never crossed, leaving the retained C drag to
        #     move the window (the stage still passed, because its
        #     assertion was not a delta: see a1_header_drag_called_begin_move);
        #   * GTK coalesces 1 px warps — a 7 px drag in 7 steps reported
        #     drag-update offsets of only 1, 2, 3 before the release.
        # So the threshold is set to the floor itself (no ambiguity about
        # what the app used) and the drag travels 24 px in 6 px steps.
        def a1_drag():
            now = set_thr(MOVE_FLOOR_PX)
            print(f"A1: threshold {S['thr0']} -> {now} "
                  f"(app floor {MOVE_FLOOR_PX}px)")
            px, py = bar_point()
            print(f"A1: dragging empty header at {px},{py}")
            S["bm_before"] = S["bm_calls"]
            S["mi_before"] = win()._move_initiated
            S["bm_args_before"] = len(S["bm_args"])
            th = win()._top_handle
            print(f"A1: top_handle can_target={th.get_can_target()} "
                  f"alloc={th.get_width()}x{th.get_height()} "
                  f"bar_target={win().top_bar.get_can_target()}")
            return drag(px, py, 24, 0, steps=4, done=a1_check)

        def a1_check():
            n8, n11, nsm = drain8()
            print(f"A1: n8={n8} n11={n11} dirs={S.get('dirs')}")
            check("a1_empty_header_moves_once", n8 == 1,
                  f"begin_move (dir-8) count {n8}, cancel {n11}")
            check("a1_no_showmenu", nsm == 0, str(nsm))
            # R17-B: the app-owned initiation, spied at its true seam, with
            # the CURRENT event's device / primary button / timestamp.  The
            # assertion is a DELTA: the positive control already called
            # begin_move once, so a bare ">= 1" would pass even if the
            # header drag never reached our handler.
            new = S["bm_args"][S["bm_args_before"]:]
            check("a1_header_drag_called_begin_move",
                  len(new) == 1 and
                  S["bm_calls"] == S["bm_before"] + 1,
                  f"calls {S['bm_before']} -> {S['bm_calls']} "
                  f"(move_init {S['mi_before']} -> "
                  f"{win()._move_initiated})")
            check("a1_move_initiated_counter_advanced",
                  win()._move_initiated == S["mi_before"] + 1,
                  f"{S['mi_before']} -> {win()._move_initiated}")
            args = new[0] if new else None
            if args is not None:
                print(f"A1: begin_move args dev={args[0]} button={args[1]} "
                      f"xy=({args[2]:.0f},{args[3]:.0f}) t={args[4]}")
                check("a1_begin_move_used_pointer_device",
                      isinstance(args[0], Gdk.Device), str(args[0]))
                check("a1_begin_move_used_primary_button", args[1] == 1,
                      str(args[1]))
                check("a1_begin_move_passed_a_time",
                      isinstance(args[4], int) and args[4] >= 0, str(args[4]))
                # Attribution: the origin must be the HEADER press (widget
                # coords near the top-left of the strip), not the video
                # centre the positive control dragged from.
                check("a1_begin_move_origin_is_the_header_press",
                      args[2] < 300.0 and args[3] < 60.0,
                      f"origin=({args[2]:.0f},{args[3]:.0f}) "
                      f"(video centre would be ~475,265)")
            now = set_thr(S["thr0"])
            print(f"A1: threshold restored to {now}")
            return again(guard(a1b_click), 250)

        # ------------------------------------------------------ A1b / A1c
        def a1b_click():
            """B.3: a click WITHOUT motion must not move the window.  The
            app-owned move waits for the threshold, so press+release stays
            a click (this is what keeps click-to-pause alive)."""
            S["bm_before"] = S["bm_calls"]
            px, py = bar_point()
            print(f"A1b: click without motion at {px},{py}")
            drain8()
            fake_button("press", px, py)
            return again(guard(lambda: (fake_button("release", px, py),
                                        again(guard(a1b_check), 450))[-1]),
                         120)

        def a1b_check():
            n8, n11, nsm = drain8()
            check("a1b_click_without_motion_no_move", n8 == 0,
                  f"n8={n8} n11={n11}")
            check("a1b_click_never_calls_begin_move",
                  S["bm_calls"] == S["bm_before"],
                  f"{S['bm_before']} -> {S['bm_calls']}")
            return again(guard(a1c_block), 200)

        def a1c_block():
            """Attribution: block the app call itself (record, do NOT call
            through).  If the app-owned call is the SOLE initiator of the
            move, the wire goes silent while it is blocked."""
            S["blocked"] = []
            live = Gdk.Toplevel.begin_move
            S["orig_bm_live"] = live

            def blocked(self, *a, **k):
                S["bm_calls"] += 1
                S["bm_args"].append(a)
                S["blocked"].append(a)
                return None
            Gdk.Toplevel.begin_move = blocked
            px, py = bar_point()
            print("A1c: header drag with begin_move BLOCKED")
            return drag(px, py, 30, 12, steps=8, done=a1c_check)

        def a1c_check():
            n8, n11, nsm = drain8()
            check("a1c_app_call_is_the_initiator",
                  len(S["blocked"]) == 1, f"blocked={len(S['blocked'])}")
            check("a1c_no_move_without_the_app_call", n8 == 0,
                  f"n8={n8} — the retained C drag must stay silent once the "
                  f"app-owned gesture claims the sequence")

            def bm(self, *a, **k):
                S["bm_calls"] += 1
                S["bm_args"].append(a)
                return S["orig_bm_live"](self, *a, **k)
            Gdk.Toplevel.begin_move = bm
            px, py = bar_point()
            print("A1c: same drag with begin_move restored")
            return drag(px, py, 30, 12, steps=8, done=a1c_check2)

        def a1c_check2():
            n8, n11, nsm = drain8()
            check("a1c_restored_moves_again", n8 == 1, f"n8={n8} n11={n11}")
            return again(guard(a2_min), 250)

        # ------------------------------------------------------- A2
        def _a2_press(btn, done):
            px, py = wp(btn)
            print(f"A2: drag-off press at {px},{py} "
                  f"threshold={S['thr0']}")
            return drag(px, py, 0, -7, steps=7, done=done)

        def a2_min():
            c0 = S["min_calls"]
            return _a2_press(win().btn_min,
                             lambda: a2_check("btn_min", "min_calls", c0))

        def a2_check(name, counter, c0):
            n8, n11, nsm = drain8()
            delta = S[counter] - c0
            check(f"a2_{name}_no_move", n8 == 0, f"n8={n8} n11={n11}")
            check(f"a2_{name}_action_once", delta == 1,
                  f"{counter} delta {delta}")
            if name == "btn_min":
                return again(guard(a2_max), 250)
            if name == "btn_max":
                return again(guard(a2_close), 250)
            return again(guard(a3_open), 250)

        def a2_max():
            c0 = S["max_calls"]
            return _a2_press(win().btn_max,
                             lambda: a2_check("btn_max", "max_calls", c0))

        def a2_close():
            c0 = S["close_calls"]
            return _a2_press(win().btn_close,
                             lambda: a2_check("btn_close", "close_calls",
                                              c0))

        # ------------------------------------------------------- A3
        def a3_open():
            n0 = len(S["open_calls"])
            px, py = bar_point()
            drain8()
            fake_button("press", px, py, button=3)
            return again(guard(lambda: a3_release(n0)), 250)

        def a3_release(n0):
            px, py = bar_point()
            fake_button("release", px, py, button=3)
            return again(guard(lambda: a3_check(n0)), 700)

        def a3_check(n0):
            w = win()
            n8, n11, nsm = drain8()
            delta = len(S["open_calls"]) - n0
            vis = w._ctx_pop.get_visible() if w._ctx_pop is not None \
                else False
            check("a3_our_menu_opened", delta == 1, f"open delta {delta}")
            check("a3_popover_visible", vis, f"popover visible={vis}")
            check("a3_right_press_no_move", n8 == 0, f"n8={n8} n11={n11}")
            check("a3_no_gtk_show_window_menu", nsm == 0, f"nsm={nsm}")
            xkey("Escape")
            return again(guard(a4_enter_fs), 500)

        # ------------------------------------------------------- A4
        def a4_enter_fs():
            w = win()
            w._act_fullscreen()
            # the app's OWN flag is the trustworthy fullscreen truth
            # here: X11 without a real WM never sends the state notify
            # back (window.py comment, P2 finding) and the fake WM's
            # geometry round trip is best-effort.  What matters for
            # this gate is the mount state the swap leaves behind, and
            # every drag coordinate is re-calibrated live anyway.
            return wait_for("a4_fullscreen_on", lambda: w._fs is True,
                            a4_fs_checks, left=60)

        def a4_fs_checks():
            w = win()
            kids = overlay_kids()
            top_handles = [c for c in kids
                           if isinstance(c, Gtk.WindowHandle)
                           and c is not w._handle]
            h = getattr(w, "_top_handle", None)
            pp = h.get_parent() if h is not None else None
            check("a4_no_handle_while_fs", not top_handles,
                  f"{len(top_handles)} top handle(s) mounted")
            check("a4_handle_detached", pp is None,
                  f"parent={'none' if pp is None else type(pp).__name__}")
            check("a4_bar_still_targetable",
                  w.top_bar.get_can_target(),
                  f"bar can_target={w.top_bar.get_can_target()}")
            return again(guard(lambda: calib(a4_fs_drag)), 250)

        def a4_fs_drag():
            set_thr(2)
            px, py = bar_point()
            print(f"A4: fullscreen header drag at {px},{py}")
            return drag(px, py, 30, 0, steps=10, done=a4_fs_check)

        def a4_fs_check():
            n8, n11, nsm = drain8()
            check("a4_fs_header_no_move", n8 == 0, f"n8={n8} n11={n11}")
            set_thr(S["thr0"])
            w = win()
            w._act_fullscreen_exit()
            return wait_for("a4_left_fullscreen", lambda: w._fs is False,
                            a4_restored, left=60)

        def a4_restored():
            w = win()
            h = getattr(w, "_top_handle", None)
            pp = h.get_parent() if h is not None else None
            check("a4_handle_remounted",
                  h is not None and h.get_parent() is w.overlay
                  and w.top_bar.get_parent() is h,
                  f"parent={'none' if pp is None else type(pp).__name__} "
                  f"bar_in_handle="
                  f"{h.get_child() is w.top_bar if h else 'no handle'}")
            return again(guard(lambda: calib(a5_hide)), 300)

        # ------------------------------------------------------- A5
        def a5_hide():
            w = win()
            # isolate: the VIDEO handle also answers begin_move BY DESIGN
            # (windowed drag-anywhere); detach it so this phase measures
            # exactly "can the hidden HEADER move the window".
            w._set_handle_active(False)
            set_thr(2)
            w.core.set_property("pause", False)        # playing: idle
            return wait_for("a5_chrome_faded", lambda: max(
                w.top_bar.get_opacity(),
                w.chrome.bottom.get_opacity()) < 0.05, a5_checks,
                left=80)

        def a5_checks():
            w = win()
            h = getattr(w, "_top_handle", None)
            check("a5_hidden_bar_not_targetable",
                  not w.top_bar.get_can_target(),
                  f"bar can_target={w.top_bar.get_can_target()}")
            check("a5_hidden_handle_not_targetable",
                  h is not None and not h.get_can_target(),
                  f"handle can_target="
                  f"{h.get_can_target() if h is not None else 'no handle'}")
            px, py = bar_point()
            print(f"A5: faded-header drag at {px},{py}")
            return drag(px, py, 25, 0, steps=10, done=a5_check)

        def a5_check():
            n8, n11, nsm = drain8()
            check("a5_hidden_header_no_move", n8 == 0,
                  f"n8={n8} n11={n11}")
            return again(guard(s_final), 200)

        def s_final():
            w = win()
            print(f"INFO: app-owned begin_move spy calls={S['bm_calls']} "
                  "(R17: the app's own drag handler is the seam; in R15 this "
                  "spy was blind to C-originated drags)")
            w.core.set_property("pause", True)
            w._set_handle_active(True)
            set_thr(S["thr0"])
            check("s_close_never_fired_by_accident",
                  S["close_calls"] == 1, f"close delta {S['close_calls']}")
            try:
                Gdk.Toplevel.begin_move = S["orig_bm"]
                w.minimize, w.close = S["orig_min"], S["orig_close"]
                w.maximize, w.unmaximize = S["orig_max"], S["orig_unmax"]
                w._open_context_menu = S["orig_open"]
            except Exception:                         # noqa: BLE001
                pass
            finish_and_quit()
            return False

        GLib.timeout_add(400, guard(s_boot))

    rc = reelsee.run(["reelsee", CLIP], on_app=schedule)
    check("run_rc", rc == 0, str(rc))
    finish()
    for d in (CFG, STATE):
        shutil.rmtree(d, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
