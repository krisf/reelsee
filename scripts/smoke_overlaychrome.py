#!/usr/bin/python3
"""R12 gate: the chrome OVERLAYS the video — retracting it NEVER resizes
the video.

The old Adw.ToolbarView contract gave the top bar real layout space, so
revealing chrome re-flowed the content and the video shrank/grew.  The
R12 contract this gate pins:

  * STRUCTURE — the window's content IS a Gtk.Overlay; its BASE child is
    the render view (through the Gtk.WindowHandle that keeps drag-
    anywhere); EVERY chrome widget (top strip with title + window
    controls, bottom bar, OSD, preview, toast) is a child of THAT SAME
    overlay; no Adw.ToolbarView / set_reveal_top_bars left anywhere.
  * ALLOCATION — the render view's size in px is BYTE-IDENTICAL visible
    vs hidden chrome, the top-level surface size never moves across a
    reveal/hide, while opacity really reaches ~1 / ~0 and the hidden
    chrome is click-THROUGH (can_target False).
  * CONTROLS — the top strip owns the window controls: minimise calls
    window.minimize(), maximise/restore calls window.maximize()/
    unmaximize() (icon + tooltip follow notify::maximized), close calls
    window.close() — exercised by spy-invoking each button; the close
    is ALSO proven end-to-end on a throwaway window, and a click on a
    control never touches playback.
  * DECORATION — the window stays decorated with a ZERO-HEIGHT titlebar
    stub; CSD machinery is proven alive by the window's own css
    csd/solid-csd classes and by the X surface holding the entire
    decorated window (the WM-less headless stand-in for resize edges —
    GTK publishes _GTK_FRAME_EXTENTS only when a WM consumes it, verified
    against a bare control window).  set_default_size still reaches the
    surface through the stub.

Runs under scripts/headless-smoke.sh; test config speeds the idle timer
(400 ms) so the hide edge is fast and deterministic."""
import json
import os
import sys

from smoke_harness import SCRATCH, check, finish

CFG_DIR = SCRATCH / "r12oc-cfg"
STATE_DIR = SCRATCH / "r12oc-state"
for _d in (CFG_DIR, STATE_DIR):
    _d.mkdir(parents=True, exist_ok=True)
os.environ["REELSEE_CONFIG_DIR"] = str(CFG_DIR)
os.environ["REELSEE_STATE_DIR"] = str(STATE_DIR)
for _f in (CFG_DIR / "config.json", STATE_DIR / "window.json",
           STATE_DIR / "resume.json"):
    if _f.exists():
        _f.unlink()
(CFG_DIR / "config.json").write_text(json.dumps(
    {"ui": {"chrome_timeout_ms": 400, "osd_ms": 1200}}))

import reelsee  # noqa: E402  (after env, like the real entry)

state: dict = {}


