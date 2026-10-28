"""aspect — pure fit/decision math for aspect-locked window resizing (R8-B).

Why a module of pure functions at all: GTK 4 deleted geometry hints
(``Gtk.Window.set_geometry_hints`` and ``Gdk.Geometry`` do not exist in this
4.22.4 build — operator-proven, do not re-litigate), so an aspect-locked
window cannot be *declared*; it has to be *corrected* after the fact.  The
correction is driven by ``Gdk.Surface::layout`` (fires as
``(surface, width, height)``) and applied with ``set_default_size()`` on the
mapped window (measured to really resize).  Signals, debounce, guards and
timers belong to the window; every decision — "what is the aspect", "what
size should this box be", "is a correction even needed" — lives here as a
pure function with no GTK, no mpv and no clock, so it is unit-testable with
no display.

One rule the whole round hangs on: the aspect comes from what the player is
ACTUALLY displaying, i.e. the observed mpv properties ``dwidth``/``dheight``
(the display size the output is using, which is what a rotated or vertical
file actually occupies on screen), never the configured ``video.aspect`` and
never a cached value.  Measured on this build, ``video-rotate`` moves neither
``dwidth`` nor ``dheight`` (a 1920x1080 file reports 1920x1080 at every
rotation), so the observed display size is used exactly as reported — no
compensation for a rotation the player itself does not report.
Unknown or zero ⇒ there is nothing to lock to, and resizing stays free.
"""
from __future__ import annotations

#: Resize-burst debounce (§R8-B.3): correct ~80 ms after a burst starts so a
#: fast drag does not storm the window system, but without ever postponing
#: indefinitely (the timer is armed once per burst and is NOT re-armed by the
#: events that follow — see window._on_surface_layout).
DEBOUNCE_MS = 80

#: How long our OWN applied size swallows incoming layout signals.  The
#: re-entrancy guard: a correction must never be able to retrigger itself.
APPLY_GUARD_MS = 150

#: A correction that would move the window by at most this many pixels is not
#: a correction (§R8-B.2: "if the correction is <= 1 px, do nothing").  It is
#: also what makes our own echo a no-op even if it arrives after the guard.
EPS_PX = 1

#: Sane floor for a box we are willing to fit into (§R8-B.2).
MIN_W, MIN_H = 320, 180

#: A newly loaded video is sized inside this fraction of the monitor's work
#: area (§R8-B.4), so 1080x1920 opens tall and 3840x2160 opens wide without
#: either one covering the screen.
WORKAREA_FRACTION = 0.8

#: Relative aspect deviation above which a window is considered "not the
#: video's shape".  Only the new-file decision uses this: a window that
#: already shows the video's aspect is never moved just because a file
#: loaded (960x540 playing a 16:9 clip must stay exactly where it is).
MISMATCH_REL = 0.05


def _round(x: float) -> int:
    """Half-up rounding.

    ``round()`` is banker's rounding, and ``round(562.5)`` is 562 — but a
    1000x1000 box holding 16:9 video must come out 1000x**563**.  Sizes are
    always positive here, so adding a half and truncating is the right
    "round to nearest, .5 up".
    """
    return int(x + 0.5)


def _num(v) -> float | None:
    """Plain number from whatever mpv/GLib handed over, or None.

    mpv INT64 properties arrive as Python ints through the snapshot; a stub
    core or a JSON-ish config may hand a str.  bool is excluded: it is an int
    subclass whose 0/1 would masquerade as a dimension.
    """
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v.strip())
        except ValueError:
            return None
    return None


def aspect_of(snap) -> float | None:
    """The aspect the player is displaying, or None when there is none.

    ``dwidth``/``dheight`` — the display dimensions the output reports — are
    the source of truth; ``width``/``height`` (coded size) are the documented
    fallback while the display size has not arrived yet (§R8-B.1).  Anything
    else — no file, audio-only, properties not yet observed — returns None,
    and None means "resize freely", never "assume 16:9".
    """
    if not snap:
        return None
    for wk, hk in (("dwidth", "dheight"), ("width", "height")):
        w, h = _num(snap.get(wk)), _num(snap.get(hk))
        if w and h and w > 0 and h > 0:
            return w / h
    return None


