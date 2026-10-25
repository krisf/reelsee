# Working notes for coding agents (and humans)

This file is the short version of "what will cost you a day if you don't
know it". The design document — requirement IDs (`R-1`…`R-12`), phase plan
(`P0`…`P12`), and the appendix proofs — is [`docs/DESIGN.md`](docs/DESIGN.md).
Docstrings and test names cite that file by section (`PLAN §4.6`), so keep
the section numbers stable when you edit it.

## Non-negotiables

1. **`/usr/bin/python3` only.** The interpreter on `PATH` in many developer
   shells is a venv without `gi` (PyGObject). Anything that imports `gi`
   must run under the system interpreter, or you will "fix" a bug that is
   only an import failure.
2. **No new runtime dependencies.** Allowed: stdlib, GLib/Gio/GTK4/Adw via
   introspection, and libmpv through the vendored ctypes bindings
   (`src/mpvlib.py`). No pip, no node, no mpv *binary* at runtime.
3. **Never weaken or delete an existing gate** to make a run green. If a
   gate is wrong, fix the gate in its own commit and say why in the message.
4. **Every fix ships with a gate that fails without it.** Deterministic:
   fresh state dir per phase, generous polling instead of fixed sleeps,
   margins on pixel/timing asserts, and a negative control where a vacuous
   pass is conceivable (see `scripts/smoke_package.py`).
5. **Scripts are refused, not sanitised.** `run`, `subprocess`,
   `subprocess-kill`, `load-script`, `script-message`, `script-binding`,
   `--script*`, `--input-conf`, `--config`, `--include` are rejected at the
   option-parsing boundary (`R12`). Do not add any path by which a string
   from a filename, playlist entry, or settings field reaches
   `mpv_command_string` unfiltered.

## The libmpv facts that are not obvious

All of these were established by running code, not by reading docs; the
spike that proves the render path is kept at
[`scripts/spike_render.py`](scripts/spike_render.py) and
`scripts/spike_render_run.sh`.

* `locale.setlocale(locale.LC_NUMERIC, "C")` **before** `mpv_create()`.
  GTK sets the process locale; libmpv parses option strings with it. With a
  comma-decimal locale, numeric options silently mis-parse.
* `vo=libmpv`. `gpu-next` segfaults against this libmpv build.
* Create the **render context before** the first `loadfile`, and free it
  while the GL context is still current.
* Never draw to FBO 0. Bind using the GLArea's
  `GL_DRAW_FRAMEBUFFER_BINDING` value and pass that as
  `MPV_RENDER_PARAM_FBO`.
* In the update callback, call `mpv_render_context_update()` and honour the
  returned flags (`1` = frame dirty → queue a draw). Skipping this is how
  "video plays but only the first frame shows" happens.
* **Runtime decoder switching needs a track reattach.** `set hwdec` alone
  does not reinitialise the decoder; the working sequence is
  `set hwdec <mode>` → `set vid no` → `set vid auto`. Position survives.
* The GTK main thread **never** calls a synchronous mpv getter (`R9`).
  Every value the UI reads arrives through `observe_property` into
  `src/mpvcore.py`'s snapshot. Blocking the main thread on the mpv handle is
  the failure mode behind "UI locks up on slow sources".
* Do not `GLib.idle_add` from the mpv event thread. Use a queue drained on
  the main context.

## GTK4 facts that will bite you

* There is **no window-move API** and no
  `Gtk.Window.set_geometry_hints` / `Gdk.Geometry`. Aspect-ratio-locked
  resize is done by handling `Gdk.Surface::layout` and calling
  `set_default_size()` on the mapped window; positioning a window toplev
  requires the X11 backend (and is impossible on Wayland by protocol).
* Aspect-ratio correction fires from `Gdk.Surface::layout`, which also
  fires for **your own** resize. Without a re-entrancy guard, one
  correction per layout burst and a debounce, a drag storm ensues; the
  gate drives synthetic `(surface,w,h)` feeds precisely so this is
  testable headlessly.
* Remembered window geometry is **shared state**. Two smokes reusing one
  state directory inherit each other's rect and produce results that look
  like correctness (`R10-fix` purges `window.json` per invocation). Give
  every phase its own `REELSEE_STATE_DIR`.
* Window *placement* is not a GTK4 capability. On X11 it is a raw Xlib
  `MoveResizeWindow`; on Wayland it is impossible by protocol, so tests
  must assert size-exactness and treat the offset as best-effort.
* Chrome visibility is a state machine (`src/chrome.py::ChromeState`), not a
  timer in the widget: hidden **only** while playing; pause, stop, EOF and
  any pointer motion show it again. Drive it with a monotonic clock so
  `REELSEE_FAKE_LATENCY_MS` and slow disks cannot desync it.

## Verifying work

```
/usr/bin/python3 -m unittest discover tests        # no display needed
scripts/headless-smoke.sh <name> -- /usr/bin/python3 scripts/smoke_<name>.py
scripts/p11_battery.sh                             # the full acceptance battery
```

* Every GUI or libmpv process goes through `scripts/headless-smoke.sh`
  (Xvfb + software GL + `hwdec=no` + `LD_LIBRARY_PATH` pointed at `vendor/`
  when a vendored libmpv is present).
* Smokes print machine-checkable `SMOKE_CHECK:<name>=<value>` lines and exit
  non-zero on failure. The battery treats a missing line as a failure.
* Gate races we have actually been bitten by: reading a config value before
  the app has parsed it (poll for the parsed value with a deadline), and two
  phases sharing one state directory (give every phase its own).
* GPU demotion must never trigger on dropped frames alone: healthy FPS with
  drops is a *presentation* problem and switching to CPU decode cannot fix
  it. The watchdog requires fps degradation as well (`src/watchdog.py`).
* Screenshots: popovers and context menus cannot be captured under Xvfb
  without a compositor — they are dismissed on focus-out instantly.
  `scripts/make_screenshots.py` therefore documents why the README has no
  menu screenshot.

## Layout

Pure (unit-testable, no `gi`): `ab.py`, `aspect.py` (`fit_aspect`),
`chrome.py::ChromeState`, `config.py`, `ctxmenu.py` (model half only),
`engine.py`, `keymap.py`, `registry.py`, `reelsee_io.py`, `watchdog.py`,
`wingeom.py` (remembered rect + cascade arithmetic). `x11pos.py` is the thin
raw-Xlib placement shim — X11 only, and it must degrade quietly.
Impure: `mpvlib.py` (ctypes), `mpvcore.py` (handle + event thread),
`render.py` (GLArea), `window.py` (chrome, gestures, transport),
`settings_ui.py`, `reelsee.py` (entry point).

See also [`CONTRIBUTING.md`](CONTRIBUTING.md) for environment setup and
[`docs/DESIGN.md`](docs/DESIGN.md) for the reasoning behind each rule.
