"""MpvCore — one mpv_handle + event thread + Snapshot (PLAN §4.1).

Discipline (R9): the GTK main thread NEVER calls a synchronous mpv getter.
Every mutable UI input arrives via observed properties (observe_property →
mpv_event_property) into `snapshot`; the event thread only sets a
threading.Event and the main thread coalesces (>=50 ms) inside poll().
"""
from __future__ import annotations

import collections
import itertools
import locale
import logging
import threading
import time

import mpvlib as M

log = logging.getLogger("reelsee.core")

# property → mpv format for observe_property.  Values arrive through
# event_property_value() with these exact formats.
OBSERVED_BASE: dict[str, int] = {
    "time-pos": M.MPV_FORMAT_DOUBLE,
    "duration": M.MPV_FORMAT_DOUBLE,
    "pause": M.MPV_FORMAT_FLAG,
    "idle-active": M.MPV_FORMAT_FLAG,
    "eof-reached": M.MPV_FORMAT_FLAG,
    "seeking": M.MPV_FORMAT_FLAG,
    "paused-for-cache": M.MPV_FORMAT_FLAG,
    # R17-D: the OTHER half of "mpv is still filling its cache".  mpv sets
    # cache-buffering-state to the PERCENTAGE of the initial cache fill
    # (100 = ready); it is emitted on change, so it arrives through this
    # observation and never needs a synchronous getter from the GTK thread
    # (R9).  A file can be mid-buffering with paused-for-cache momentarily
    # false (mpv resumes and re-pauses), so the watchdog needs both.
    "cache-buffering-state": M.MPV_FORMAT_INT64,
    "volume": M.MPV_FORMAT_DOUBLE,
    "mute": M.MPV_FORMAT_FLAG,
    "speed": M.MPV_FORMAT_DOUBLE,
    "hwdec-current": M.MPV_FORMAT_STRING,
    "estimated-vf-fps": M.MPV_FORMAT_DOUBLE,
    "container-fps": M.MPV_FORMAT_DOUBLE,
    "frame-drop-count": M.MPV_FORMAT_INT64,
    "decoder-frame-drop-count": M.MPV_FORMAT_INT64,
    "video-codec": M.MPV_FORMAT_STRING,
    "audio-codec": M.MPV_FORMAT_STRING,
    "filename": M.MPV_FORMAT_STRING,
    "media-title": M.MPV_FORMAT_STRING,
}

POST_THROTTLE_S = 0.05  # coalesce idle_add-equivalent posts to >= 50 ms (§4.1)


def _fmt(value) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)