def aspect_lock_active(lock_setting, crop_on, fullscreen, maximized) -> bool:
    """THE aspect-lock eligibility decision (R11-C), as one pure function.

    Round 8 made the window TRACK the video's aspect; round 11 adds the
    one state that must override it: CROP TO WINDOW.  A crop is literally
    "the box may be any shape, the video fills it" — locking the box to
    the video's aspect while cropping would undo the crop's whole point —
    so crop-on SUSPENDS the lock, and crop-off re-arms it.  Turning the
    setting itself while crop is on must not fight the suspension: this
    single function is the only place eligibility is decided, so both the
    resize path and the lock toggle read the same truth.

    Fullscreen and maximized geometries are not ours to correct (round 8
    `frozen` truth, folded in so the whole rule lives HERE):

        lock_setting AND NOT crop_on AND NOT fullscreen AND NOT maximized
    """
    return bool(lock_setting) and not crop_on and not fullscreen \
        and not maximized


def fit_aspect(w, h, aspect, max_w=None, max_h=None,
               min_w: int = MIN_W, min_h: int = MIN_H) -> tuple[int, int]:
    """Box ``(w, h)`` → the aspect-faithful box for that box.  ALWAYS a
    ``(w, h)`` tuple — never None — because "unknown aspect" has exactly one
    meaning here: leave the box as the user left it, resizing stays free.

    The box is the *request* (what the user dragged to, or the work area a
    new video is fitted into), ``aspect`` is ``dwidth / dheight``:

    1. unknown / zero / negative aspect ⇒ the box comes back unchanged;
    2. the box is raised to the sane floor (``min_w`` x ``min_h``) — §R8-B.2
       asks for the minimum "before clamping to the aspect";
    3. the box is capped by ``max_w``/``max_h`` when given (the monitor work
       area, so we never exceed it — a work area smaller than the floor
       wins, since "never exceed the work area" is the hard rule);
    4. inside that box, exactly one dimension is *reduced* so the result
       matches the aspect: too wide (``w / h > A``) narrows the width to
       ``round(h * A)``, otherwise the height drops to ``round(w / A)``.
       The box is never widened or tallened: a drag always shrinks into
       itself, which is what keeps a resize from feeling like a tug.

    Consequences worth pinning in tests: a 1000x1000 box at 16:9 →
    (1000, 563); a 1000x400 box at 9:16 → (225, 400) — a vertical fit inside
    a wide drag is legitimately narrower than the 320 px floor, because the
    floor guards the *box*, and the aspect is the thing being honoured.
    """
    a = _num(aspect)
    bw, bh = _num(w), _num(h)
    if bw is None or bh is None or bw <= 0 or bh <= 0:
        return (int(bw or 0), int(bh or 0))
    if a is None or a <= 0:
        return (int(bw), int(bh))           # nothing to lock to: stay free
    bw, bh = max(bw, min_w), max(bh, min_h)
    if max_w is not None:
        bw = min(bw, max_w)
    if max_h is not None:
        bh = min(bh, max_h)
    if bw / bh > a:
        bw = _round(bh * a)
    else:
        bh = _round(bw / a)
    return (max(1, int(bw)), max(1, int(bh)))


def fit_in_workarea(work_w, work_h, aspect,
                    fraction: float = WORKAREA_FRACTION) -> tuple[int, int]:
    """A new video's opening size: its own aspect inside ``fraction`` of the
    monitor work area (§R8-B.4).

    Returns ``(0, 0)`` — "there is nothing to size to" — when either the
    aspect or the work area is unknown, so a caller that forgot to check
    first still cannot produce a bogus resize (``set_default_size(0, 0)`` is
    a GTK no-op).
    """
    ww, wh = _num(work_w), _num(work_h)
    a = _num(aspect)
    if ww is None or wh is None or ww <= 0 or wh <= 0:
        return (0, 0)
    if a is None or a <= 0:
        return (0, 0)
    f = fraction if fraction and fraction > 0 else 1.0
    return fit_aspect(_round(ww * f), _round(wh * f), a)


