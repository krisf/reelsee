# REELSEE — design document (as built)

> Written before the first line of code; kept because docstrings, test
> names and gates cite its section numbers (`PLAN §4.6`, `R-5`, `P0`…`P12`).

# PLAN.md — REELSEE: greenfield GTK4 video player (libmpv frontend, Looksee's sister app)
Dispatch model: whole-build single-run via DeepSeek Harness (dsh alias `qwen3.8-27b` @ 127.0.0.1:8180).
Executor: DSH. Operator (Hermes/user) owns P0 only (system packages + the render-API spike).

## 0. P0 OUTCOME — AUTHORITATIVE ADDENDUM (2026-10-24, post-verification)

**Status: `REELSEE_P0_OPERATOR_OK`.** All proofs PASS; full evidence in
`references/P0-RECON.md` + `references/P0-SPIKE.md`; working reference implementation in
`scripts/spike_render.py` (+ `spike_render_run.sh`, `spike_hwdec_switch.sh`,
`testdata/gen_clips.sh`). **This section overrides any conflicting statement below.**

No sudo was used and nothing was apt-installed system-wide. libmpv is **vendored**
into the repo (details §2). The following rules were learned by experiment — violating
any of them = segfault, dead playback, or an invisible frame:

1. **LC_NUMERIC=C at the creation site.** `import gi` resets LC_NUMERIC to the env
   locale and libmpv aborts `mpv_create` ("Non-C locale detected"). Re-set
   `locale.setlocale(locale.LC_NUMERIC, "C")` immediately before `mpv_create` and before
   render-context creation.
2. **Init options MUST include `vo=libmpv` and `gpu-api=opengl`.** Without `vo=libmpv`,
   `loadfile` makes mpv auto-create its own VO (extra window, RADV probe storm,
   segfault under llvmpipe). `vo=gpu-next` + render context SEGFAULTS (PROOF-E).
3. **Render context BEFORE `loadfile`.** Loading earlier kills the VO: playback frozen,
   update callback never fires. Per window: handle → init → GLArea realize → render
   context → only then `loadfile` (queue the path if not realized yet).
4. **Render into the CURRENTLY-BOUND FBO, not FBO 0.** GTK4's GLArea composites its own
   offscreen framebuffer. Query `GL_DRAW_FRAMEBUFFER_BINDING` (0x8CA6) inside the render
   callback and pass that id in `MPV_RENDER_PARAM_OPENGL_FBO` + `FLIP_Y=1`. FBO 0 =
   white window with zero video pixels (PROOF-B, 1 unique color vs 57,600 after fix).
5. **Update flow:** the update callback (mpv thread) may ONLY set a `threading.Event`;
   a 10 ms main-thread poll calls `mpv_render_context_update()` when the flag is set and
   queues a repaint iff `flags & MPV_RENDER_UPDATE_FRAME` (== 1). The `…update()` call
   after each callback is REQUIRED — skipping it stalls after one frame. Never call
   GLib from the callback thread.
6. **Teardown order:** (a) anything needing live playback (probes/captures); (b) detach
   update callback + `mpv_render_context_free` while the GL context is current
   (`area.make_current()`); (c) only then `quit()` / `mpv_terminate_destroy`. Freeing
   earlier kills the VO; letting mpv free the context after GL is gone aborts (R11).
7. **Runtime engine switch mechanism (PROOF-A/DEMOTE):** `set hwdec <mode>` alone does
   NOT re-init the decoder. The proven apply is: `set hwdec <mode>` → `set vid no` →
   `set vid auto`. Works in BOTH directions mid-playback, position preserved,
   `hwdec-current` reflects the change within ~1.5 s.
