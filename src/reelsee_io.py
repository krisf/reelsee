"""§4.8 async IO — folder scan, natural sort, monitor, next-entry probe.

Discipline: nothing in this module touches a synchronous filesystem call
(tests/test_no_sync_fs.py additionally forbids the sanctioned-call pragma
here).  Directory enumeration runs through Gio's *async* APIs
(enumerate_children_async + batched next_files_async), so multi-thousand
entry folders never stall the main loop; ffprobe runs on a worker thread
that a superseding probe cancels.

REELSEE_FAKE_LATENCY_MS injects artificial delay in THIS module only
(test hook, default 0) — tests/test_no_sync_fs.py enforces that locality.
"""
from __future__ import annotations

import os
import queue
import re
import subprocess
import threading
import time

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

VIDEO_EXTS = frozenset({
    ".mp4", ".m4v", ".mkv", ".webm", ".avi", ".mov", ".wmv", ".flv",
    ".ts", ".m2ts", ".mpg", ".mpeg", ".mpe", ".ogv", ".ogm", ".vob",
    ".dv", ".3gp", ".asf", ".mxf", ".m3u8",
})

_NUM = re.compile(r"(\d+)")


def fake_latency_s() -> float:
    """Test hook: artificial delay for the completion callbacks below."""
    try:
        return max(0.0, float(os.environ.get("REELSEE_FAKE_LATENCY_MS",
                                             "0")) / 1000.0)
    except ValueError:
        return 0.0


def natural_key(name: str):
    """Digit-aware key so episode2 < episode10 (case-insensitive)."""
    return [int(part) if part.isdigit() else part.lower()
            for part in _NUM.split(name)]


def natural_sort(paths) -> list:
    return sorted(paths, key=lambda p: natural_key(
        p.rsplit("/", 1)[-1] if "/" in p else p))


def is_video(name: str) -> bool:
    dot = name.rfind(".")
    return dot > 0 and name[dot:].lower() in VIDEO_EXTS


class FolderScan:
    """Async, batched directory enumeration → natural-sorted video list.

    start() is non-blocking; on_done(files) fires on the main thread after
    REELSEE_FAKE_LATENCY_MS.  next_files_async(20) keeps memory and
    per-iteration work bounded on huge folders."""

    BATCH = 20

    def __init__(self, path: str, on_done):
        self.path = path
        self.on_done = on_done
        self._cancelled = False
        self._files: list[str] = []
        self._enum = None

    def start(self):
        # idle_add so even constructing the GFile work lands off the
        # critical path of window presentation.
        GLib.idle_add(self._begin)
        return self

    def cancel(self):
        self._cancelled = True

    def _begin(self):
        if self._cancelled:
            return False
        f = Gio.File.new_for_path(self.path)
        f.enumerate_children_async(
            "standard::name,standard::type",
            Gio.FileQueryInfoFlags.NONE, GLib.PRIORITY_DEFAULT, None,
            self._on_enum, None)
        return False

    def _on_enum(self, src, res, _data):
        if self._cancelled:
            return
        try:
            self._enum = src.enumerate_children_finish(res)
        except GLib.Error:
            self._deliver()               # unreadable dir → empty playlist
            return
        self._next_batch()

    def _next_batch(self):
        if self._cancelled or self._enum is None:
            return
        self._enum.next_files_async(self.BATCH, GLib.PRIORITY_DEFAULT,
                                    None, self._on_batch, None)

    def _on_batch(self, src, res, _data):
        if self._cancelled:
            return
        try:
            infos = src.next_files_finish(res)
        except GLib.Error:
            infos = []
        for info in infos:
            name = info.get_name()
            if (info.get_file_type() == Gio.FileType.REGULAR
                    and is_video(name)):
                self._files.append(
                    os.path.join(self.path, name))
        if infos:
            # yield to the main loop between batches (UI stays animating)
            GLib.idle_add(self._next_batch)
        else:
            self._deliver()

    def _deliver(self):
        out = natural_sort(self._files)
        lat_ms = int(fake_latency_s() * 1000)
        # timeout(0), NOT idle_add: a busy main loop (10 ms render ticks)
        # starves G_PRIORITY_DEFAULT_IDLE and would postpone delivery
        # indefinitely; a timeout honors its deadline.
        GLib.timeout_add(lat_ms, lambda: (self._fire(out), False)[1])

    def _fire(self, out):
        if not self._cancelled:
            self.on_done(out)
        return False


