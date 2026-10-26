# Architecture

One Python process, one `mpv_handle` per window, no subprocesses, no mpv
binary, no scripting. libmpv is called directly through ctypes
(`src/mpvlib.py`, generated against the vendored `mpv/*.h` for libmpv 0.41.0 /
client API 2.5 — a unit test compiles a C probe and compares
`sizeof`/`offsetof` against the bindings, so a header drift fails loudly).

```
                    ┌───────────────────────────┐
  CLI / .desktop ──▶│ reelsee.py   entry, Argv  │
                    └──────┬────────────────────┘
                           │ one per window
             ┌─────────────▼──────────────┐
             │ window.py  GTK4 chrome,    │   pure state: chrome.py
             │ gestures, transport, menus │◀── ctxmenu.py (Gio.Menu model)
             └───────┬──────────────┬─────┘   keymap.py (key → action)
        observed     │              │ GL draw
        properties   │              ▼
             ┌───────▼──────┐  ┌────────────┐
             │ mpvcore.py   │  │ render.py  │  GtkGLArea + render context
             │ event thread │  └────────────┘  (FBO from the GLArea, not 0)
             │ + Snapshot   │
             └───────┬──────┘
                     │ command string / property set
             ┌───────▼──────┐   ┌──────────────────────────────┐
             │ libmpv 0.41  │   │ watchdog.py  overload → CPU  │
             └──────────────┘   │ engine.py    GPU + hwdec pick│
                                └──────────────────────────────┘
   pure helpers: ab.py  config.py  registry.py  reelsee_io.py
```

## The two rules everything else follows

**1. The main thread never blocks on mpv (`R9`).**
`mpvcore.py` owns the `mpv_handle` and an event thread. UI inputs are
`observe_property` notifications collected into a `Snapshot`; widgets read
that snapshot. There is no synchronous `get_property` on the GTK thread, so
a stalled demuxer cannot stall the window — this is what makes `R-1`
("UI doesn't lock on slow filesystem reads") provable rather than hopeful.

**2. The same discipline applies to the filesystem (`R-1`).**
`reelsee_io.py` touches no synchronous FS call: enumeration goes through
Gio's async APIs with cancellables, natural sort runs off-thread, and a
folder monitor appends new files (250 ms debounce).
`tests/test_no_sync_fs.py` greps the module for forbidden calls, so a future
`os.listdir` there breaks CI instead of shipping a frozen UI.

## Decode and GPU selection (`engine.py`, `watchdog.py`)

* **Enumeration** builds a GPU list from glvnd EGL vendor ICD JSONs plus a
  sysfs DRM scan (bus ids, render nodes), optionally augmented with Vulkan
  device names. Each entry maps to a `DRI_PRIME`/render-node override.
* **Selection**: `engine.decoder` pins a GPU, or `auto` round-robins new
  windows across distinct GPUs (display-attached first) so simultaneous
  windows use different pipelines (`R-6`). Last-good engine per GPU is
  remembered in `engines.json` so a known-bad combo is not retried forever.
* **hwdec chains**: discrete GPUs try `nvdec-copy → hwflash-copy →
  vaapi-copy → vdpau-copy`; integrated tries `vaapi → vdpau`. Copy-back is
  deliberate — it keeps the software scaler in control, which the embedded
  render path requires. CPU decode is a first-class choice (`hwdec=no`), not
  a fallback only (`R-8`).
* **Engagement check**: *requested* is not *actual*. After load, the
  observed `hwdec-current` is compared with the request; a silent fall-back
  to software is recorded and drives the chain forward. This is the bug class
  where "I selected the iGPU" still meant CPU decode.
* **Overload watchdog (`R-7`)**: a rolling 5 s window of dropped-frame
  deltas against the render tick demotes `hwdec` to `no` with an OSD toast —
  at most once per file, with a cooldown. Demotion requires *fps
  degradation*; dropped frames at parity fps are a presentation problem that
  CPU decode cannot cure (that false positive shipped once and is now gated).
