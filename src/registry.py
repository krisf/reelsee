"""WindowRegistry (§4 item 10) — pure multi-window policy, no GTK.

Policy under test (tests/test_registry.py):
  * open reuse: an empty window is reused unless --new-window
  * engine round-robin when engine.decoder == "auto": windows spread over
    distinct GPU engines in enumeration order (display GPU first, R-8);
    a pinned decoder pins every window to that engine
  * audio focus: only the focused window keeps audio (transient mute on
    others) when audio.focus_mute is on
  * closing one window must not disturb the others (registry stays
    consistent: counters keep counting, rosters stay exact)
"""
from __future__ import annotations

GPU_KINDS = ("vaapi", "vulkan")


class WindowRegistry:
    def __init__(self):
        self._live: list = []
        self._rr = 0

    # ---------------------------------------------------------------- roster
    def add(self, win) -> None:
        if win not in self._live:
            self._live.append(win)

    def remove(self, win) -> None:
        try:
            self._live.remove(win)
        except ValueError:
            pass                      # close-one: idempotent, others intact

    @property
    def count(self) -> int:
        return len(self._live)

    def live(self):
        return [w for w in self._live if not getattr(w, "_closing", False)]

    def others(self, win):
        return [w for w in self.live() if w is not win]

    def empty_window(self):
        for w in self._live:
            if getattr(w, "target", None) is None and not getattr(
                    w, "_closing", False):
                return w
        return None

    # ----------------------------------------------------------------- audio
    def focus_plan(self, focused, focus_mute: bool):
        """Windows to transiently mute (all but the focused one, R-audio)."""
        if not focus_mute or focused is None:
            return []
        return self.others(focused)

    # ---------------------------------------------------------------- engine
    @staticmethod
    def gpu_pool(engines):
        return [e for e in engines if getattr(e, "kind", "") in GPU_KINDS]

    def pick_engine(self, decoder: str, engines, select=None):
        """Engine for the NEXT new window.  auto → round-robin over the GPU
        pool (N windows spread over distinct GPUs); anything else is pinned
        for every window."""
        if (decoder or "auto") != "auto":
            if select is None:
                raise ValueError("select required for pinned decoders")
            return select(decoder, engines)
        pool = self.gpu_pool(engines)
        if not pool:
            # no GPU at all: every window gets CPU decode explicitly
            from engine import select_engine
            return select_engine("cpu", engines)
        e = pool[self._rr % len(pool)]
        self._rr += 1
        return e