class FolderMonitor:
    """Gio.FileMonitor over a directory; 'changed' is debounced and
    re-fires on_changed(path) on the main thread."""

    DEBOUNCE_MS = 250

    def __init__(self, path: str, on_changed):
        self.path = path
        self.on_changed = on_changed
        self._mon = None
        self._tid = None

    def start(self):
        f = Gio.File.new_for_path(self.path)
        self._mon = f.monitor(Gio.FileMonitorFlags.NONE, None)
        self._mon.connect("changed", self._on_event)
        return self

    def _on_event(self, _mon, _file, _other, _evt):
        if self._tid is not None:
            GLib.source_remove(self._tid)
        self._tid = GLib.timeout_add(self.DEBOUNCE_MS, self._fire)

    def _fire(self):
        self._tid = None
        self.on_changed(self.path)
        return False

    def close(self):
        if self._tid is not None:
            GLib.source_remove(self._tid)
            self._tid = None
        if self._mon is not None:
            self._mon.cancel()
            self._mon = None


class NextProbe:
    """ffprobe metadata for the NEXT playlist entry, on a worker thread.
    A superseding probe() cancels the previous one (result dropped).

    R3-P3(a) threading contract (the old "MetadataProbe"-flagged pattern —
    this is the one class in src/ whose worker still called
    GLib.timeout_add): the worker NEVER touches GLib.  Results go onto a
    thread-safe queue with a generation token (same contract as
    UrlResolver, R2-P3/I2); the OWNER drains them with poll() from the
    main-thread tick, so callback delivery is main-thread BY CONSTRUCTION
    and cancellation is a plain Event.
    """

    FFPROBE_ARGS = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration:stream=codec_type,width,height",
        "-of", "default=noprint_wrappers=1:nokey=0",
    ]

    def __init__(self, on_done, ffprobe: str | None = None):
        self.on_done = on_done
        self.ffprobe = ffprobe or os.environ.get("REELSEE_FFPROBE",
                                                 "ffprobe")
        self._results: "queue.Queue[tuple[int, tuple[str, dict]]]" \
            = queue.Queue()
        self._cancel: threading.Event | None = None
        self._thread: threading.Thread | None = None
        self._gen = 0                       # probe generation token

    def probe(self, path: str) -> None:
        self.cancel()                       # supersede: old result dropped
        ev = threading.Event()
        self._cancel = ev
        self._gen += 1
        gen = self._gen
        self._thread = threading.Thread(
            target=self._run, args=(path, ev, gen), daemon=True,
            name="reelsee-probe")
        self._thread.start()

    def cancel(self) -> None:
        """Mark any in-flight result dead (window close / supersede).  The
        worker is a bounded subprocess — nothing to kill beyond invalidating
        its generation."""
        if self._cancel is not None:
            self._cancel.set()

    def poll(self) -> bool:
        """MAIN-THREAD drain: fire on_done(path, info) once when a result
        of the CURRENT generation is ready; results of cancelled/superseded
        generations are dropped.  Returns True if something was delivered."""
        try:
            gen, res = self._results.get_nowait()
        except queue.Empty:
            return False
        if gen != self._gen or (self._cancel is not None
                                and self._cancel.is_set()):
            return False                    # superseded/cancelled: drop it
        self._thread = None
        self.on_done(*res)
        return True

    def _run(self, path: str, ev: threading.Event, gen: int) -> None:
        # worker thread: NO GLib of any kind (R3-P3). queue.put is the
        # only cross-thread act.
        lat = fake_latency_s()
        if lat:
            time.sleep(lat)
        if ev.is_set():
            return
        try:
            r = subprocess.run(self.FFPROBE_ARGS + [path],
                               capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.TimeoutExpired):
            return
        if ev.is_set() or r.returncode != 0:
            return
        self._deliver(ev, gen, path, _parse_ffprobe(r.stdout))

    def _deliver(self, ev, gen, path, info) -> None:
        if ev.is_set() or gen != self._gen:
            return                      # cancelled/superseded: drop
        self._results.put((gen, (path, info)))


def _parse_ffprobe(out: str) -> dict:
    """format=duration:stream=codec_type,width,height flat output → dict."""
    info: dict = {}
    for line in out.splitlines():
        k, _, v = line.partition("=")
        v = v.strip()
        if k == "duration":
            try:
                info["duration"] = float(v)
            except ValueError:
                try:
                    h, m, s = v.split(":")
                    info["duration"] = (int(h) * 3600 + int(m) * 60
                                        + float(s))
                except ValueError:
                    pass
        elif k in ("width", "height") and v.isdigit():
            info[k] = int(v)
        elif k == "codec_type" and v.lower() == "video":
            info["has_video"] = True
    return info


