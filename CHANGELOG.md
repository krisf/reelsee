# Changelog

Built with `packaging/build-deb.sh`; the package depends on the distro
`libmpv2` and ships no vendored copy by design.

## 0.2.0

- **Crop to window.** A stateful toggle (right-click → *Crop to window*,
  command palette, or settings → Video) that fills the window with the video
  and clips the overflow instead of letterboxing it: a landscape clip in a
  tall window loses its sides, a portrait clip in a wide window loses top and
  bottom. Aspect-locked resizing is suspended while cropping and re-armed when
  you turn it off, and the setting persists like the others.
- **The chrome is a zero-space overlay.** The bars no longer take layout room,
  so the video owns the entire client area and controls float over it; hiding
  and revealing them cannot resize the picture at all.
- **Chrome stays hidden.** Plain idleness used to resurrect the bars on a
  timer discontinuity; the idle countdown now survives tick gaps and a hidden
  window stays hidden until you move the pointer.
- Repository hygiene: the `install_paths` gate no longer references anyone's
  real media library (it generates its own hostile paths — spaces, apostrophes,
  `$&`, capitalised directory names — and proves the same thing), the incident
  and round reports were genericised, packaging uses the project's public
  identity, `vendor/` (6.3 MB of unpacked Debian libraries) is untracked as the
  docs always claimed, and `README.md`, the screenshots,
  `scripts/make_screenshots.py` and `scripts/make_public.sh` are now tracked, so
  the published repository can be re-minted from a clone by one command.

## 0.1.1

- **The in-video menu bar is gone.** File / Audio / Subtitles / Repeat /
  Playlist / Chapters / Tracks now live in the menu that opens on right-click
  over the video, next to the media title. The screen is either picture or
  controls — never a strip of text sitting on top of the frame. A
  `config.json` that still carries the old visibility setting loads silently,
  and no settings page offers it any more.
- **Resizing keeps the video's aspect ratio.** Drag a corner and a 16:9 clip
  stays 16:9; a 9:16 phone clip gets a tall window instead of pillarboxed
  bars. A newly loaded video sizes its own window to its own shape, fitted
  inside 80 % of the work area. Toggle: settings → Interface, or the command
  palette (`win.toggle-aspect-lock`). GTK4 exposes no geometry hints, so this
  is done by correcting the surface layout — with a re-entrancy guard so the
  correction cannot trigger itself.
- **Repeat sticks.** The repeat choice survives reloading the same file,
  moving to the next/previous entry, and quitting and restarting. Previously
  the mode was pushed to the player on every load from the default, so it
  silently reset — the same keys (`l`, `r`) now go through one writer that
  both applies and persists.
- **New windows remember the last geometry and cascade.** A second file opens
  at the previous window's *exact* size, offset by 50 px in whichever
  direction has room, clamped to stay on screen; the remembered rectangle is
  never a fullscreen one. Placement is an X11 capability, so the packaged
  launcher pins `GDK_BACKEND=x11` (environment-overridable) and a native
  Wayland session degrades honestly to size-only cascading.
- 387 unit tests; the headless acceptance battery gates each of the above,
  including a three-phase window-cascade smoke run three times per pass.

## 0.1.0

First release.

- Embedded **libmpv 0.41** through ctypes bindings verified against the
  headers by a compiled struct probe — no mpv binary, no subprocesses, no
  script loading (option blocklist enforced at the parse boundary).
- Video rendered into a `GtkGLArea` FBO through the libmpv render API.
- **GPU pinning and CPU fallback**: enumerate GPUs, pin one, or let `auto`
  spread windows across GPUs; the app checks *engaged* decode rather than
  trusting what it requested, and the overload watchdog demotes to CPU decode
  when fps collapses (never on dropped frames alone).
- **A/B section repeat** with per-path persistence — mark `A` and `B`, the
  player loops that segment.
- **Mute, volume, speed and resume position persist** across launches;
  resume is our own store, flushed on graceful shutdown.
- Monochrome OLED-black chrome that retracts on pointer idle, drag-anywhere
  window move, fullscreen, always-on-top, OSD toasts.
- Playlists with natural sort and live folder refresh, chapter navigation,
  audio/subtitle track selection with delay and styling, external subtitle and
  audio files, screenshots, speed 0.1–4.0 with pitch correction, equalizer and
  geometry controls, network URLs (`yt-dlp` expansion when installed, raw URL
  otherwise).
- Every remaining libmpv option reachable live from settings → *Advanced mpv*.
- Fully asynchronous filesystem IO: a slow mount cannot lock the UI.