8. **Engagement facts:** `vaapi-copy` engages under plain GL/EGL; zero-copy `vaapi`
   engages with `gpu-api=vulkan` (user's daily config) but NOT under plain `vo=gpu`
   GL on XWayland. After each file-load the app MUST verify engagement via observed
   `hwdec-current` and self-fallback `vaapi → vaapi-copy → no` (§5.3). Zero-copy vaapi
   inside the embedded EGL/Wayland path is an OPERATOR acceptance item (§9.1).
9. **GLArea sizing:** `set_hexpand(True)` + `set_vexpand(True)` or the area sizes 0×0.
10. **Headless pixel gates:** under WM-less Xvfb the window maps at 0,0 — crop the xwd
    (`-crop 640x360+0+0`) before measuring. Passing values today: mean_luma≈127,
    unique_colors≈57,600, Δt≈0.667 s, vf_fps=30, rc=0.

## 1. Mission

The user asked (verbatim requirements, numbered; each one is an acceptance item in §9):

> **R-1** UI doesn't lock on slow filesystem reads
> **R-2** heavily hardware accelerated and easily pinnable to a gpu in settings
> **R-3** same black/white ui that retracts away when no mouse activity
> **R-4** audio mute persists across multiple starts
> **R-5** A B small section of video filter to repeat a short section of a video
> **R-6** very performant when multiple videos playing simultaneously on the same system
> **R-7** detects selected GPU (i will pin to IGPU most of the time) overload (low fps
>         playback) and auto swaps to CPU decode to attempt to improve
> **R-8** CPU decode is also selectable in the GPU selector instead of gpu decode

Plus two standing asks:

> **R-9** feature parity with mpv's featureset
> **R-10** looks and acts like a sister app to Looksee (the image viewer in
>          the sibling image viewer (Looksee), GTK4/libadwaita, `.deb`, `pro.krisforbes.*`)

Deliverable: a working `.deb` built in this repo, installed by the OPERATOR (user runs
the `sudo`), set as the default *video* handler only after the §9 matrix passes. The
user's existing `~/.local/bin/mpv` wrapper + `~/bin/mpv.AppImage` stay untouched and
remain the fallback for the whole build.

Name: **Reelsee** (app id `pro.krisforbes.Reelsee`, package `reelsee`, binary `reelsee`,
repo `~/projects/reelsee`). A rename is a cheap textual change — do not spend a phase on it.

### 1.1 What "meets mpv's featureset" means concretely

We do **not** re-implement a video player. We embed **libmpv** — the same engine the mpv
CLI uses — through its **client API + render API**, and we surface its feature surface in
three tiers (§7): real UI controls (T1), a searchable advanced option/command panel that
reaches the whole libmpv option namespace (T2), and a small, explicitly-documented
out-of-scope list (T3: scripting, VapourSynth, disc navigation, other OS backends).
Measured on this host (mpv 0.41.0): **1,245 distinct options, 978 properties, 85 input
commands**. Because the engine is libmpv, those exist at runtime by construction; the plan's
job is to expose the everyday 80% as UI and keep the rest reachable and honest.

### 1.2 Looksee parity (what "sister app" means, item by item)

| Looksee behaviour | Reelsee equivalent |
|---|---|
| GLib async folder scan, async decode, `tests/test_no_sync_fs.py` static lint | Same discipline + a second lint: no synchronous mpv getter on the GTK main loop |
| OLED `#000000` window/headerbar via ONE scoped `Gtk.CssProvider` class (`looksee-black`) | `reelsee-black` class, same provider-install-once-per-display pattern, strictly monochrome |
| Chrome retracts after pointer idle while playing, any pointer activity reveals, keyboard never re-arms | Same state machine (3.0 s idle), plus cursor hide and a transient OSD for keyboard-only actions |
| `~/.config/looksee/config.json`, corrupt file → per-key defaults, never crash | `~/.config/reelsee/config.json`, same tolerance + atomic writes |
| Look-ahead decoded-image cache (3+1 window, 200 MB cap) | mpv demuxer read-ahead (tuned cache) + async probe of the next playlist entry; optional paused preload window (off by default) |
| Natural-numeric folder sort, `Gio.FileMonitor` refresh | Same scanner logic reused for the video playlist |
| Long filename ellipsizes instead of clamping window min width | Same (title label ellipsize, no min-width from text) |
| `.deb` + `.desktop` + MIME list + `packaging/build-deb.sh` | Same, with video MIME list |
| `scripts/headless-smoke.sh` xvfb recipe, phase gates | Same runner, adapted flags (§2, R1) |
| Zero pip, `/usr/bin/python3` only, stdlib + PyGObject | Same, plus vendored ctypes libmpv bindings (no `python-mpv`) |

## 2. Environment facts (operator-verified 2026-10-24 — do NOT rediscover)

Everything below was measured on this box in the planning session. Evidence is in
`references/P0-RECON.md` (operator writes it in P0; content restated here).

**Host / session**
- Kernel `7.0.0-38-generic`, 32 CPUs (Ryzen 9 9950X), 121 GiB RAM.
- Session `wayland` (`WAYLAND_DISPLAY=wayland-0`, `DISPLAY=:0` = Xwayland). The user's
  desktop is the **iGPU** HDMI output.
- `/usr/bin/python3` = Python **3.14.4** with `gi` → GTK **4.22.4**, libadwaita **1.9**.
  `python3` earlier on `PATH` is a venv **without** `gi`: every GUI invocation in this repo
  MUST use `/usr/bin/python3` (looksee hard-won lesson).
- Audio: PipeWire 1.6.2 speaking PulseAudio (server "PulseAudio (on PipeWire 1.6.2)"),
  default sink `alsa_output.pci-0000_13_00.1.hdmi-stereo` (iGPU HDMI).

**GPUs (all `amdgpu`, Mesa 26.0.8)**
| Role | PCI | Render node | RADV / GL name |
|---|---|---|---|
| iGPU (display) | `0000:13:00.0` (`1002:13c0`) | `/dev/dri/renderD130` | `AMD Ryzen 9 9950X 16-Core Processor (RADV RAPHAEL_MENDOCINO)` / `radeonsi, raphael_mendocino` |
| dGPU #1 | `0000:03:00.0` (`1002:7551`) | `/dev/dri/renderD128` | `AMD Radeon AI PRO R9700 (RADV GFX1201)` / `radeonsi, gfx1201` |
| dGPU #2 | `0000:07:00.0` (`1002:7551`) | `/dev/dri/renderD129` | same as dGPU #1 |
- Render-node **numbers are not stable**; PCI slot names are. Always resolve
  `/dev/dri/by-path/pci-<slot>-render` → node. Never hardcode `renderD12x`.
- VAAPI hardware decode **verified working on both** the iGPU and dGPU #1 with
  `ffmpeg -hwaccel vaapi -hwaccel_device <node> -hwaccel_output_format vaapi -f null -`
  (exit 0 on `renderD130` and `renderD128`).

**GTK4 GPU selection (contradicts an older note — this is measured)**
- `Gtk.GLArea` on this stack gives **OpenGL ES 3.2** (Mesa 26.0.8) — not desktop GL.
- `GTK_GL_DEVICE=<pci>` is a **NO-OP** here: with `GTK_GL_DEVICE=0000:03:00.0` and
  `0000:07:00.0` the live renderer still reported `raphael_mendocino` and only
  `renderD130` was mmapped. Do not build anything on it.
- `DRI_PRIME=2` **DID** move the GL context to a R9700 (`gfx1201`, mmaps `renderD128` +
  `renderD130`) on an X11 display. Therefore: renderer override = `DRI_PRIME=<n>` exported
  **before `Gtk.init`**, effective on X11/XWayland only, and its success must be proven by
  reading `glGetString(GL_RENDERER)` in-process — never by assuming the env var worked.
- On native Wayland the compositor's GPU wins (dmabuf constraint): presentation stays on
  the display GPU. Reelsee therefore separates **presentation GPU** (follows the display,
  shown read-only in settings) from **decode GPU** (freely selectable, R-2/R-8).

**mpv / libmpv**
- mpv CLI the user runs: `~/.local/bin/mpv` = a **shell wrapper** (iGPU pin via
  `DRI_PRIME=2` + `MESA_VK_DEVICE_SELECT`, forces X11/XWayland because the AppImage lacks
  libdecor) exec'ing `~/bin/mpv.AppImage`; config `~/.config/mpv/mpv.conf` carries
  `mute=yes`, `loop-file=inf`, `gpu-api=vulkan`, `vulkan-device=AMD Ryzen 9 9950X …
  (RADV RAPHAEL_MENDOCINO)`, `hwdec=auto-safe`, `hwdec-codecs=all`,
  `vaapi-device=/dev/dri/renderD130`. **This is the incumbent tool Reelsee replaces for
  the user's own use — read-only, never modify these files.**
- `libmpv` is **vendored into this repo** (P0, no sudo used):
  `vendor/usr/lib/x86_64-linux-gnu/libmpv.so.2` (0.41.0-2ubuntu4, matches the AppImage)
  plus six vendored runtime deps (`libmujs3 liblua5.2-0 libsndio7.0 libsixel1 libxpresent1
  libva-wayland2`). **Every GUI process and every test that touches libmpv runs with
  `LD_LIBRARY_PATH=<repo>/vendor/usr/lib/x86_64-linux-gnu`** — loading by full path alone
  does NOT resolve the vendored deps. Client API version: **2.5**. Headers (ground truth
  for all ctypes structs/enums): `vendor/usr/include/mpv/{client.h,render.h,render_gl.h}`.
  The `.deb` still declares `Depends: libmpv2` so the user's install pulls the system
  package; the vendored copy serves dev/tests. `/usr/share/mpv/` (ytdl_hook.lua etc.) still
  comes from the system `mpv` package at user-install time — absent now, so URL playback
  tests must not depend on ytdl hooks.
- `--vo=help` includes `libmpv` ("render API for libmpv") → embedding path confirmed.
- `hwdec` values include `vaapi`, `vaapi-copy`, `vulkan`, `vulkan-copy` and per-codec
  aliases (h264/hevc/av1/vp9/…). `--vaapi-device`, `--vulkan-device` (choice list of full
  RADV device names, e.g. `AMD Ryzen 9 9950X 16-Core Processor (RADV RAPHAEL_MENDOCINO)`),
  `--vd-lavc-threads`, `--framedrop`, `--video-sync`, `--interpolation`,
  `--audio-exclusive`, all `--sub-*` and all `--screenshot-*` options exist.
- `ab-loop-a` / `ab-loop-b` / `ab-loop-count` exist as options **and** runtime-settable
  properties. Operator test: with A=1.0, B=2.0 set at runtime, a `seek 1.4 absolute`
  advanced to `time-pos 1.866667` and stayed inside the window → engine-side A/B looping
  works; Reelsee only has to drive and draw it.
- **RESOLVED (P0 PROOF-A + DEMOTE):** `set hwdec <mode>` alone does NOT re-init the
  decoder; the video chain must be toggled (`set vid no` → `set vid auto`). Proven in both
  directions mid-playback on the live display, position preserved (see §0.7 and
  `references/P0-SPIKE.md`). This is the watchdog's apply mechanism — no seek, no reload.
  Engagement: `vaapi-copy` engages under plain GL; zero-copy `vaapi` needs `gpu-api=vulkan`
  under XWayland (§0.8).

**Tooling present**
`ffmpeg 8.0.1` / `ffprobe`, `gcc`, `vulkaninfo`, `Xvfb`, `xvfb-run`, `xwd`, `xwininfo`,
ImageMagick (`convert`, `magick`), `yt-dlp` (`~/.local/bin/yt-dlp`). **Absent:** `vainfo`,
`glxinfo`. There is no `/usr/share/mpv/` until P0 installs apt `mpv`.
Headless GUI recipe inherited from Looksee (proven there): `xvfb-run -a -s "-screen 0
1600x1000x24"` with `GSK_RENDERER=cairo LIBGL_ALWAYS_SOFTWARE=1 GDK_BACKEND=x11
GSETTINGS_BACKEND=memory`, `WAYLAND_DISPLAY` unset, `TMPDIR` inside the repo.
**Under this recipe the GL driver is llvmpipe**: VAAPI/dmabuf import is impossible, so
headless gates MUST run `hwdec=no` (see R13) and hardware-engine proof is operator-only.

**Live services on this rig (never touch, R4)**: vLLM/Radiance `:8180`, DSH `:3080`,
TTS `:8234`, serve-ts `:1199`/`:9119`, docker containers, Caddy, systemd units, and the
user's mpv wrapper/config.

## 3. Incident ledger → hard requirements

| # | Incident / hazard (observed) | Requirement |
|---|---|---|
| 1 | GTK4 apps (and Loupe) die silently under Xvfb without the special env | **R1** Headless GUI gates ONLY via `scripts/headless-smoke.sh` (recipe above). |
| 2 | DSH sandbox denies writes outside the repo | **R2** All build/test writes repo-relative; runtime config at `~/.config/reelsee/` is allowed (runtime data, not build output). |
| 3 | No pip/network in DSH | **R3** stdlib + PyGObject + **vendored ctypes** only. libmpv is pre-installed by the operator in P0. No `python-mpv`, no pip, no model downloads. |
| 4 | Careless agents kill live services | **R4** No sudo, no apt, no systemd, no container/service restarts, no touching `:8180`. |
| 5 | Secrets in repos | **R5** Repo carries no credentials; config schema has none. |
| 6 | Default-handler mutation mid-build breaks the user's flow | **R6** DSH never touches `xdg-mime`/desktop defaults; operator applies them after §9. |
| 7 | Token paste truncation in dsh-run | **R7** Gates print exact literals `REELSEE_P<n>_DONE`, final `REELSEE_ALL_PHASES_DONE`; commits `P<n>: <summary>`. |
| 8 | Rule softening past a failing gate | **R8** 2 failures of the same gate → `REELSEE_PHASE_n_BLOCKED` + exact failing command + full stderr → STOP. |
| 9 | mpv property getters can block on the demuxer/decoder (slow mount = frozen UI) | **R9** The GTK main thread NEVER calls a synchronous mpv getter. Properties are `mpv_observe_property`-driven, cached in a snapshot dict; UI writes use `mpv_command_async`/`mpv_set_property_async`. Enforced by `tests/test_no_blocking_mpv.py`. |
| 10 | An env var that "should" pin the GPU silently did nothing (GTK_GL_DEVICE no-op, §2) | **R10** Every GPU/decoder selection must be verified at runtime from the live readout (`glGetString(GL_RENDERER)`, `hwdec-current`, `video-codec`) and surfaced in Settings. A selector that cannot prove its effect is shown disabled with the reason. |
| 11 | Freeing a GL render context after GTK destroyed its context = silent segfault on close | **R11** `mpv_render_context_free` runs on `unrealize` **while the GL context is current**, after detaching the update callback; window close must be crash-free in every smoke (rc 0, no `Traceback`/`Fatal`/`Segmentation fault`). |
| 12 | mpv's `run`/`subprocess`/`load-script` execute arbitrary shell/Lua | **R12** Blocklist `{run, subprocess, subprocess-kill, load-script, script-message, script-binding, enable-section, disable-section}` and options `--script*`, `--input-conf`, `--config`, `--include` in the command palette and the advanced panel. Everything else passes through verbatim (that is mpv parity). |
| 13 | DSH gates would chase impossible VAAPI-under-llvmpipe proofs | **R13** Headless gates pin `hwdec=no`; hardware decode/GPU claims are operator gates (P0 proof + §9). |
| 14 | Long filenames clamping minimum window width (Looksee FTR4) | **R14** Title label ellipsizes; window min width driven only by chrome buttons. |
| 15 | `import gi` resets LC_NUMERIC → libmpv aborts `mpv_create` | **R15** `locale.setlocale(LC_NUMERIC, "C")` immediately before `mpv_create` and render-context creation (§0.1). |
| 16 | `loadfile` before the render context exists = frozen playback, no update callbacks | **R16** Per window: render context first, `loadfile` only after (queued if not realized) (§0.3). |
| 17 | mpv auto-VO (no `vo=libmpv`) = extra window + probe storm + llvmpipe segfault; `vo=gpu-next` + render ctx segfaults | **R17** Init set pins `vo=libmpv` + `gpu-api=opengl`; no other vo is ever set (§0.2). |
| 18 | Rendering to FBO 0 = white window, zero video pixels (GTK composites its own offscreen FBO) | **R18** Query `GL_DRAW_FRAMEBUFFER_BINDING` (0x8CA6) inside the render callback and pass THAT id in `MPV_RENDER_PARAM_OPENGL_FBO` (§0.4). |
| 19 | Missing `mpv_render_context_update()` ack after the update callback = stall after one frame | **R19** Update-flag + 10 ms main-thread poll + `mpv_render_context_update()` + repaint iff `flags & 1` (§0.5). |
| 20 | `set hwdec` alone never re-inits the decoder | **R20** Engine apply = `set hwdec <mode>` + `set vid no` → `set vid auto`; verify observed `hwdec-current`, self-fallback vaapi→vaapi-copy→no (§0.7/§0.8). |

## 4. Architecture, hop by hop

One process, N windows. Each window owns one mpv handle and one render context.

1. **Launch** — `reelsee [PATH|URL ...] [--new-window]`. Before any GTK import:
   read `~/.config/reelsee/config.json`; if `engine.renderer_override != "auto"` export
   `DRI_PRIME=<n>` (mapped from PCI via `/dev/dri/by-path`), then `import gi`. A change of
   the override re-execs the process (`os.execv`) so the env lands before `Gtk.init`.
2. **Window** — `Gtk.ApplicationWindow` (no headerbar, class `reelsee-black`) →
   `Gtk.Overlay` → child `Gtk.GLArea` (render surface, `hexpand`/`vexpand`), overlay
   children = `ChromeTop` (title/track/loop buttons) and `ChromeBottom` (seek bar, times,
   speed, volume). Chrome is pure GTK/CSS — never drawn in-frame — so it stays crisp and
   animates at compositor speed.
3. **Engine selection** — `src/engine.py` enumerates GPUs (`/dev/dri/by-path`, sysfs
   connector status, `lspci -mm`, plus `mpv --no-config --vulkan-device=help` names) and
   builds the selector list: `Auto (display GPU)`, each GPU as `vaapi`, each GPU as
   `vulkan` (only if the capability probe passes), and `CPU decode`. The user's chosen
   engine maps to mpv init options (§5.2). Capability probe results cache in
   `~/.local/state/reelsee/engines.json` keyed by `pci|hwdec|codec`.
4. **Playback** — `MpvCore` (per window): `locale.setlocale(LC_NUMERIC, "C")` →
   `mpv_create` → `mpv_set_option_string` for the init set (MUST include `vo=libmpv` +
   `gpu-api=opengl`, §0.2) → `mpv_initialize` → one **event thread** loops
   `mpv_wait_event(handle, 0.1)` (0.1 s timeout so shutdown is prompt), handling
   `MPV_EVENT_PROPERTY_CHANGE`, `MPV_EVENT_FILE_LOADED`, `MPV_EVENT_END_FILE`,
   `MPV_EVENT_SHUTDOWN`. It maintains `Snapshot` (a plain dict) and posts
   `GLib.idle_add(on_snapshot)` at most every 50 ms (coalesced). `loadfile` is only ever
   sent AFTER that window's render context exists (§0.3) — queue the path on realize.
5. **Render** — on GLArea `realize`: `make_current()`, re-assert LC_NUMERIC=C, create the
   render context with `MPV_RENDER_PARAM_API_TYPE = MPV_RENDER_API_TYPE_OPENGL` +
   `MPV_RENDER_PARAM_OPENGL_INIT_PARAMS` (get_proc_address via `eglGetProcAddress` from
   `libEGL.so.1`). `mpv_render_context_set_update_callback` → the callback (mpv thread)
   ONLY sets a `threading.Event`; a 10 ms main-thread poll calls
   `mpv_render_context_update()` when the flag is set and queues a repaint iff
   `flags & MPV_RENDER_UPDATE_FRAME` (§0.5 — GLib is never called from the callback).
   On GLArea `render`: query the bound draw FBO (`GL_DRAW_FRAMEBUFFER_BINDING`, 0x8CA6)
   and pass THAT id in `MPV_RENDER_PARAM_OPENGL_FBO = {fbo:<bound>, w:fb_w, h:fb_h,
   internal_format:0}` + `MPV_RENDER_PARAM_FLIP_Y=1` (§0.4 — FBO 0 renders nothing
   visible). `fb_w/fb_h` = widget size × `get_scale_factor()` (HiDPI correct). The
   reference implementation of this whole hop is `scripts/spike_render.py` — reuse it.
6. **Chrome state machine** — `src/chrome.py` holds a pure `ChromeState` class (injected
   clock): `on_pointer_motion()`, `on_pointer_leave()`, `on_playing_changed()`,
   `on_key()`, `tick(now_ms)` → `visible: bool` + `reason`. GTK side applies it as opacity
   (180 ms transition) plus a blank cursor. Rules: hidden only while playing; 3000 ms of
   pointer inactivity hides; any pointer motion/click/wheel shows and re-arms; pause/stop/
   EOF always shows; hovering the bottom 80 px keeps it visible; a keyboard seek while
   hidden shows a 1200 ms transient OSD (time + speed) without revealing the bars.
7. **Input** — `src/keymap.py`: mpv-`input.conf`-compatible syntax is the model. Defaults
   (all overridable in `~/.config/reelsee/input.conf`): `Space` play/pause, `←/→` seek
   ±5 s, `↑/↓` volume ±5, `j/;`? no — `Shift+←/→` ±30 s, `,/.` frame-step, `9/0` volume,
   `m` mute, `f`/`F11` fullscreen, `Esc` leave fullscreen, `s` screenshot, `a` cycle audio
   track, `v` cycle subtitle track, `[` `]` set A/B (§5.4), `Backspace` clear A/B, `l`
   cycle loop-file, `Shift+L` loop-playlist, `[`/`]` speed down/up? — **final keymap is
   specified in `src/keymap.py` and documented in README**; the only required bindings are
   the ones §9 tests: Space, ←/→, [ ], Shift+B, m, f/F11, s.
8. **IO** — `src/reelsee_io.py`: folder scan (GLib `enumerate_children_async` + batched
   `next_files_async`), natural sort, `Gio.FileMonitor`, video-extension filter, and an
   async probe of the next playlist entry (`ffprobe -v error -show_entries …` on a
   **worker thread**, cancelled when superseded). `REELSEE_FAKE_LATENCY_MS` env injects
   artificial delay in this module only (test hook, default 0).
9. **Watchdog** (§Appendix C) — samples the snapshot every 1 s while playing; a pure
   `decide(samples, config)` returns `hold|demote|stop`; the applier walks the demotion
   chain, applies the new engine to the live instance (P0-proven mechanism), and shows a
   monochrome toast.
10. **Multi-window** — `WindowRegistry`: each new file/URL opens a new window unless
    `--new-window` is absent and one window is empty. Engine round-robin when
    `engine.decoder == "auto"` (spread N windows over distinct GPUs). Audio focus: only
    the focused window keeps audio (transient mute on others; config `audio.focus_mute`).
11. **Shutdown** — per window: detach update callback → free render context (current GL
    context) → `mpv_terminate_destroy` from a worker thread if it blocks > 200 ms (never on
    the main loop) → persist resume/AB/config.

### 4.1 Why this architectural choice (do not re-litigate in DSH phases)

- **libmpv render API, not `--wid`**: GTK4 cannot hand a foreign window id to another
  process on Wayland, and `--wid` embedding is unsupported there. The render API is mpv's
  own embedding path and keeps decode/scale/subtitle rendering inside mpv (so subtitles,
  `--vf`, interpolation and color management behave exactly like mpv).
- **Not GStreamer/GtkVideo**: that would make R-9 (mpv parity) a multi-year project.
- **Not a separate mpv process**: we would lose the custom chrome and per-window state.
- **Python is only the control plane**: decode, filter and render all happen in libmpv's
  C threads; Python sees ~1 render call per frame per window and a 50 ms-throttled
  snapshot. That is what makes R-6 achievable; the plan's rule is "no per-frame property
  reads, no per-frame allocation".

## 5. Contracts (exact shapes)

### 5.1 Files and paths
```
~/.config/reelsee/config.json         settings (atomic: tmp + os.replace)
~/.config/reelsee/input.conf          optional keymap overrides (mpv syntax)
~/.local/state/reelsee/resume.json    {abs_path: {"pos": <float>, "dur": <float>, "ts": <int>}}  (LRU 500)
~/.local/state/reelsee/ab.json        {abs_path: {"a": <float>, "b": <float>}}                 (LRU 100)
~/.local/state/reelsee/engines.json   {"<pci>|<hwdec>|<codec>": {"ok": bool, "ts": int}}
```

### 5.2 config.json (version 1; every key optional, unknown keys preserved)
```json
{
  "version": 1,
  "audio":    {"muted": true, "volume": 100.0, "focus_mute": true, "exclusive": false},
  "engine":   {"decoder": "auto",            /* auto | cpu | <pci>  (see §5.3) */
               "hwdec_pref": "vaapi",        /* vaapi | vulkan                       */
               "renderer_override": "auto",  /* auto | DRI_PRIME index (string)      */
               "cpu_threads": 0,             /* 0 = auto                             */
               "watchdog_enabled": true,
               "watchdog_fps_floor_pct": 90,
               "watchdog_drop_ceiling_pct": 3,
               "retry_on_new_file": true},
  "playback": {"speed": 1.0, "loop_file": "no", "loop_playlist": "no",
               "autoplay": true, "resume": true, "resume_min_s": 300,
               "preload_next": false, "keep_open": true},
  "ab":       {"count": "inf"},
  "ui":       {"chrome_timeout_ms": 3000, "osd_ms": 1200, "seek_step_s": 5,
               "seek_step_long_s": 30, "theme": "black"},
  "subtitles":{"visible": true, "scale": 1.0, "delay_s": 0.0, "auto": "exact"},
  "screenshot":{"directory": "~/Pictures", "format": "png", "jpeg_quality": 90},
  "mpv":      {"options": {}}                /* T2 raw option overrides, applied at init */
}
```
Rules: missing/corrupt file → defaults, never crash; unknown keys preserved on write;
writes debounced 250 ms and atomic; **mute and volume are written on every user change**
(R-4). `engine.decoder` accepts exactly: `"auto"`, `"cpu"`, or a PCI slot string such as
`"0000:13:00.0"`, optionally suffixed `":vulkan"` (e.g. `"0000:03:00.0:vulkan"`).

### 5.3 Engine model (`src/engine.py`)
```python
@dataclass(frozen=True)
class Engine:
    id: str            # "auto" | "cpu" | "0000:13:00.0" | "0000:13:00.0:vulkan"
    label: str         # "iGPU — Radeon Graphics (VAAPI)" etc, built from sysfs+lspci
    kind: str          # "auto" | "vaapi" | "vulkan" | "cpu"
    pci: str | None
    render_node: str | None   # "/dev/dri/renderD130" resolved via by-path at use time
    is_display_gpu: bool
```
`mpv_options(engine, cfg, n_windows)` → dict of mpv option strings:
```
auto            : hwdec=auto-safe, hwdec-codecs=all      (+ vaapi-device=<display node>)
<display pci>   : hwdec=vaapi       (zero-copy: decode device == render device)
<other pci>     : hwdec=vaapi-copy  (decode on that GPU, frames copied for iGPU render)
<...>:vulkan    : hwdec=vulkan|vulkan-copy + vulkan-device="<exact RADV name>"
cpu             : hwdec=no + vd-lavc-threads=<0=auto → max(1, (nproc-2)//max(1,n_windows))>
```
Invariants: `vaapi-device` is always an **absolute render-node path resolved at call time**;
`vulkan-device` is always one of the exact strings from `mpv --vulkan-device=help`
(current values are in §2 — re-read them at runtime, never hardcode).
**Engagement verification (R20):** after `MPV_EVENT_FILE_LOADED`, watch observed
`hwdec-current` for 3 s; if the requested GPU engine did not engage (reports `no`),
self-fallback one step (`vaapi → vaapi-copy → no`) with the §0.7 mechanism and record it
in `engines.json` + a toast. `vaapi-copy` is the safe default for non-display GPUs.

### 5.4 A/B contract (`src/ab.py`, pure)
`ABState`: `a: float|None`, `b: float|None`, `count: str|int`.
- `set_a(t)` → `a=t`, clears `b` if `b is not None and b <= t + 0.15`.
- `set_b(t)` → requires `a is not None and t >= a + 0.15` else no-op with reason
  `"B must be at least 0.15s after A"`; sets `b=t`.
- `clear()` → both `None`.
- `apply(core)` → `set ab-loop-a <a|no>`, `set ab-loop-b <b|no>`, `set ab-loop-count <count>`.
- UI: two 3 px white ticks on the seek bar plus a 12 %-white shaded band between them;
  `A`/`B` keyboard + two small buttons in `ChromeBottom`'s left cluster; active state is a
  filled (white) marker. Persisted per path in `ab.json` (R-5).

### 5.5 Desktop entry + package
`data/pro.krisforbes.Reelsee.desktop`: `Type=Application`, `Name=Reelsee`,
`GenericName=Video Player`, `Exec=reelsee %U`, `Icon=reelsee`, `Terminal=false`,
`Categories=AudioVideo;Player;Video;`, and a `MimeType=` list built from
`/usr/share/applications/mpv.desktop` (read it in P10 and copy the `video/*` entries
verbatim; fall back to an explicit list of `video/mp4;video/x-matroska;video/webm;
video/x-msvideo;video/quicktime;video/mpeg;video/ogg;video/x-ms-wmv;`),
plus `X-Reelsee-Version=1`.
`packaging/DEBIAN/control`: `Package: reelsee`, `Version: 0.1.0`, `Architecture: all`,
`Section: video`, `Depends: python3 (>= 3.10), python3-gi, gir1.2-gtk-4.0, gir1.2-adw-1,
libadwaita-1-0, libmpv2`, `Maintainer: Kris Forbes <krisf@users.noreply.github.com>`.
Tree: `/usr/bin/reelsee` (sh wrapper → `exec /usr/bin/python3 /usr/share/reelsee/reelsee.py "$@"`),
`/usr/share/reelsee/*.py`, `/usr/share/applications/pro.krisforbes.Reelsee.desktop`,
`/usr/share/icons/hicolor/scalable/apps/reelsee.svg`, `/usr/share/doc/reelsee/README.md` + copyright.

### 5.6 Phase tokens
`REELSEE_P1_DONE` … `REELSEE_P11_DONE`, `REELSEE_ALL_PHASES_DONE`,
`REELSEE_PHASE_n_BLOCKED`, operator-only `REELSEE_P0_OPERATOR_OK`.

### 5.7 Smoke output contract
`scripts/headless-smoke.sh` prints `SMOKE OK (mode=… elapsed=…s)` on success, exits 1 with
the captured stderr on failure. Smoke **scripts** print machine-checkable lines
`SMOKE_CHECK:<name>=<value>` and exit non-zero on a failed assertion, so a gate can grep
values (e.g. `SMOKE_CHECK:hwdec_current=no`).

## 6. Target repo layout
```
reelsee/
  PLAN.md, DSH-REPORT.md, README.md
  src/reelsee.py            entry point, app, windows registry, CLI
  src/mpvlib.py             vendored ctypes bindings (client + render API) — NO pip
  src/mpvcore.py            MpvCore: handle, event thread, Snapshot, async command queue
  src/render.py             GLArea ↔ mpv_render_context glue (creation, frame, teardown)
  src/chrome.py             ChromeState (pure state machine) + chrome widgets + toast
  src/settings_ui.py        Adw preferences pages (Playback/Audio/Decode&GPU/Subs/UI/Advanced mpv)
  src/engine.py             GPU enumeration, Engine model, capability probe, demotion chain
  src/watchdog.py           pure decide() + sampler/applier
  src/ab.py                 pure ABState
  src/keymap.py             key parsing + default bindings (+ input.conf loader)
  src/reelsee_io.py         async folder scan, natural sort, monitor, async probe, CSS builder
  src/config.py             schema, atomic load/save, migration, state stores
  tests/                    stdlib unittest (pure logic + lints + ctypes conformance)
  testdata/                 committed clips (§6.1) + generator script
  data/pro.krisforbes.Reelsee.desktop, data/reelsee.svg
  packaging/DEBIAN/control, packaging/build-deb.sh, packaging/reelsee-wrapper.sh
  references/               P0-RECON.md, P0-SPIKE.md, visual notes
  scripts/headless-smoke.sh scripts/smoke_*.py scripts/spike_render.py
  buildout/ (gitignored)    scratch/ (gitignored)
```
### 6.1 Test media (committed, tiny)
`testdata/gen_clips.sh` uses ffmpeg to produce (and P1 commits):
`smoke_360p.mp4` (3 s, 640×360, testsrc2 + 440 Hz sine, ~120 KB),
`perf_1080p30.mp4` (10 s, 1920×1080 h264 + aac, ≲2 MB),
`perf_4k30.mp4` (5 s, 3840×2160 h264, ≲4 MB),
`tone_5s.mkv` (5 s, h264 + two audio tracks + a mov_text subtitle track),
`ext.srt` (3 cues) for subtitles. Generator must be deterministic (`-fflags +bitexact
-flags:v +bitexact`, fixed `-crf`, `-preset veryfast`); clips ≤ 4 MB each.

## 7. mpv feature-parity matrix

Tier **T1** = first-class UI control; **T2** = reachable via the Advanced mpv panel /
command palette (searchable, applied live, persisted in `mpv.options`); **T3** = out of
scope, documented in README with the reason. Every T1 row must exist in the app before the
final token.

| Feature | Engine hook | Tier |
|---|---|---|
| Play / pause / stop / quit | `cycle pause`, `stop`, `quit` | T1 |
| Seek absolute / relative / percent | `seek` | T1 |
| Frame step forward/back, exact seek | `frame-step`, `seek … absolute+exact` | T1 |
| Speed 0.1–4.0, pitch-preserving | `speed`, `audio-pitch-correction` | T1 |
| Loop file / playlist (no, 1, inf) | `loop-file`, `loop-playlist` | T1 |
| A/B section repeat + count | `ab-loop-a/b/count` | T1 |
| Chapters list + next/prev | `chapter-list`, `add chapter 1` | T1 |
| Audio track select, external audio, delay | `aid`, `audio-add`, `audio-delay` | T1 |
| Volume / mute (persisted) | `volume`, `mute` | T1 |
| Audio device + exclusive mode | `audio-device`, `audio-exclusive` | T1 |
| Video track select | `vid` | T1 |
| Subtitle track select, external sub, delay, scale, visibility | `sid`, `sub-add`, `sub-delay`, `sub-scale`, `sub-visibility` | T1 |
| Subtitle styling preset (font/size/color/border/margin) | `sub-*` | T1 |
| Aspect override, zoom, pan, rotate, crop | `video-aspect-override`, `video-zoom`, `video-pan-x/y`, `video-rotate`, `video-crop` | T1 |
| Equalizer (brightness/contrast/saturation/gamma/hue) | `brightness`, `contrast`, `saturation`, `gamma`, `hue` | T1 |
| Scale quality presets, deband, dither, interpolation, tscale | `scale`, `cscale`, `dscale`, `deband`, `dither`, `interpolation`, `tscale` | T1 |
| Deinterlace | `deinterlace` | T1 |
| Screenshot (png/jpg, dir, format, clipboard) | `screenshot-*`, `screenshot` cmd | T1 |
| Fullscreen, always-on-top, window title | `fullscreen`, `ontop`, `title` | T1 |
| Resume position | our `resume.json` (+ `save-position-on-quit` in T2) | T1 |
| Hardware decode + GPU pin + CPU decode + watchdog | §5.3, §Appendix C | T1 |
| Playlist (add/remove/reorder/shuffle/save m3u, next/prev) | `playlist-*`, `loadlist`, `playlist-shuffle` | T1 |
| URL / network playback (yt-dlp if present) | `loadfile <url>` | T1 (best-effort) |
| Audio-only mode, cover art | `vid=no`, `cover-art-auto` | T1 |
| OSD on seek / track change | our chrome OSD (mpv `osd-level=0`) | T1 |
| Everything else in libmpv (raw options, filters, demuxer tunables…) | `mpv.options` + panel + palette | T2 |
| Lua/JS scripting, `load-script`, `--script-opts` | — | T3 (R12 blocklist) |
| VapourSynth (`--vf=vapoursynth`) | — | T3 (needs the runtime; not installed) |
| DVD/BluRay/DVB navigation UI | — | T3 |
| `--wid` embedding, macOS/Windows backends, `tct`/terminal output | — | T3 |
| Encoding / transcoding | mpv has none | T3 |

## 8. Phases

Each phase: work → machine gate → exact token printed → `git commit -m "P<n>: <summary>"`.
A gate that fails twice → `REELSEE_PHASE_n_BLOCKED` + the failing command and full stderr.

- **P0 (OPERATOR) — DONE 2026-10-24, token `REELSEE_P0_OPERATOR_OK` already emitted.**
  Deliverables in repo: `vendor/` (libmpv 0.41.0 + 6 deps), `references/P0-RECON.md`,
  `references/P0-SPIKE.md` (PROOF-A/B/C/D/E all recorded), `testdata/` fixtures,
  `scripts/spike_render.py` (render-API reference implementation, PROOF-B PASS:
  104 renders, 30 fps, 57,600 unique colors, rc=0), `scripts/spike_hwdec_switch.sh`
  (PROOF-A: bidirectional runtime engine switch PASS). DSH reads these, never re-derives.
- **P1 scaffold + bindings** — git init; layout; `.gitignore`; `scripts/headless-smoke.sh`
  (from Looksee, adapted); `testdata/gen_clips.sh` + committed clips; README stub;
  `src/mpvlib.py` client API subset (`mpv_create`, `_set_option_string`, `_initialize`,
  `_terminate_destroy`, `_command_string`, `_command_async`, `_set_property_async`,
  `_get_property_string` (worker-thread only), `_observe_property`, `_unobserve_property`,
  `_wait_event`, `_error_string`, `_client_api_version`); `src/config.py` with schema +
  atomic IO.
  Gate: `tests/test_mpvlib_structs.py` compiles a gcc probe from `vendor/usr/include/mpv/*.h`
  and compares `sizeof`/`offsetof` for every ctypes struct (mismatch = fail);
  `tests/test_config.py` (defaults, corrupt file, unknown-key preservation, atomic write);
  a live handle round-trip prints `SMOKE_CHECK:mpv_version=<x>` and exits 0; unittest
  discover green; `py_compile` clean. → `REELSEE_P1_DONE`
- **P2 render + playback** — `src/render.py` + `src/mpvcore.py`; open a file, play, pause,
  seek, `F` fullscreen; HiDPI-correct FBO; clean teardown (R11).
  Gate: `LOOKSEE`-style smoke opens `testdata/perf_1080p30.mp4`, waits 2 s, xwd capture
  **cropped to 640x360+0+0** (window maps at 0,0 under WM-less Xvfb — §0.10), asserts
  mean luminance > 8 and ≥200 distinct colours (P0 spike passes at mean≈127 /
  57,600 colors — a white Adwaita window gives 1 color and must fail this), `time-pos`
  advancing, `hwdec-current=no` (R13), `estimated-vf-fps ≥ 20`; second smoke closes the
  window and requires rc 0 with no `Traceback|Fatal|Segmentation`. → `REELSEE_P2_DONE`
- **P3 chrome + autohide** — black/monochrome chrome, seek bar with drag + hover preview
  time, time labels, volume, speed chip, toast/OSD, `ChromeState`.
  Gate: `tests/test_chrome_state.py` (≥10 cases: idle→hidden, motion→visible+re-arm,
  pause→visible and stays, keyboard does not re-arm, bottom-hover keeps visible, clock
  jumps); smoke asserts `SMOKE_CHECK:chrome_hidden_after_idle=true` and
  `SMOKE_CHECK:chrome_visible_after_motion=true`. → `REELSEE_P3_DONE`
- **P4 settings + persistence** — Adw preferences window with pages Playback / Audio /
  Decode & GPU / Subtitles / Interface / Advanced mpv; live-apply; debounced atomic writes.
  Gate: `tests/test_config_persist.py` proves mute+volume survive a process restart (write
  in one `/usr/bin/python3` run, read in a second); `tests/test_settings_bind.py` proves
  every schema key has exactly one widget binding; smoke opens/closes settings.
  → `REELSEE_P4_DONE`
- **P5 A/B loop** — `src/ab.py` + markers + persistence + count.
  Gate: `tests/test_ab.py` (set/validate/clear/count/persist ≥12 cases); live integration:
  set A=1.0 B=2.0 via the app's own API, let it play 6 s, assert every observed `time-pos`
  after the first wrap is within `[0.7, 2.3]`, and that clearing restores free playback
  (`SMOKE_CHECK:ab_loops=…`). → `REELSEE_P5_DONE`
- **P6 engines + GPU selector** — enumeration, capability probe, selector UI with live
  readout (`Renderer: …`, `Decoder: vaapi (renderD130)`, `hwdec-current`), CPU decode entry,
  re-exec on renderer override change.
  Gate: `tests/test_engine.py` (by-path→node mapping with a fake sysfs fixture; `mpv_options`
  invariants; display-GPU detection); live: `SMOKE_CHECK:engines=<n>` lists ≥4 entries and
  `SMOKE_CHECK:engine_cpu_hwdec=no`; operator (not DSH) confirms VAAPI engagement in P0/§9.
  → `REELSEE_P6_DONE`
- **P7 overload watchdog** — `src/watchdog.py` pure `decide()` + sampler + applier + toast +
  settings toggles + `--simulate-overload` test flag.
  Gate: `tests/test_watchdog.py` ≥12 cases (startup grace, seeking suppressed, pause
  suppressed, single-sample noise ignored, 3-sample confirm, fps-floor path, drop-ceiling
  path, chain order, retry-on-new-file, disabled switch); live with `--simulate-overload`:
  `SMOKE_CHECK:watchdog_action=demote` and the engine in config changed as specified.
  → `REELSEE_P7_DONE`
- **P8 multi-window performance** — registry, engine round-robin, thread budget, focus-audio
  policy, performance profiles.
  Gate: `tests/test_registry.py` (round-robin, thread math, focus mute, close-one doesn't
  disturb others); smoke opens 4 windows × `smoke_360p.mp4`, all `eof-reached=false` after
  6 s, main-loop heartbeat p95 < 50 ms, clean close of all four (rc 0).
  → `REELSEE_P8_DONE`
- **P9 async IO hardening** — folder scan/monitor/probe, `REELSEE_FAKE_LATENCY_MS`,
  `tests/test_no_sync_fs.py` and `tests/test_no_blocking_mpv.py`.
  Gate: both lints green over `src/`; smoke with `REELSEE_FAKE_LATENCY_MS=3000` shows the
  window within 400 ms and heartbeat p95 < 50 ms during the delay
  (`SMOKE_CHECK:ui_alive_during_slow_io=true`). → `REELSEE_P9_DONE`
- **P10 packaging** — `packaging/build-deb.sh` → `buildout/reelsee_0.1.0_all.deb`,
  desktop file (MIME list from `mpv.desktop`), SVG icon, wrapper, copyright, docs.
  Gate: `.deb` exists; `dpkg-deb -I` parses with contract fields; `dpkg-deb -c` lists ≥6
  expected paths; desktop entry parses via `configparser` with non-empty
  Name/Exec/Icon/Type/MimeType; wrapper smoke exits 0 (`LOOKSEE_SMOKE_INTERP=/bin/sh` style).
  → `REELSEE_P10_DONE`
- **P11 docs + report** — README (features, keyboard table, GPU/decode explanation,
  troubleshooting, build/install), parity matrix §7 rendered, `DSH-REPORT.md` with every
  deviation row, final full-suite run.
  Gate: `python3 -m unittest discover tests` green from a clean checkout; all smokes green;
  README contains the §7 T3 list verbatim; `DSH-REPORT.md` has ≥1 row per phase that had a
  deviation. → `REELSEE_P11_DONE` … `REELSEE_ALL_PHASES_DONE`

## 9. E2E acceptance matrix (OPERATOR runs on the live session; DSH never does these)

1. `/usr/bin/reelsee testdata/perf_1080p30.mp4` on the real Wayland desktop: window black,
   chrome monochrome, video plays smoothly; Settings → Decode & GPU shows
   `Renderer: …raphael_mendocino`, `hwdec-current: vaapi (renderD130)`.
2. Pin decode to dGPU #1 (R9700): `hwdec-current` becomes `vaapi-copy`, video keeps playing,
   `amd-smi` shows VCN activity on that card; switch back to iGPU → `vaapi` again.
3. Select **CPU decode** in the same selector: `hwdec-current = no`, playback continues
   (R-8), and the selector remembers it after restart.
4. Overload (R-7): `--simulate-overload` run shows the toast + engine demotion and the
   config records the fallback; a real-world check plays 4K60 with decode pinned to the iGPU
   and confirms the watchdog fires only when drops are real (fps floor 90 %).
5. Mute (R-4): mute → quit → relaunch (still muted, `config.json` `audio.muted=true`);
   unmute → quit → relaunch (still unmuted). Both directions persist.
6. A/B (R-5): `[` at 0:10, `]` at 0:14 → playback loops 10–14 with visible markers;
   `Shift+B` clears; relaunch of the same file restores the section.
7. Chrome (R-3): 3 s no pointer motion → bars fade, cursor vanishes; motion → returns;
   pause → bars stay.
8. Multi-video (R-6): open four 1080p30 files (4 windows): all ≥55 fps (Settings readout),
   only the focused window has audio, UI remains responsive; total dropped frames < 1 % over 60 s.
9. Slow mount (R-1): open a file from a slow mount (an rclone/sshfs mount): the
   window appears immediately, chrome stays responsive, `Esc`/close always works.
10. Subtitles/audio: external `.srt` loads; delay ±; two audio tracks switch; volume/mute.
11. Screenshot `s` writes PNG to the configured directory; fullscreen `F11`/`Esc` clean.
12. Uninstall/rollback: `sudo apt remove reelsee` removes the app; the user's
    `~/.local/bin/mpv` wrapper remains the video default (Reelsee never claimed it unless
    the operator sets it).

## 10. What NOT to do
- No `--wid` embedding, no separate mpv process, no GStreamer/GtkVideo, no `python-mpv`
  from pip, no VapourSynth, no mpv Lua/JS scripts (R12 blocklist).
- No sudo/apt/systemd/service restarts; never touch `:8180`, `:3080`, docker, Caddy (R4).
- Never modify `~/.config/mpv/`, `~/.local/bin/mpv`, `~/bin/mpv.AppImage` — read-only
  reference (R2/R4).
- No synchronous mpv property getters on the GTK main thread (R9); no render-API calls
  outside GLArea callbacks with the context current (R11).
- No hardcoded `renderD12x`; resolve through `/dev/dri/by-path` every time (R10).
- Do not rely on `GTK_GL_DEVICE` (no-op here, §2). Do not assume a pin worked — read the
  live renderer string (R10).
- Do not gate hardware decode under llvmpipe/Xvfb (R13).
- No scope creep: no video editing, no library database, no casting, no cloud sync, no
  account features. Playlist + the §7 T1 list is the whole feature surface.
- No writes outside the repo except the three `~/.config/reelsee` / `~/.local/state/reelsee`
  runtime paths (R2).

## 11. Deviations protocol
Adapt where reality differs (mpv header constants, GTK 4.22 signatures, Adw page APIs,
`mpv_render_context_report_swap` timing) while keeping requirements R1–R14, contracts §5,
and the §7 T1 list intact. Every deviation gets a `DSH-REPORT.md` row:
`<phase> | what differed | decision | requirement impact`. Two failed attempts on the same
gate → `REELSEE_PHASE_n_BLOCKED` + exact command + full stderr → STOP.

## Dispatch prompt (whole-build single-run)

    READ AND EXECUTE PLAN.md end-to-end, phases P1..P11 strictly in order, commit after
    each phase, print REELSEE_P1_DONE .. REELSEE_P11_DONE as each verify gate goes green,
    and the final exact token REELSEE_ALL_PHASES_DONE.

    HARD RULES:
    - Paths RELATIVE to the repo root (the sandbox denies writes outside it).
    - GUI tests ONLY via scripts/headless-smoke.sh (xvfb-run; GSK_RENDERER=cairo;
      LIBGL_ALWAYS_SOFTWARE=1; GDK_BACKEND=x11; WAYLAND_DISPLAY unset), and headless
      playbacks MUST use hwdec=no (llvmpipe cannot import dmabuf).
    - /usr/bin/python3 ONLY (the PATH python3 venv has no gi). No pip, no network,
      no sudo, no package installs, no systemd, no service restarts.
    - libmpv is VENDORED, not system-installed (P0): the library and its six runtime deps
      live in vendor/usr/… . Every GUI process and every libmpv-touching test runs with
      LD_LIBRARY_PATH=<repo>/vendor/usr/lib/x86_64-linux-gnu (loading by full path alone
      does NOT resolve the vendored deps). Read vendor/usr/include/mpv/client.h and
      vendor/usr/include/mpv/render.h for every struct layout and enum value — never
      invent a constant. tests/test_mpvlib_structs.py must stay green.
    - scripts/spike_render.py is the PROVEN render reference implementation (PROOF-B PASS).
      Copy its verified mechanism verbatim: LC_NUMERIC=C at creation, vo=libmpv +
      gpu-api=opengl, render context before loadfile, bound-FBO (GL_DRAW_FRAMEBUFFER_BINDING)
      rendering, Event-flag update poll + mpv_render_context_update(), teardown order
      (§0.1–§0.6). Do not re-derive the render pipeline from scratch.
    - The GTK main thread never calls a synchronous mpv getter; all properties arrive via
      mpv_observe_property into the Snapshot (§4.4/§4.5, R9).
    - Never modify ~/.config/mpv/, ~/.local/bin/mpv, ~/bin/mpv.AppImage, or any live
      service on this host.
    - Blocklist in the command palette/advanced panel: run, subprocess, subprocess-kill,
      load-script, script-message, script-binding, --script*, --input-conf, --config,
      --include (R12).
    - Every phase: run the full unit suite + that phase's smoke before the token; commit
      after the token with message "P<n>: <summary>". Token must be printed as a bare
      literal on its own line.

---

## Appendix A — P0 record (COMPLETED 2026-10-24; historical, DSH does not re-run)

Executed by the operator. No sudo was used; nothing was apt-installed system-wide.
libmpv 0.41.0-2ubuntu4 + libmpv-dev were downloaded as `.deb`s and extracted into
`vendor/` together with six runtime deps (`libmujs3 liblua5.2-0 libsndio7.0 libsixel1
libxpresent1 libva-wayland2`); `ldd` on the vendored `libmpv.so.2` resolves clean under
`LD_LIBRARY_PATH=vendor/usr/lib/x86_64-linux-gnu`; client API version = 2.5.

All proofs PASSED; raw evidence and reproduction commands in `references/P0-SPIKE.md`,
environment matrices in `references/P0-RECON.md`:

- **PROOF-A** — runtime engine switch: `set hwdec <mode>` alone does nothing; with
  `set vid no → set vid auto` the decoder re-inits in both directions mid-playback
  (position preserved). `scripts/spike_hwdec_switch.sh` = reproducible proof.
- **PROOF-B** — GLArea + render API: real frame visible (104 renders, 30 fps,
  57,600 unique colors, clean rc=0) headless and on the live session.
- **PROOF-C** — `DRI_PRIME=2` → R9700 gfx1201; `GTK_GL_DEVICE` is a no-op on this stack.
- **PROOF-D** — vulkan device names re-read and recorded (§2).
- **PROOF-E** — `vo=gpu-next` + render context SEGFAULTS → `vo=libmpv` is mandatory.

Token `REELSEE_P0_OPERATOR_OK` emitted. P1 dispatch cleared.

## Appendix B — engine enumeration algorithm (`src/engine.py`)

1. `glob('/dev/dri/by-path/pci-*-render')` → `{pci_slot: render_node_path}` (reboot-stable).
2. For each render node: `/sys/class/drm/<node>/device/uevent` → `DRIVER` (must be `amdgpu`
   for VAAPI), `PCI_ID`; `lspci -mm -s <pci>` → vendor + device marketing name.
3. Display GPU: any `/sys/class/drm/card*-*/status` == `connected` whose card belongs to the
   PCI slot (resolve `/sys/class/drm/cardN/device` symlink). If several, prefer the one
   whose connector is the session's primary output; else first.
4. Friendly labels: `iGPU — <device name> (VAAPI)` when `pci` matches the display GPU's
   `device` class (Granite Ridge iGPU), else `dGPU — <device name> (VAAPI)`; append
   `#1/#2` ordered by PCI slot for duplicates.
5. Vulkan entries: parse `mpv --no-config --vulkan-device=help` lines
   (`'<name>' (GPU <n>, PCI ID <id>, UUID …)`) → attach exact `name` to the matching PCI.
6. Capability probe (cached in `engines.json`, 7-day TTL): for engine E, decode a few
   frames of a small clip through the app's own embedded path (fresh hidden `vo=libmpv`
   render context on the LIVE display) and read observed `hwdec-current`. **P0 finding:
   external `vo=gpu` probes under bare Xvfb are INVALID (no GL → audio-only playback)
   and `vo=null` forces `hwdec=no`.** A probe that cannot be run marks the engine
   `unverified` (still selectable, with a warning) rather than silently failing.