def mismatch(w, h, aspect, rel: float = MISMATCH_REL) -> bool:
    """True when box ``(w, h)`` is NOT the video's shape (§R8-B.4 gate: only
    then may loading a file move the window).  Unknown aspect ⇒ False.
    """
    a = _num(aspect)
    bw, bh = _num(w), _num(h)
    if a is None or a <= 0 or bw is None or bh is None or bw <= 0 or bh <= 0:
        return False
    return abs(bw / bh - a) / a > rel


def needs_correction(w, h, aspect, eps: int = EPS_PX, max_w=None, max_h=None,
                     min_w: int = MIN_W, min_h: int = MIN_H) -> bool:
    """Does this box demand a correction at all?

    Exactly the §R8-B.2 rule: compare with what ``fit_aspect`` would hand
    back and require more than ``eps`` pixels of movement — a 1 px (or zero
    px) correction is a no-op.  Because it asks the same function the
    correction path asks, "needs no correction" and "would be a no-op" can
    never disagree, which is what stops an echo from looking like a drag.
    """
    fit = fit_aspect(w, h, aspect, max_w=max_w, max_h=max_h,
                     min_w=min_w, min_h=min_h)
    a = _num(aspect)
    if a is None or a <= 0:
        return False
    return max(abs(fit[0] - int(w)), abs(fit[1] - int(h))) > eps


# ---------------------------------------------------------------- PAspect
#: Ceiling for the numerator/denominator we are willing to publish in
#: WM_NORMAL_HINTS.  The fields are 32-bit and a correct WM uses them
#: exactly, but ratios like 3840x2160 reduce to 16:9 anyway; anything whose
#: exact reduction exceeds this many pixels of "scale" is a non-obvious
#: ratio (a weird dwidth from a filter or a rotate) and belongs in a
#: continued-fraction convergent instead — WMs and old tools have been
#: observed mis-multiplying huge aspect words.
PASPECT_MAX_DEN = 1000

#: Pixel error a published ratio is allowed to introduce, measured against
#: a ``PASPECT_REF_PX``-tall window.  1 px at 4K: 16:9 and 9:16 hold to
#: within one pixel of the true display ratio even at the largest window
#: this app sizes, so the post-hoc §R8-B correction never has anything to
#: say once a WM has honoured the hints.
PASPECT_TOL_PX = 1.0
PASPECT_REF_PX = 4096


def paspect_pair(dwidth, dheight, max_den: int = PASPECT_MAX_DEN,
                 tol_px: float = PASPECT_TOL_PX,
                 ref_px: float = PASPECT_REF_PX) -> tuple[int, int] | None:
    """``(num, den)`` for the X11 PAspect words, or None = nothing to lock.

    The pair must express ``dwidth / dheight`` closely enough that a WM
    honouring the hints needs no post-correction: exactly, when the
    greatest-common-divisor reduction is small enough (1920x1080 →
    ``(16, 9)``, 270x480 → ``(9, 16)``); otherwise the best
    continued-fraction convergent whose numerator and denominator are both
    within ``max_den`` AND whose error stays under ``tol_px`` pixels on a
    ``ref_px``-tall window.  A ratio no small pair can promise that close
    is published EXACT and unreduced (a 32-bit field swallows it, and
    exact-beats-tidy) — never None, unless the size itself is unknown.
    """
    w, h = _num(dwidth), _num(dheight)
    if w is None or h is None or w <= 0 or h <= 0:
        return None
    wi, hi = int(w), int(h)
    a, b = wi, hi
    while b:                                   # Euclid; exact reduction
        a, b = b, a % b
    num, den = max(1, wi // a), max(1, hi // a)
    if num <= max_den and den <= max_den:
        return (num, den)
    target = wi / hi
    # Continued-fraction convergents, ascending denominators; keep the last
    # one that is both small enough and accurate enough.
    best = None
    h0, k0, h1, k1 = 0, 1, 1, 0
    x = target
    for _ in range(64):                        # CF of a double terminates
        ai = int(x)
        h2, k2 = ai * h1 + h0, ai * k1 + k0
        if k2 > max_den or h2 > max_den:
            break
        if abs(h2 / k2 - target) * ref_px <= tol_px:
            best = (h2, k2)
        h0, k0, h1, k1 = h1, k1, h2, k2
        frac = x - ai
        if frac <= 1e-12:
            break
        x = 1.0 / frac
    if best is not None:
        return best
    return (wi, hi)                            # exact wins over tidy
