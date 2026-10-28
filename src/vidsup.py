"""R16-B: hidden-video suppression policy (pure — no gi, no mpv, no clock).

A minimized/unmapped window shows nobody a single frame, yet mpv keeps
decoding a 4K stream at full rate to feed a render path nobody looks at.
The policy here is the whole decision — "what must mpv's `vid` be right
now, and did *we* just change it?" — so it is unit-testable as a truth
table and the window keeps only plumbing (a `notify::mapped` handler and
one property write).

The three facts the table encodes:

* the USER's own video choice (`auto`, `no`, or a track id) is remembered
  separately from the suppression's own `no`; a remap restores THAT, never
  a bland `auto` that silently picks a different track;
* a user who already turned video off (`playback.video_off`) had nothing
  running, so the unmap writes nothing and the remap must NOT turn video
  back on — the user's choice outranks the power saving;
* with the setting off, nothing is ever written (the pre-R16 behaviour).

Audio is not this module's business: it never returns anything but a `vid`
value, and the window never writes `mute` or `pause` from here.
"""
from __future__ import annotations

#: what mpv's `vid` property means when the user has not chosen a track
AUTO = "auto"


class VidSuppression:
    """The state machine behind `playback.suppress_video_when_hidden`.

    Every method returns the value to write to `vid`, or ``None`` for
    "write nothing" — callers must not second-guess the answer, which is
    exactly how a second writer would creep in.
    """

    def __init__(self, enabled: bool = True, user_vid: str = AUTO,
                 hidden: bool = False):
        self.enabled = bool(enabled)
        self._user = norm_vid(user_vid)
        self.hidden = bool(hidden)   # surface unmapped right now
        self._held = False           # WE wrote the current "no"

    # ------------------------------------------------------------- reads
    @property
    def user_vid(self) -> str:
        """The user's own choice, suppression aside."""
        return self._user

    def effective(self) -> str:
        """What `vid` must be on the live handle RIGHT NOW."""
        if self.hidden and self.enabled:
            return "no"
        return self._user

    @property
    def suppressed(self) -> bool:
        """True only while WE hold an outstanding `vid=no`."""
        return self._held

    # ------------------------------------------------- transitions (writes)
    def unmap(self) -> str | None:
        """The surface went unmapped (minimized / iconified / hidden)."""
        if self.hidden:
            return None                    # already hidden: no second write
        self.hidden = True
        if not self.enabled or self._user == "no":
            # Setting off, or video was already off for the USER's reason:
            # nothing to stop, and nothing to "restore" later.
            self._held = False
            return None
        self._held = True
        return "no"

    def remap(self) -> str | None:
        """The surface is visible again."""
        if not self.hidden:
            return None                    # never hidden: nothing to restore
        self.hidden = False
        if not self._held:
            return None                    # we never wrote anything
        self._held = False
        return self._user

    def sync(self, hidden: bool) -> str | None:
        """Reconcile with an OBSERVED hidden state.

        The trigger is not a single reliable event (see
        `ReelseeWindow._surface_hidden`), so the window re-states the truth
        on every pump tick as well as on `notify::mapped`.  A repeated
        observation of the same state writes NOTHING — `unmap`/`remap` are
        transition-only, which is what keeps a 100 ms poll from hammering
        mpv with `vid=no`."""
        return self.unmap() if hidden else self.remap()

    def user_wants(self, value) -> str:
        """The user (a track menu, `video_off`, a file load) chooses `vid`.
        Returns the value to write — the choice itself, unless a suppression
        is holding the decoder off, in which case the EFFECTIVE value stays
        `no` while the choice is recorded for the remap."""
        self._user = norm_vid(value)
        return self.effective()

    def set_enabled(self, flag) -> str | None:
        """The setting flipped.  Turning it OFF while hidden must hand the
        decoder back immediately; turning it ON while hidden must stop it."""
        flag = bool(flag)
        if flag == self.enabled:
            return None
        self.enabled = flag
        if not self.hidden:
            return None                    # nothing observable changes
        if flag:
            if self._user == "no":
                return None                # already off; nothing to write
            self._held = True
            return "no"
        if not self._held:
            return None                    # we never wrote anything
        self._held = False
        return self._user


def norm_vid(value) -> str:
    """mpv's `vid` is a string property; normalise the shapes callers use."""
    if value is None:
        return AUTO
    s = str(value).strip()
    return s or AUTO