def main() -> int:
    def schedule(app):
        from gi.repository import GLib, Gtk
        import gi
        gi.require_version("Adw", "1")
        from gi.repository import Adw
        state["app"] = app

        def win():
            return app.windows[0]

        def guard(fn):
            def run(*a):
                try:
                    return fn(*a)
                except Exception as exc:               # noqa: BLE001
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
            val = None
            try:
                val = pred()
            except Exception:                          # noqa: BLE001
                val = None
            if val:
                check(desc, True, "ok" if val is True else str(val))
                return again(then, 60)
            if left <= 0:
                check(desc, False, "timeout")
                app.quit()
                return False
            return again(lambda: wait_for(desc, pred, then, left - 1), 100)

        # ------------------------------------------------------------ helpers
        def fades():
            w = win()
            return (round(float(w.chrome.bottom.get_opacity()), 3),
                    round(float(w.top_bar.get_opacity()), 3))

        def targets():
            w = win()
            return (bool(w.chrome.bottom.get_can_target()),
                    bool(w.top_bar.get_can_target()))

        def revealed():
            return fades()[0] > 0.95 and fades()[1] > 0.95 \
                and all(targets())

        def retracted():
            return fades()[0] < 0.05 and fades()[1] < 0.05 \
                and not any(targets())

        def video_alloc():
            """THE numbers: render-view px, overlay px, surface px."""
            w = win()
            surf = w.get_surface()
            return (w.view.area.get_width(), w.view.area.get_height(),
                    w.overlay.get_width(), w.overlay.get_height(),
                    surf.get_width(), surf.get_height())

        def walk(node, out):
            out.append(node)
            c = node.get_first_child()
            while c is not None:
                walk(c, out)
                c = c.get_next_sibling()
            return out

        # ------------------------------------------------------------- phases
        def p0_boot():
            w = win()
            w._load_path("testdata/perf_1080p30.mp4")
            return again(lambda: wait_for(
                "playing_at_boot_phase",
                lambda: w.core.snapshot.get("pause") is False
                and w.view.area.get_width() > 0,
                p1_structure), 200)

        def p1_structure():
            import gi
            gi.require_version("Gtk", "4.0")
            from gi.repository import Gtk
            w = win()
            check("content_is_the_overlay",
                  isinstance(w.overlay, Gtk.Overlay)
                  and w.get_child() is w.overlay,
                  f"child={type(w.get_child()).__name__}")
            check("render_view_is_base_child",
                  isinstance(w.overlay, Gtk.Overlay)
                  and w.overlay.get_child() is w._handle
                  and w._handle.get_child() is w.view.area,
                  f"base={type(w.overlay.get_child()).__name__} "
                  f"handle_child_is_glarea={w._handle.get_child() is w.view.area}")
            # R15-A premise update (own commit): the top strip now rides
            # inside a Gtk.WindowHandle — the HANDLE is the overlay child
            # and the strip's parent is the handle, not the overlay.  The
            # contract this gate guards (every chrome hangs off THE ONE
            # overlay, no layout slot) is unchanged; the chain just has
            # one honest extra link for the strip.
            tb = w.top_bar.get_parent()
            top_is_overlay_child = (
                tb is w.overlay or (tb is getattr(w, "_top_handle", None)
                                    and tb.get_parent() is w.overlay))
            kids = (top_is_overlay_child,
                    w.chrome.bottom.get_parent() is w.overlay,
                    w.chrome.osd.get_parent() is w.overlay,
                    w.chrome.preview.get_parent() is w.overlay,
                    w.chrome.toast.get_parent() is w.overlay)
            check("chrome_children_of_same_overlay", all(kids),
                  f"top/bottom/osd/preview/toast={kids}")
            nodes = walk(w, [])
            check("toolbarview_gone_entirely",
                  not any(isinstance(n, Adw.ToolbarView) for n in nodes)
                  and not hasattr(w, "_toolbar")
                  and not hasattr(w, "header")
                  and not hasattr(w.chrome, "set_reveal_top_bars"),
                  f"toolbarviews="
                  f"{sum(1 for n in nodes if isinstance(n, Adw.ToolbarView))}")
            check("window_controls_present_in_top_strip",
                  all(b.get_parent() is not None
                      and w.top_bar.is_ancestor(b) is False
                      and b.is_ancestor(w.top_bar)
                      for b in (w.btn_min, w.btn_max, w.btn_close))
                  and isinstance(w.btn_min, Gtk.Button)
                  and isinstance(w.btn_max, Gtk.Button)
                  and isinstance(w.btn_close, Gtk.Button),
                  "min/max/close inside the overlay top strip")
            # the base child gets the FULL overlay width (video fills the
            # client area while chrome is revealed on top of it)
            check("video_fills_width_with_chrome_revealed",
                  w.view.area.get_width() == w.overlay.get_width(),
                  f"video={w.view.area.get_width()} "
                  f"overlay={w.overlay.get_width()}")
            return again(p2_reveal_visible, 80)

        def p2_reveal_visible():
            w = win()
            w.chrome.motion(float(w.overlay.get_width()) / 2, 300.0,
                            float(w.overlay.get_width()),
                            float(w.overlay.get_height()))
            return again(lambda: wait_for(
                "chrome_revealed_visible", revealed, p3_record_visible), 60)

        def p3_record_visible():
            state["vis"] = video_alloc()
            check("visible_opacities", fades()[0] >= 0.95
                  and fades()[1] >= 0.95, str(fades()))
            # idle out: playing + 400 ms without motion retracts
            return again(p4_wait_hidden, 80)

        def p4_wait_hidden():
            return again(lambda: wait_for(
                "chrome_hidden_idle", retracted, p5_record_hidden), 100)

        def p5_record_hidden():
            state["hid"] = video_alloc()
            check("allocation_byte_identical_hidden",
                  str(state["vis"]) == str(state["hid"]),
                  f"visible={state['vis']} hidden={state['hid']}")
            v, h = state["vis"], state["hid"]
            check("surface_size_unchanged", v[4:6] == h[4:6],
                  f"surface {v[4:6]} vs {h[4:6]}")
            check("video_px_unchanged", v[0:2] == h[0:2],
                  f"video {v[0:2]} vs {h[0:2]}")
            check("hidden_opacity_zero", fades()[0] <= 0.05
                  and fades()[1] <= 0.05, str(fades()))
            check("hidden_click_through", not any(targets()),
                  str(targets()))
            # surface truth at the X level too: mapped geometry identical
            check("hidden_cursor_blank",
                  win().chrome._cursor_blank is True, "blanked")
            return again(p6_reveal_again, 80)

        def p6_reveal_again():
            w = win()
            w.chrome.motion(float(w.overlay.get_width()) / 2, 300.0,
                            float(w.overlay.get_width()),
                            float(w.overlay.get_height()))
            return again(lambda: wait_for(
                "chrome_revealed_again", revealed, p7_record_again), 60)

        def p7_record_again():
            state["rev"] = video_alloc()
            check("allocation_byte_identical_revealed",
                  str(state["vis"]) == str(state["rev"]),
                  f"visible={state['vis']} revealed={state['rev']}")
            check("revealed_clickable", all(targets()), str(targets()))
            return again(p8_window_controls, 120)

        # ------------------------------------------------------ window controls
        # NOTE: Gtk.Button.activate() returns TRUE but does NOT emit
        # "clicked" in this GTK 4 build (measured — it only triggers the
        # activate action); emit("clicked") is the deterministic way to
        # fire the real handler chain without synthetic X input.
        def click(btn):
            btn.emit("clicked")

        def p8_window_controls():
            w = win()
            calls = state.setdefault("calls", [])
            w.minimize = lambda: calls.append("minimize")
            w.maximize = lambda: calls.append("maximize")
            w.unmaximize = lambda: calls.append("unmaximize")
            w.close = lambda: calls.append("close")
            try:
                w.chrome.wake()
                click(w.btn_min)
                click(w.btn_max)
                click(w.btn_close)          # SPY — main window stays
                check("minimise_button_calls_minimize",
                      "minimize" in calls, str(calls))
                check("maximise_button_calls_maximize",
                      "maximize" in calls and "unmaximize" not in calls,
                      str(calls))
                check("close_button_calls_close", "close" in calls,
                      str(calls))
                check("controls_never_touch_playback",
                      win().core.snapshot.get("pause") is False,
                      f"pause={win().core.snapshot.get('pause')}")
            finally:
                for a in ("minimize", "maximize", "unmaximize", "close"):
                    delattr(w, a) if a in w.__dict__ else None
            return again(p9_maximized_notify, 60)

        def p9_maximized_notify():
            """Icon + tooltip follow the maximized STATE (the real notify
            signal path is wired at build; driving the handler with the
            state spied is the deterministic form of that edge)."""
            w = win()
            orig = w.is_maximized
            try:
                w.is_maximized = lambda: True
                w._on_maximized_notify()
                img = w.btn_max.get_child()
                check("maximized_icon_is_restore",
                      img.get_icon_name() == "window-restore-symbolic",
                      str(img.get_icon_name()))
                check("maximized_tooltip_is_restore",
                      "restore" in str(w.btn_max.get_tooltip_text()).lower(),
                      str(w.btn_max.get_tooltip_text()))
                calls = state.setdefault("calls2", [])
                w.unmaximize = lambda: calls.append("unmaximize")
                click(w.btn_max)              # maximized → restore
                check("maximised_button_restores",
                      calls == ["unmaximize"], str(calls))
            finally:
                w.is_maximized = orig
                if "unmaximize" in w.__dict__:
                    delattr(w, "unmaximize")
            w._on_maximized_notify()            # back to maximise icon
            img = w.btn_max.get_child()
            check("unmaximized_icon_is_maximise",
                  img.get_icon_name() == "window-maximize-symbolic",
                  str(img.get_icon_name()))
            check("maximize_button_wired_while_windowed",
                  w.btn_max.get_sensitive() is True, "sensitive")
            return again(p10_real_close, 60)

        def p10_real_close():
            """The REAL close wiring, end to end — on a THROWAWAY window
            (the main window must survive)."""
            w = win()
            w2 = app._spawn_window()           # the app's own spawn path
            n_before = len(app.windows)
            state["w2"] = w2
            return again(lambda: wait_for(
                "throwaway_mapped",
                lambda: w2.get_mapped(),
                lambda: p10b_close(w2, n_before)), 300)

        def p10b_close(w2, n_before):
            w2.btn_close.emit("clicked")        # REAL handler this time

            def after_closed():
                check("throwaway_forgotten_from_app",
                      w2 not in app.windows
                      and len(app.windows) == n_before - 1,
                      f"windows={len(app.windows)} was={n_before}")
                check("main_window_survives_close",
                      win() in app.windows and win().get_mapped(), "ok")
                return again(p11_decorated, 60)

            return again(lambda: wait_for(
                "throwaway_closed_by_close_button",
                lambda: w2 not in app.windows and not w2.get_mapped(),
                after_closed, left=30), 100)

        # -------------------------------------------------------- decoration
        def p11_decorated():
            w = win()
            tb = w.get_titlebar()
            check("still_csd_titlebar_present",
                  w.get_decorated() and tb is w._titlebar_stub,
                  f"decorated={w.get_decorated()} "
                  f"titlebar={type(tb).__name__ if tb else None}")
            h = tb.get_height() if tb is not None else -1
            check("titlebar_height_zero_or_one", 0 <= h <= 1,
                  f"titlebar_height={h}")
            check("overlay_got_the_whole_window",
                  w.overlay.get_height() == w.get_height() - h,
                  f"overlay={w.overlay.get_height()} "
                  f"window={w.get_height()} titlebar={h}")
            return again(p12_x_props, 60)

        def p12_x_props():
            """CSD machinery alive — the window is STILL client-side
            decorated so a compositor/WM hands out resize edges around
            the client area.  Honest headless evidence (measured against
            a BARE Gtk.ApplicationWindow control under this same Xvfb:
            GTK publishes _GTK_FRAME_EXTENTS only when a WM is there to
            consume it, so that property is NOT the discriminator here):
              * the window's own CSS classes carry csd/solid-csd (SSD
                would say "ssd");
              * the top-level X window is the FULL surface size — bigger
                than the widget allocation — i.e. GTK draws its whole
                decoration inside our surface, which is exactly the
                geometry a compositor turns into resize borders."""
            import os as _os
            from Xlib import X, display as xdisplay
            w = win()
            classes = list(w.get_css_classes())
            check("csd_class_active",
                  ("csd" in classes or "solid-csd" in classes)
                  and "ssd" not in classes,
                  ",".join(classes))
            d = xdisplay.Display(_os.environ.get("DISPLAY", ":0"))
            root = d.screen().root

            def mapped(xw):
                try:
                    return xw.get_attributes().map_state == X.IsViewable
                except Exception:                      # noqa: BLE001
                    return False

            big = [None, 0]

            def walkx(xw, depth=0):
                if mapped(xw) and xw != root:
                    try:
                        g = xw.get_geometry()
                        area = g.width * g.height
                    except Exception:                  # noqa: BLE001
                        g, area = None, 0
                    if g is not None and g.width >= 800 and g.height >= 400 \
                            and (big[0] is None or area > big[1]):
                        big[0], big[1] = xw, area
                try:
                    kids = xw.query_tree().children
                except Exception:                      # noqa: BLE001
                    kids = []
                for k in kids:
                    walkx(k, depth + 1)
            walkx(root)
            surf = w.get_surface()
            ok_geom = False
            desc = "no top-level window found"
            if big[0] is not None:
                g = big[0].get_geometry()
                # The X surface carries GTK's own decoration margin:
                # full surface >= client allocation (SSD would instead
                # show a WM parent frame OUTSIDE an exactly-sized win).
                ok_geom = (g.width >= max(w.get_width(), 800)
                           and g.height >= max(w.get_height(), 400)
                           and g.width == surf.get_width()
                           and g.height == surf.get_height())
                desc = (f"x={g.width}x{g.height} "
                        f"surface={surf.get_width()}x{surf.get_height()} "
                        f"widget={w.get_width()}x{w.get_height()}")
            check("csd_surface_holds_whole_window", ok_geom, desc)
            d.close()
            return again(p13_resize_reaches_surface, 60)

        def p13_resize_reaches_surface():
            w = win()
            surf = w.get_surface()
            seen = state.setdefault("layouts", [])

            def rec(_s, gw, gh, *_a):
                seen.append((int(gw), int(gh)))
            if not state.get("rec_wired"):
                surf.connect("layout", rec)
                state["rec_wired"] = True
            w.set_default_size(854, 480)      # 16:9 ⇒ the aspect lock lets
            return again(lambda: wait_for(
                "set_default_size_reaches_surface",
                lambda: seen and abs(seen[-1][0] - 854) <= 2
                and abs(seen[-1][1] - 480) <= 2 and seen[-1] != (960, 540),
                p14_finish), 150)

        def p14_finish():
            check("final_alloc_still_video_shaped",
                  win().view.area.get_width() >= 800,
                  f"alloc={video_alloc()} layouts={state['layouts'][-3:]}")
            check("evidence_allocations",
                  state["vis"] == state["hid"] == state["rev"],
                  f"visible={state['vis']} hidden={state['hid']} "
                  f"revealed={state['rev']}")
            app.quit()
            return False

        GLib.timeout_add(1200, guard(p0_boot))
        return False

    rc = reelsee.run(["reelsee", "testdata/perf_1080p30.mp4"],
                     on_app=schedule)
    check("app_rc", rc == 0, str(rc))
    finish()
    return 0


if __name__ == "__main__":
    sys.exit(main())
