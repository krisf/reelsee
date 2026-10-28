"""WindowRegistry (§4 item 10) — pure multi-window policy, no GTK.

Policy under test (tests/test_registry.py):
  * open reuse: an empty window is reused unless --new-window
  * engine choice when engine.decoder == "auto": the decode plan (R16-D) —
    the integrated iGPU for the first `capacity` windows, CPU beyond it, CPU
    for everything when no integrated card qualifies.  A dGPU is NEVER chosen
    automatically; a pinned decoder pins every window to that engine
  * over-capacity demotion targets the window that gained focus LAST, and
    never a window the user pinned to a decoder by hand
  * audio focus: only the focused window keeps audio (transient mute on
    others) when audio.focus_mute is on
  * closing one window must not disturb the others (registry stays
    consistent: counters keep counting, rosters stay exact)
"""
from __future__ import annotations

GPU_KINDS = ("vaapi", "vulkan")


def demotion_target(windows, capacity: int):
    """Which window the decode plan demotes to CPU when the GPU carries more
    than `capacity` streams (§D.2): the one that gained focus LAST.

    Pure — it reads three attributes off the windows (`gpu_decoding()`,
    `_decode_user`, `_focus_seq`) and touches nothing else.  Windows the user
    pinned to a decoder by hand are never candidates: the per-video GPU/CPU
    toggle always overrides the plan for its window.  Returns None when the
    GPU is within budget (or nothing is GPU-decoded and demote-able)."""
    try:
        cap = max(0, int(capacity))
    except (TypeError, ValueError):
        cap = 0
    over = [w for w in windows
            if getattr(w, "gpu_decoding", None) is not None
            and w.gpu_decoding() and not getattr(w, "_decode_user", False)]
    if len(over) <= cap:
        return None
    ranked = sorted(over, key=lambda w: (getattr(w, "_focus_seq", 0),
                                         id(w)), reverse=True)
    return ranked[0]


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

    def pick_engine(self, decoder: str, engines, select=None,
                    capacity=None, sysroot="/sys", pin=None):
        """Engine for the NEXT new window.

        R16-D: `auto` no longer round-robins over every GPU in the machine.
        The two R9700s carry the inference/serve workloads and must never
        receive video decode automatically, so `auto` now asks the decode
        plan: the integrated iGPU for the first `capacity` windows, CPU beyond
        it — and CPU for everyone when no integrated card qualifies.  A pinned
        decoder (`engine.decoder` != auto) still routes through the config
        path untouched: an explicit human choice always overrides the plan.

        R16-E §E.2: `pin` is `playback.vaapi_device`.  When set it decides
        WHICH card the plan treats as "the GPU" — including a dGPU, because
        the pin is an explicit human choice — while the plan still decides how
        many windows may use it.  Empty/None leaves §E.1's integrated-only
        rule in charge.
        """
        if (decoder or "auto") != "auto":
            if select is None:
                raise ValueError("select required for pinned decoders")
            return select(decoder, engines)
        from engine import DECODE_CAPACITY, decode_plan, engine_for_node, \
            select_engine
        cap = DECODE_CAPACITY if capacity is None else capacity
        if pin:
            plan = decode_plan(self.count + 1, cap, engines, sysroot,
                               gpu_engine=engine_for_node(pin, engines))
        else:
            plan = decode_plan(self.count + 1, cap, engines, sysroot)
        if not plan:
            return select_engine("cpu", engines)
        return plan[-1]