7. Selector order: `Auto (display GPU)` → display GPU (VAAPI) → other GPUs (VAAPI) →
   Vulkan variants → `CPU decode (software)` last, always enabled (R-8).

## Appendix C — overload watchdog (exact rules, `src/watchdog.py`)

Inputs: 1 s samples of `{t, playing, seeking, pause, eof, estimated_vf_fps, container_fps,
frame_drop_count, decoder_frame_drop_count, hwdec_current}` from the Snapshot.

```
grace          : ignore first max(4 s, 2 × container_fps_window) of each file-loaded
skip sample    : pause, seeking, core-idle, eof, container_fps == 0, or hwdec_current in {no}
fps_ratio      : estimated_vf_fps / container_fps
frame_delta    : estimated_vf_fps × sample_dt            (fallback: container_fps × dt)
drop_ratio     : (Δframe_drop_count + Δdecoder_frame_drop_count) / max(frame_delta, 1)
overloaded_i   : fps_ratio < fps_floor_pct/100  OR  drop_ratio > drop_ceiling_pct/100
confirm        : 3 consecutive overloaded samples (reset on any clean sample)
action         : demote one step in the chain, wait 3 s, re-arm the counter
chain(vaapi)   : vaapi → vaapi-copy → no
chain(vulkan)  : vulkan → vulkan-copy → vaapi → vaapi-copy → no
chain(auto)    : auto-safe → vaapi-copy → no
chain(cpu)     : (terminal — no action, only a toast if it is still dropping)
apply          : set engine on the live handle (P0-PROOF-A mechanism), update config
                 engine.last_fallback = {from, to, ts, fps_ratio, drop_ratio}, show toast
retry          : on the next FILE_LOADED, restore the configured engine when
                 engine.retry_on_new_file is true
never          : demote while hwdec_current is already "no"; never touch audio; never
                 change the file's playback position
```
Thresholds default: `fps_floor_pct=90`, `drop_ceiling_pct=3`, both in config and in the
settings UI under Decode & GPU → “Auto-fallback on overload”.
