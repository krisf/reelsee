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

6. **Every new release gets published.** The public repo
   (`github.com/krisf/reelsee`) must be kept current — a minted deb is not
   DONE while the public tree is stale. After each mint phase closes its
   gate, the operator runs `scripts/publish.sh --dry-run` (review the file
   list) then `scripts/publish.sh -m <msgfile>`: `make_public.sh` mints the
   public tree from HEAD and REFUSES on any leak-pattern match, and the
   push happens from a fresh clone under the noreply identity. DSH runs
   never push — publishing is the operator close-out. Never commit
   `scripts/leak-patterns.txt`; never name its contents in anything that
   ships. (User order 2026-11-06, mirrored in looksee PLAN §27.)

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
* **A remembered position inside the last 10 % of the runtime is NOT
  restored** (R26): `_resume_restore()` drops the entry and plays from 0 when
  `dur - pos < playback.resume_tail_frac * dur` — strictly less, so a position
  with exactly 10 % left still resumes. `duration` only exists in the async
  observed snapshot (R9: the GTK thread never calls a sync mpv getter) and
  `file-loaded` can beat it, so the check is a **bounded retry**
  (`GLib.timeout_add(150 ms, …)`, ~2 s, id kept on `self._resume_pending_id`
  and cancelled by a second load, a second restore call or window teardown);
  if the duration never arrives it falls back to seeking, so resume can never
  get worse than before. `_resume_record()`'s own `pos >= dur - 1.0` deletion,
  the 300 s `resume_min_s` floor and the 5 s tick are untouched.
  `tests/test_resume_tail_r26.py` and `scripts/smoke_resume_tail.py` gate both
  directions (the mid-file phase is the negative control).
* Window *placement* is not a GTK4 capability. On X11 it is a raw Xlib
  `MoveResizeWindow`; on Wayland it is impossible by protocol, so tests
  must assert size-exactness and treat the offset as best-effort.
* **The app pins its own GDK backend** (`src/backendpin.py`, called from
  `reelsee.run()` before `gi` is imported). Positioning only exists on X11,
  and a `.desktop` `Exec=` pin is not durable — user-level entries shadow the
  packaged one and a terminal launch carries no env at all, which shipped
  0.2.7 as an unpositioned native Wayland client. `pin_decision` pins
  `GDK_BACKEND=x11` only inside a provable XWayland session (socket on disk),
  never raises, and is overridden by `GDK_BACKEND` or `REELSEE_BACKEND_PIN`.
  `scripts/headless-smoke.sh` exports `GDK_BACKEND=x11`, so **no smoke covers
  the unpinned path** — `smoke_backendpin.py` is the gate that does, and
  `smoke_geomresume.py`'s no-xid phase is the negative control proving the
  position assertions can actually fail.
* `config_dir()` and `state_dir()` resolve to `~/.config/reelsee` and
  `~/.local/state/reelsee` and **ignore** `XDG_CONFIG_HOME` /
  `XDG_STATE_HOME`. To point the app at a scratch home, set
  `REELSEE_CONFIG_DIR` / `REELSEE_STATE_DIR` (they name the directory
  itself, with no `reelsee/` suffix). Writing config somewhere else is
  silent: the app starts on defaults and every setting you thought you
  applied is fiction — this cost a screenshot that showed the opposite
  of the feature it advertised.
* The bars are an **overlay with no layout slot** (0.2.0). If a bar is
  given a real allocation, revealing it resizes the video — visually a
  flicker, mechanically a re-init. `overlaychrome` gates this.
* Crop-to-window and aspect lock are **mutually exclusive by design**;
  read the state through `aspect_lock_active`, never by re-reading
  `ui.lock_aspect` directly.
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
- **GPU demotion on dropped frames requires a SUSTAINED streak (R19):** a
  single-sample drop spike with healthy FPS is a seek/flush artifact and must
  never demote (the R7 false-positive guard), but sustained drops-only
  overload — 5 consecutive samples (`DROP_CONFIRM`) with fps healthy — DOES
  demote (`reason="drops"`): that is the GPU-decode presentation-stall
  signature the user reported after R18 made engagement actually succeed.
  fps-starved overload keeps the 3-sample confirm path (`src/watchdog.py`).**
* **`window.json` is always PHYSICAL pixels (R25).** Two coordinate spaces
  exist on a HiDPI/XWayland desktop: raw-Xlib rects (physical) and GTK's
  `set_default_size` / `get_width` / `Gdk.Monitor` geometry (logical =
  physical ÷ `get_scale_factor()`). The invariant is that persisted rects and
  `x11pos` moves are physical, and the conversion happens at exactly two
  seams in `src/window.py`: `adopt_geometry` DIVIDES by the scale before
  `set_default_size`, and `_geom_live_rect`'s Wayland fallback MULTIPLIES
  GTK's logical size before storing it. Getting either wrong does not fail
  loudly — Mutter clamps the over-tall window to the screen and the aspect
  fit widens it, so the user sees a *maximized* window. `wingeom.py` stays
  pure and never learns about scale; the scale is read per-monitor via
  `_geom_scale_for(rect)` so a mixed-DPI rig gets the right factor.
  **Every Xvfb gate runs at scale 1, where this whole class of bug is
  invisible** — `tests/test_scale_geom_r25.py` is the only coverage, and it
  fakes `scale=2`. Do not "simplify" either seam back to a raw pass-through.
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