* Mid-file switching uses `set hwdec` → `set vid no` → `set vid auto`;
  `set hwdec` alone does not reinit the decoder.

## Chrome (`chrome.py`, `window.py`)

Since 0.2.0 the bars are a **zero-space overlay**: the video surface always
fills the client area and the bars float above it, so a reveal can never
resize or re-letterbox the picture. Any change that gives a bar a layout slot
is a regression, and the `overlaychrome` gate is in the battery for that
reason. The idle countdown also guards against tick gaps (a suspend/resume
must not resurrect hidden chrome) — hidden means hidden until pointer motion.


`ChromeState` is a pure state machine (`R-3`, Looksee parity): hidden only
while playing; pause, stop and EOF force it visible; pointer motion reveals;
a monotonic clock drives the idle timeout so injected IO latency cannot
desync it. The same provider installs one scoped CSS class for the
OLED-black monochrome theme. Top bar, bottom transport bar and the window
controls fade as one unit; the video surface is a `GtkWindowHandle`, so the
window can be dragged from the picture itself.

## Window geometry

Two independent mechanisms, both pure-tested before they touch a widget:

* **Aspect lock** — the resize keeps the video's own aspect ratio, landscape
  and portrait alike. GTK4 has no `set_geometry_hints`, so the correction is
  computed by `aspect.fit_aspect()` from the observed `dwidth`/`dheight` and
  applied on `Gdk.Surface::layout` via `set_default_size()` on the mapped
  window. A re-entrancy guard plus one correction per layout burst and an
  ~80 ms debounce keep a fast drag from storming; fullscreen, maximized and
  unknown-aspect states leave resizing completely free. A newly loaded video
  fits its own aspect inside 80 % of the work area.
* **Crop to window** — a persistent boolean (`video.crop_to_window`) that
  rides mpv's `panscan=1.0`: the picture covers the window and the overflow is
  clipped. While it is on, the aspect lock is suspended through a single
  decision point (`aspect_lock_active`), because "keep the video's aspect" and
  "fill and clip" are contradictory instructions; turning crop off re-arms the
  lock.
* **Remembered geometry and cascading** — the last windowed rect is stored
  (debounced 250 ms, flushed on close/quit, never a fullscreen rect) and a new
  window opens at *exactly* that size, offset ±50 px with the direction chosen
  by available space. GTK4 cannot move a window, so placement goes through raw
  Xlib; the packaged launcher pins `GDK_BACKEND=x11` (environment-overridable)
  and native Wayland degrades to size-only cascading, which is the honest
  ceiling of `xdg-shell`.

## Persistence

| Path | Contents | Discipline |
|---|---|---|
| `~/.config/reelsee/config.json` | all settings | atomic (`tmp` + `os.replace`), 250 ms debounce, unknown keys preserved, corrupt file → defaults (`R-4`: mute/volume write immediately) |
| `~/.local/state/reelsee/resume.json` | position per path | cap 500, LRU; flushed on graceful shutdown, not only on a 5 s tick |
| `~/.local/state/reelsee/ab.json` | A/B points per path | cap 500 (`R-5`) |
| `~/.local/state/reelsee/engines.json` | last-good engine per GPU | pin memory |

`REELSEE_CONFIG_DIR` / `REELSEE_STATE_DIR` redirect both for tests;
`REELSEE_FAKE_LATENCY_MS` injects IO latency into `reelsee_io` so the async
guarantees can be tested on a fast machine.

## Verification pyramid

1. **Unit suite** (`tests/`, no display): config tolerance, state machines,
   natural sort, engine selection, watchdog arithmetic, binding layouts.
2. **Smokes** (`scripts/smoke_*.py` under `scripts/headless-smoke.sh`): real
   libmpv under Xvfb + software GL, printing `SMOKE_CHECK:name=value`.
3. **Battery** (`scripts/p11_battery.sh`): the acceptance set, run twice
   consecutively; a missing `SMOKE_CHECK` line counts as failure, and gates
   carry negative controls so they cannot pass vacuously.
