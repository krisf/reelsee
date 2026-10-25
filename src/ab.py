"""A/B section repeat — PLAN §5.4 contract, pure module (no gi, no mpv).

ABState holds (a, b, count) and validation rules; `apply(core)` pushes the
triple to mpv (engine-side looping — P0 proved runtime ab-loop-* works, so
REELSEE only drives and draws).  Persistence lives in ab.json per path (R-5)
and is handled by the AbStore; the serialization helpers here are pure.
"""
from __future__ import annotations

MIN_GAP_S = 0.15

REASON_NO_A = "Set A first"
REASON_GAP = "B must be at least 0.15s after A"
REASON_TIME = "needs a finite time >= 0"


def press_ab(state: "ABState", which: str, t) -> tuple[bool, bool, str]:
    """R5-E: one A/B keypress with clear-first semantics.

    Pressing A or B while a loop is ACTIVE (both endpoints set) FIRST
    clears the whole section — marks, drawn markers, persisted entry and
    the mpv ab-loop-a/b endpoints are the caller's to propagate — and
    only then applies the pressed mark.  A B press right after such a
    clear therefore hits the ordinary "Set A first" rule; no partial
    state ever results.

    Returns (applied, cleared, reason) — pure, no engine side effects.
    """
    cleared = state.active
    if cleared:
        state.clear()
    if which == "a":
        ok, reason = state.set_a(t)
    else:
        ok, reason = state.set_b(t)
    return ok, cleared, reason


def _time(t) -> float | None:
    """Coerce to float time or None (bools excluded, must be numeric)."""
    if isinstance(t, bool) or not isinstance(t, (int, float)):
        return None
    t = float(t)
    return t if t >= 0 and t == t and t != float("inf") else None


class ABState:
    def __init__(self, count="inf"):
        self.a: float | None = None
        self.b: float | None = None
        self.count = self._valid_count(count) or "inf"

    # -------------------------------------------------------- validation --
    @staticmethod
    def _valid_count(count) -> str | int | None:
        if count == "inf":
            return "inf"
        if isinstance(count, bool):
            return None
        if isinstance(count, int):
            return count if count >= 1 else None
        if isinstance(count, str) and count.isdigit():
            n = int(count)
            return n if n >= 1 else None
        return None

    def set_count(self, count) -> tuple[bool, str]:
        v = self._valid_count(count)
        if v is None:
            return False, "count must be inf or an integer >= 1"
        self.count = v
        return True, ""

    # ------------------------------------------------------------- edits --
    def set_a(self, t) -> tuple[bool, str]:
        t = _time(t)
        if t is None:
            return False, REASON_TIME
        self.a = t
        if self.b is not None and self.b <= t + MIN_GAP_S:
            self.b = None          # §5.4: A moved to/after B → B invalid
        return True, ""

    def set_b(self, t) -> tuple[bool, str]:
        if self.a is None:
            return False, REASON_NO_A
        t = _time(t)
        if t is None:
            return False, REASON_TIME
        if t < self.a + MIN_GAP_S:
            return False, REASON_GAP
        self.b = t
        return True, ""

    def clear(self) -> None:
        self.a = None
        self.b = None

    @property
    def active(self) -> bool:
        return self.a is not None and self.b is not None

    # ------------------------------------------------------------- mpv ---
    def apply(self, core) -> None:
        """Push state to the engine ('no' clears a mpv endpoint)."""
        core.set_property("ab-loop-a", self.a if self.a is not None else "no")
        core.set_property("ab-loop-b", self.b if self.b is not None else "no")
        core.set_property("ab-loop-count",
                          "no" if self.count == "inf" else self.count)

    # ------------------------------------------------------ persistence --
    def to_dict(self) -> dict:
        return {"a": self.a, "b": self.b, "count": self.count}

    @classmethod
    def from_dict(cls, data) -> "ABState":
        st = cls()
        if isinstance(data, dict):
            st.a = _time(data.get("a")) if data.get("a") is not None else None
            st.b = _time(data.get("b")) if data.get("b") is not None else None
            if st.a is not None and st.b is not None and st.b < st.a + MIN_GAP_S:
                st.b = None                     # corrupt/persisted-stale
            c = cls._valid_count(data.get("count", "inf"))
            st.count = c if c is not None else "inf"
        return st