class MpvCore:
    """mpv handle wrapper: async commands in, Snapshot out, event-threaded."""

    def __init__(self, lib, init_options: dict[str, str]):
        self.lib = lib
        self.snapshot: dict[str, object] = {}
        self._lock = threading.Lock()
        self._data_event = threading.Event()      # set by event thread only
        self._queue: collections.deque = collections.deque()
        self._listeners: list = []                # snapshot listeners (main thread)
        self._handlers: dict[str, list] = {}      # file_loaded / end_file / ...
        self._uids = itertools.count(1)
        self._cmd_refs: dict[int, object] = {}
        self._last_post = 0.0
        self._pending_post = False   # data drained but throttled: re-post
        self._stop = threading.Event()
        self.alive = False
        #: R16-B: optional callable(mode) run at the end of apply_hwdec so a
        #: window can re-assert state its own vid write would otherwise clobber.
        self.hwdec_hook = None

        locale.setlocale(locale.LC_NUMERIC, "C")  # §0.1 at the creation site
        h = M.ctypes.c_void_p(lib.mpv_create())
        if not h:
            raise RuntimeError(
                "mpv_create failed — LC_NUMERIC must be 'C' at this point (§0.1)")
        self.h = h
        for k, v in init_options.items():
            rc = lib.mpv_set_option_string(h, k.encode(), _fmt(v).encode())
            if rc < 0:
                log.warning("set_option %s=%s -> %s", k, v, M.error_string(lib, rc))
        rc = lib.mpv_initialize(h)
        if rc < 0:
            lib.mpv_terminate_destroy(h)
            raise RuntimeError(f"mpv_initialize failed: {M.error_string(lib, rc)}")
        self.alive = True
        self.observe(dict(OBSERVED_BASE))
        self._thread = threading.Thread(target=self._event_loop, daemon=True,
                                        name="mpv-events")
        self._thread.start()

    # ------------------------------------------------------------- observing
    def observe(self, props: dict[str, int]) -> None:
        for name, fmt in props.items():
            self.lib.mpv_observe_property(self.h, 0, name.encode(), fmt)

    # -------------------------------------------------------------- commands
    def command(self, *argv: str) -> int:
        """Async command (mpv_command_async) — never blocks the main loop."""
        uid = next(self._uids)
        arr = M.make_command(*argv)
        self._cmd_refs[uid] = arr
        rc = self.lib.mpv_command_async(self.h, uid, arr)
        if rc < 0:
            self._cmd_refs.pop(uid, None)
            log.warning("command %s submit failed: %s", argv, M.error_string(self.lib, rc))
        return rc

    def set_property(self, name: str, value) -> int:
        return self.command("set", name, _fmt(value))

    def loadfile(self, path_or_url: str, *opts: str) -> int:
        """replace-mode load; `opts` are per-file mpv options (key=value)."""
        return self.command("loadfile", path_or_url, "replace", *opts)

    def apply_hwdec(self, mode: str) -> None:
        """Runtime decoder switch — the PROVEN mechanism (§0.7): set hwdec
        ALONE does not re-init; vid must be toggled.

        `hwdec_hook` (set by the window, R16-B) runs AFTER the sequence:
        this mechanism's own vid-toggle unconditionally re-enables video,
        which must not silently undo a suppression the window still owns
        (a hidden window whose decoder is being switched stays off)."""
        self.command("set", "hwdec", mode)
        self.command("set", "vid", "no")
        self.command("set", "vid", "auto")
        if self.hwdec_hook is not None:
            try:
                self.hwdec_hook(mode)
            except Exception:                       # noqa: BLE001
                log.exception("hwdec_hook failed for mode=%s", mode)

    # ------------------------------------------------------ main-thread poll
    def add_listener(self, fn) -> None:
        self._listeners.append(fn)

    def on(self, event: str, fn) -> None:
        self._handlers.setdefault(event, []).append(fn)

    def poll(self) -> None:
        """Called from the main-thread 10 ms timer.  Drains the event queue
        and re-posts the UI at most every 50 ms (§4.1, R9).

        The throttle must never LOSE the last update (P5 finding): data
        drained inside a throttle window stays pending and re-posts once
        the window passes — otherwise a pause event that lands <50 ms
        after the previous post freezes the chrome forever.
        """
        if self._data_event.is_set():
            self._data_event.clear()
            drained = []
            with self._lock:
                while self._queue:
                    drained.append(self._queue.popleft())
            for event, payload in drained:
                for fn in self._handlers.get(event, ()):
                    try:
                        fn(self, payload)
                    except Exception:  # a listener bug must not kill the pump
                        log.exception("listener %s failed", event)
            self._pending_post = True
        if not self._pending_post:
            return
        now = time.monotonic()
        if now - self._last_post >= POST_THROTTLE_S:
            self._last_post = now
            self._pending_post = False
            for fn in self._listeners:
                try:
                    fn(self)
                except Exception:
                    log.exception("snapshot listener failed")

    # ------------------------------------------------------------ event loop
    def _event_loop(self) -> None:  # runs on self._thread — no GLib, no GTK
        lib = self.lib
        while not self._stop.is_set():
            ev = lib.mpv_wait_event(self.h, 0.1)
            if not ev:
                continue
            eid = ev.contents.event_id
            if eid == M.MPV_EVENT_SHUTDOWN:
                break
            elif eid == M.MPV_EVENT_PROPERTY_CHANGE:
                if not ev.contents.data:
                    continue
                ep = M.ctypes.cast(ev.contents.data,
                                   M.ctypes.POINTER(M.MpvEventProperty)).contents
                name = ep.name.decode(errors="replace") if ep.name else ""
                if not name:
                    continue
                value = M.event_property_value(lib, ep)
                with self._lock:
                    self.snapshot[name] = value
                self._data_event.set()
            elif eid == M.MPV_EVENT_FILE_LOADED:
                with self._lock:
                    self._queue.append(("file_loaded", None))
                self._data_event.set()
            elif eid == M.MPV_EVENT_START_FILE:
                with self._lock:
                    self._queue.append(("start_file", None))
                self._data_event.set()
            elif eid == M.MPV_EVENT_END_FILE:
                reason = 0
                if ev.contents.data:
                    ef = M.ctypes.cast(
                        ev.contents.data,
                        M.ctypes.POINTER(M.MpvEventEndFile)).contents
                    reason = ef.reason
                with self._lock:
                    self._queue.append(("end_file", reason))
                self._data_event.set()
            elif eid == M.MPV_EVENT_COMMAND_REPLY:
                uid = ev.contents.reply_userdata
                self._cmd_refs.pop(uid, None)
                if ev.contents.error < 0:
                    log.debug("command reply %s error %s", uid,
                              M.error_string(lib, ev.contents.error))
            elif eid == M.MPV_EVENT_SET_PROPERTY_REPLY:
                if ev.contents.error < 0:
                    log.debug("set-property reply error %s",
                              M.error_string(lib, ev.contents.error))

    # ------------------------------------------------------------- teardown
    def destroy(self) -> None:
        """mpv_terminate_destroy — call only AFTER every render context of
        this handle is freed with a current GL context (§0.6, R11).  May
        block: run from a worker when the last window closed (§4.11)."""
        if not self.alive:
            return
        self.alive = False
        # client.h: NO other libmpv call may run CONCURRENTLY with
        # terminate_destroy — join the event thread (≤0.1 s) FIRST
        # (P2 finding: concurrent mpv_wait_event => heap corruption).
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=2.0)
        self.lib.mpv_terminate_destroy(self.h)

    # ----------------------------------------------------------- worker use
    def get_property_string_sync(self, name: str) -> str | None:  # SYNC-MPV
        """Sync getter — WORKER THREADS ONLY, never the main loop (R9)."""
        p = self.lib.mpv_get_property_string(self.h, name.encode())
        if not p:
            return None
        try:
            return M.ctypes.c_char_p(p).value.decode(errors="replace")
        finally:
            self.lib.mpv_free(p)