# =========================================================== §7 T1 helpers ==
def is_url(target: str) -> bool:
    """Network-ish open target?  file:// is NOT (plain path semantics)."""
    t = (target or "").strip()
    if not t or t.lower().startswith(("file://", "fd://")):
        return False
    return ("://" in t or t.startswith("magnet:")
            or t.startswith("ytdl:") or t.startswith("ytdl://"))


#: streaming manifests are valid ANYWHERE in the URL (they may carry a
#: token behind ?query — R2-P3: ".m3u8 in a stream URL" must skip yt-dlp)
_STREAM_EXT_RE = re.compile(r"\.(m3u8|mpd|ism)([?#/]|$)", re.IGNORECASE)


def is_direct_media(target: str) -> bool:
    """R2-P3: target mpv can open WITHOUT yt-dlp — path (query/fragment
    stripped) ends in a known media extension, or the URL CONTAINS a
    stream-manifest extension (.m3u8/.mpd behind a query still counts).
    Such targets skip the resolver entirely: yt-dlp on a direct .mp4 URL
    is pure latency (measured: 7 s to fail a dead host)."""
    t = (target or "").strip()
    path = t.split("?", 1)[0].split("#", 1)[0]
    dot = path.rfind(".")
    if dot > 0 and path[dot:].lower() in VIDEO_EXTS:
        return True
    return bool(_STREAM_EXT_RE.search(t))


def build_m3u(paths) -> str:
    """Playlist export (§7 T1 save-m3u): one path per line under #EXTM3U."""
    return "#EXTM3U\n" + "".join(f"{p}\n" for p in paths)


