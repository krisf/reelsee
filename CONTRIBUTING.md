# Contributing

## Environment

Debian/Ubuntu-ish; the app needs:

```
sudo apt install python3 python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 \
                 libadwaita-1-0 libmpv2
```

For the headless test harness also install `xvfb`, `x11-utils`,
`python3-xlib`, `ffmpeg` (generates the synthetic clips) and `imagemagick`
(only for `scripts/make_screenshots.py`).

libmpv resolution order at startup — the packaged app relies on the last
one, so a distro `libmpv2` is enough:

1. `$REELSEE_LIBMPV`
2. a `vendor/` directory next to `src/reelsee.py` (packaged vendored build)
3. `vendor/usr/lib/x86_64-linux-gnu/libmpv.so.2` in a dev checkout
4. the system `libmpv2`

If you want to run against a private libmpv (what development used), unpack
one into `vendor/` — the tree layout below is what the resolver expects:

```
apt download libmpv2 libmpv-dev
for d in libmpv2_*.deb libmpv-dev_*.deb; do dpkg-deb -x "$d" vendor/; done
LD_LIBRARY_PATH=vendor/usr/lib/x86_64-linux-gnu /usr/bin/python3 src/reelsee.py clip.mp4
```

`vendor/` is git-ignored on purpose — it is a build artefact, and the
packaged app never needs it.

## Running things

```
/usr/bin/python3 src/reelsee.py <file|folder|URL> [...]

/usr/bin/python3 -m unittest discover tests           # no display required
scripts/headless-smoke.sh render -- /usr/bin/python3 scripts/smoke_render.py
scripts/p11_battery.sh                                # full acceptance battery
xvfb-run -a /usr/bin/python3 scripts/make_screenshots.py   # README images
```

Always use `/usr/bin/python3` for GUI runs: a venv interpreter has no
PyGObject, and the resulting `ImportError` looks like an application bug.

## Making a change

1. Reproduce in a test first. The pure modules (`ab.py`, `chrome.py`,
   `config.py`, `engine.py`, `keymap.py`, `registry.py`, `reelsee_io.py`,
   `watchdog.py`) are unit-testable without a display; prefer that level.
2. If the bug lives in behaviour only a real player has (render, decode
   switch, chrome reveal, shutdown), add a `scripts/smoke_<name>.py` that
   prints `SMOKE_CHECK:<name>=<value>` and exits non-zero on failure, and
   register it in `scripts/p11_battery.sh`.
3. Give the gate fresh state (`REELSEE_CONFIG_DIR` / `REELSEE_STATE_DIR` per
   phase), poll for conditions instead of sleeping, and add a negative
   control when the gate could plausibly pass by doing nothing.
4. Do not modify an existing gate to make it pass. If it is genuinely wrong,
   change it in a separate commit with the reason in the message.
5. Keep the "mpv features are options, not code paths" rule: if libmpv can
   do it, expose it through settings/the palette rather than re-implementing
   it in Python.

Read [`AGENTS.md`](AGENTS.md) before touching the render path, the decode
watchdog, or anything that runs on the GTK main thread — those sections
record constraints that are invisible in the code.

## Reporting a bug

Include: distro + `gtk4`/`libadwaita`/`libmpv2` versions (`apt policy`), GPU
model and which engine you pinned, the failing `SMOKE_CHECK` line if any, and
for playback/decode issues the app's stderr (it names the engine it selected
and any demotion). If you can, attach the `~/.config/reelsee/config.json`
with any private paths removed.

## License

By contributing you agree your contributions are licensed under the MIT
license in [`LICENSE`](LICENSE).