class UrlResolver:
    """yt-dlp `-g` direct-URL resolution on a worker thread (§7 T1 URL
    playback, best-effort).

    R2-P3 (I2) threading contract: the worker NEVER calls GLib (the old
    GLib.timeout_add-from-worker delivery was a P0 anti-pattern).  It
    pushes the result onto a thread-safe queue; the OWNER (window) drains
    it with poll() from its existing main-thread tick — delivery is
    therefore main-thread by construction and cancellation is a plain
    Event.

    R2-P3 result contract: poll() returns (url, direct, err).
      * success      → (url, direct_url, None)
      * any failure  → (url, url, "<resolver message>")  — the UI toasts
        the message honestly instead of silently returning to idle, then
        mpv still gets its best-effort shot at the raw URL (mpv's own
        ytdl hook handles schemes we did not resolve).
    The subprocess is hard-capped at `timeout_s` (config
    playback.ytdlp_timeout, default 5 s — yt-dlp needs ~1 s to resolve a
    live site and must not dangle 25 s on a dead one)."""

    DEFAULT_TIMEOUT_S = 5.0

    def __init__(self, binary: str | None = None,
                 timeout_s: float | None = None):
        self.binary = (binary or os.environ.get("REELSEE_YTDLP_BIN")
                       or "yt-dlp")
        self.timeout_s = float(timeout_s or self.DEFAULT_TIMEOUT_S)
        self._results: "queue.Queue[tuple[int, tuple[str, str, str | None]]]" \
            = queue.Queue()
        self._cancel: threading.Event | None = None
        self._thread: threading.Thread | None = None
        self._gen = 0                       # resolve generation (R2-P3)

    @staticmethod
    def argv(binary: str, url: str) -> list[str]:
        return [binary, "-g", "--no-playlist", "--no-warnings", url]

    @property
    def busy(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def resolve(self, url: str, timeout_s: float | None = None) -> None:
        self.cancel()
        if timeout_s:
            self.timeout_s = float(timeout_s)
        ev = threading.Event()
        self._cancel = ev
        self._gen += 1                  # generation token: a result from a
        gen = self._gen                 # cancelled/superseded run is dead
        self._thread = threading.Thread(
            target=self._run, args=(url, ev, self.timeout_s, gen),
            daemon=True, name="reelsee-ytdlp")
        self._thread.start()

    def cancel(self) -> None:
        """Drop any in-flight result (Esc/click/supersede).  The worker is
        a bounded subprocess (hard timeout) — nothing to kill beyond
        marking its generation dead."""
        if self._cancel is not None:
            self._cancel.set()

    def poll(self):
        """MAIN-THREAD drain: returns (url, direct, err) once when a
        result of the CURRENT generation is ready, else None.  Results of
        cancelled/superseded generations are dropped — a stale yt-dlp
        answer must never overwrite a newer open."""
        if self.busy:
            return None
        try:
            gen, res = self._results.get_nowait()
        except queue.Empty:
            return None
        if gen != self._gen or (self._cancel is not None
                                and self._cancel.is_set()):
            return None                 # superseded/cancelled: drop it
        self._thread = None
        return res

    def _run(self, url: str, ev: threading.Event, timeout_s: float,
             gen: int) -> None:
        # worker thread: NO GLib of any kind (I2). queue.put is the only
        # cross-thread act.
        try:
            r = subprocess.run(self.argv(self.binary, url),
                               capture_output=True, text=True,
                               timeout=timeout_s)
        except FileNotFoundError:
            self._deliver(ev, gen, url, url, "yt-dlp not found")
            return
        except subprocess.TimeoutExpired:
            self._deliver(ev, gen, url, url,
                          f"yt-dlp timed out after {timeout_s:.0f}s")
            return
        except OSError as exc:
            self._deliver(ev, gen, url, url, f"yt-dlp failed: {exc}")
            return
        if r.returncode != 0:
            msg = (r.stderr or r.stdout or "").strip().splitlines()
            tail = msg[-1] if msg else f"exit {r.returncode}"
            self._deliver(ev, gen, url, url, tail[:160])
            return
        for line in r.stdout.splitlines():
            if "://" in line:
                self._deliver(ev, gen, url, line.strip(), None)
                return
        self._deliver(ev, gen, url, url, "yt-dlp returned no stream URL")
        return

    def _deliver(self, ev, gen, url, direct, err) -> None:
        if ev.is_set() or gen != self._gen:
            return                      # cancelled/superseded: drop
        self._results.put((gen, (url, direct, err)))


class NewestFileFinder:
    """Async 'newest file matching prefix+exts in dir' (clipboard copy after
    screenshot).  Pure Gio async enumeration — never a sync stat/listdir."""

    def __init__(self, directory: str, prefixes, exts, on_done):
        self.directory = directory
        self.prefixes = tuple(prefixes)
        self.exts = tuple(exts)
        self.on_done = on_done
        self._best: tuple[int, str] | None = None
        self._enum = None

    def start(self):
        f = Gio.File.new_for_path(self.directory)
        f.enumerate_children_async(
            "standard::name,standard::type,standard::modification-time",
            Gio.FileQueryInfoFlags.NONE, GLib.PRIORITY_DEFAULT, None,
            self._on_enum, None)
        return self

    def _on_enum(self, src, res, _data):
        try:
            self._enum = src.enumerate_children_finish(res)
        except GLib.Error:
            self._deliver()
            return
        self._next_batch()

    def _next_batch(self):
        self._enum.next_files_async(64, GLib.PRIORITY_DEFAULT, None,
                                    self._on_batch, None)

    def _on_batch(self, src, res, _data):
        try:
            infos = src.next_files_finish(res)
        except GLib.Error:
            infos = []
        for info in infos:
            name = info.get_name()
            if (info.get_file_type() == Gio.FileType.REGULAR
                    and name.startswith(self.prefixes)
                    and name.lower().endswith(self.exts)):
                # R2-P4(N3): some GIO backends hand out GFileInfos without
                # time::modified even though the query asked for it;
                # get_modification_time() on such an info prints
                # GLib-GIO-CRITICAL ×N (seen ×8 in t1 clipboard leg).
                # has_attribute() first — identical result, silent.
                mtime = (info.get_modification_time().tv_sec
                         if info.has_attribute("time::modified") else 0)
                if self._best is None or mtime >= self._best[0]:
                    self._best = (mtime, os.path.join(self.directory, name))
        if infos:
            GLib.idle_add(self._next_batch)
        else:
            self._deliver()

    def _deliver(self):
        path = self._best[1] if self._best else None
        GLib.timeout_add(0, lambda: (self.on_done(path), False)[1])


def save_text_async(path: str, text: str, on_done=None) -> None:
    """Async UTF-8 text write (save-m3u); on_done(ok) on the main thread.

    P11 API note: this GI build exposes neither save_to_bytes_async nor
    replace_contents_bytes_finish; replace_contents_async/finish is the
    working async roundtrip (probed live).
    """
    f = Gio.File.new_for_path(path)
    buf = text.encode("utf-8")
    holder = [buf]  # GI borrows the buffer; PyGObject does NOT keep it alive
    # across the async call — without this pin the write lands on freed
    # memory (observed live: garbage bytes in the m3u).

    def _done(src, res, _data):
        ok = False
        try:
            ok = bool(src.replace_contents_finish(res)[0])
        except GLib.Error:
            pass
        except Exception:                        # noqa: BLE001
            import logging
            logging.getLogger("reelsee.io").exception("save_text_async cb")
        holder.clear()
        if on_done is not None:
            GLib.timeout_add(0, lambda: (on_done(ok), False)[1])

    f.replace_contents_async(buf, None, False,
                             Gio.FileCreateFlags.NONE, None, _done, None)
