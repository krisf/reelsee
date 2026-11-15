"""wingeom — pure window-placement math (R10).

Two questions, both answerable with no display, no GTK and no clock, so
every rule is unit-testable (tests/test_wingeom.py):

  * WHERE does a cascade window open?  (§B.2, R20 §A1) Same size as the
    source, moved `offset` px TOWARD THE CENTRE of the source monitor's
    work area (centerward_place — never past the centre, never resized,
    never fully off-screen).  cascade_place (right/left preference) is
    still here, and still unit-tested; the window simply no longer calls
    it.  Chained windows re-run this from the most recent window.
  * WHERE does a remembered rect reopen?  (§A, R20 §0.1) Verbatim — the
    exact coords+dims it was closed at — UNLESS at least 2 of its corners
    are off the current SCREEN (corners_offscreen); only then may it be
    repositioned, and then clamp_into_workarea does that.
  * is a remembered rect REAL?  (§A) A stored rect is data from disk —
    it must parse to ints with a positive size before it may move a
    window, and anything else (missing, corrupt, oversized, bool-as-
    number, negative size) is silently "no remembered geometry".

A rect is (x, y, w, h) top-left + size in root/desktop pixels; a work
area is the same shape.  Inputs are permissive (rect dicts, tuples,
Gdk.Rectangle-ish objects with x/y/width/height) and outputs are plain
int tuples — the window/registry side never re-derives or re-rounds.
"""
from __future__ import annotations

MAX_DIM = 16384            # a "window" wider/taller than this is corrupt data
MIN_W, MIN_H = 32, 32      # GTK's own content minimums are above these


def _int(v) -> int | None:
    """Plain int from whatever JSON/GDK handed over; bool is NOT a number
    here (True would masquerade as 1 — the aspect module's rule)."""
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, int):
        return int(v)
    if isinstance(v, float):
        return int(v) if float(v) == int(v) else None
    if isinstance(v, str):
        try:
            return int(v.strip())
        except ValueError:
            return None
    return None


def norm_rect(v) -> tuple[int, int, int, int] | None:
    """Anything shaped like a rect → (x, y, w, h) ints, or None.

    Accepts a dict with x/y/w/h keys, a 4-sequence, or an object with
    x/y/width/height attributes (Gdk.Rectangle).  NO validation of the
    numbers here beyond "they are ints" — use valid_rect for that.
    """
    if v is None:
        return None
    if isinstance(v, dict):
        vals = (v.get("x"), v.get("y"), v.get("w", v.get("width")),
                v.get("h", v.get("height")))
    elif isinstance(v, (tuple, list)) and len(v) == 4:
        vals = tuple(v)
    else:
        try:
            vals = (v.x, v.y, v.width, v.height)
        except AttributeError:
            return None
    x, y, w, h = (_int(vals[0]), _int(vals[1]), _int(vals[2]), _int(vals[3]))
    if x is None or y is None or w is None or h is None:
        return None
    return (x, y, w, h)


def valid_rect(v) -> tuple[int, int, int, int] | None:
    """A rect that may be TRUSTED to place a window: sane positive size.

    None (never an exception) for anything else — this is the gate for
    data read back from the state file (§A.3 "corrupt file ⇒ default
    behaviour, no traceback").
    """
    r = norm_rect(v)
    if r is None:
        return None
    _x, _y, w, h = r
    if not (MIN_W <= w <= MAX_DIM and MIN_H <= h <= MAX_DIM):
        return None
    # absurd coordinates are treated as corrupt too (a 9-digit x would
    # "restore" the window off the face of the desktop)
    if abs(r[0]) > MAX_DIM * 4 or abs(r[1]) > MAX_DIM * 4:
        return None
    return r


def valid_monitor(v) -> int | None:
    """Monitor index as stored data: non-negative int or None ('unknown')."""
    m = _int(v)
    return m if m is not None and m >= 0 else None


def overlap_area(a, b) -> int:
    """Pixel area shared by two rects (0 when they merely touch or miss)."""
    ra, rb = norm_rect(a), norm_rect(b)
    if ra is None or rb is None:
        return 0
    ax, ay, aw, ah = ra
    bx, by, bw, bh = rb
    ow = min(ax + aw, bx + bw) - max(ax, bx)
    oh = min(ay + ah, by + bh) - max(ay, by)
    return ow * oh if ow > 0 and oh > 0 else 0


def overlaps(a, b) -> bool:
    return overlap_area(a, b) > 0


def work_areas(monitors) -> list[tuple[int, int, int, int]]:
    """Monitors (objects with get_workarea() or plain rects) → work areas.

    Single monitor, two monitors, garbage entries: the output is exactly
    the usable areas, in input order.
    """
    out: list[tuple[int, int, int, int]] = []
    for m in monitors or ():
        wa = m
        get = getattr(m, "get_workarea", None)
        if callable(get):
            try:
                wa = get()
            except Exception:                     # noqa: BLE001
                continue
        r = norm_rect(wa)
        if r is not None and r[2] > 0 and r[3] > 0:
            out.append(r)
    return out


def pick_monitor(rect, areas: list):
    """Index of the monitor WORK AREA `rect` lives on — the one the
    cascade rules must use (§B.2), chosen by shared area.

    A window exactly on a boundary splits its area between two monitors;
    the one holding more of it wins, ties break to the lower index.  A
    rect on no monitor at all (layout changed, phantom monitor) falls
    back to the CLOSEST by center distance so a restore still lands on
    a real screen.  Empty list → None.
    """
    r = norm_rect(rect)
    if r is None or not areas:
        return None
    best_i, best = None, None
    for i, a in enumerate(areas):
        a = norm_rect(a)
        if a is None:
            continue
        area = overlap_area(r, a)
        key = (-area, _center_dist(r, a), i)      # min = biggest overlap first
        if best is None or key < best:
            best, best_i = key, i
    return best_i


def _center_dist(r, a) -> float:
    rx, ry = r[0] + r[2] / 2.0, r[1] + r[3] / 2.0
    ax, ay = a[0] + a[2] / 2.0, a[1] + a[3] / 2.0
    return ((rx - ax) ** 2 + (ry - ay) ** 2) ** 0.5


def _clamp(v: int, lo: int, hi: int) -> int:
    return min(max(v, lo), hi)


def _area_set(v) -> list | None:
    """A `work_area` argument → list of normalised rects, None for garbage.

    Accepts ONE rect (tuple/dict/rect-ish object) or a LIST of them — the
    list is the multi-monitor case, where the rects together are "the
    current screen(s)": a corner on ANY of them is on a screen.  Garbage
    entries are dropped (work_areas' rule); nothing usable left ⇒ None."""
    if v is None:
        return None
    multi = (isinstance(v, (list, tuple)) and len(v) > 0
             and not isinstance(v[0], (int, float, str)))
    if multi:
        areas = work_areas(v)
        return areas or None
    r = norm_rect(v)
    if r is None or r[2] <= 0 or r[3] <= 0:
        return None
    return [r]


def corners_offscreen(rect, work_area) -> int | None:
    """How many of `rect`'s four corners fall OUTSIDE `work_area` (R20 §A1).

    0..4, or None when either input is not a usable rect (the caller then
    takes its safe default — reposition).  A corner exactly ON an edge is
    ON screen (the bounds are inclusive: a window flush with the screen
    edge is not hanging off it).  `work_area` may be one rect or a LIST of
    monitor rects; with a list a corner counts as off only when it is on
    NO monitor.

    Geometry worth knowing (tests/test_wingeom_r20.py proves it): against
    ONE convex area an axis-aligned rect can only score 0, 2, 3 or 4 — a
    hang-off-edge always takes BOTH corners on that side with it.  So
    "exactly one corner off" needs a non-convex screen (the notch of an
    L-shaped two-head layout), and the caller measures against the SCREEN,
    not the work area, for the operator's case to score 0: a window closed
    with a corner under the panel has that corner ON the screen.
    """
    r = norm_rect(rect)
    if r is None or r[2] <= 0 or r[3] <= 0:
        return None
    areas = _area_set(work_area)
    if areas is None:
        return None
    x, y, w, h = r
    n = 0
    for cx, cy in ((x, y), (x + w, y), (x, y + h), (x + w, y + h)):
        if not any(a[0] <= cx <= a[0] + a[2] and a[1] <= cy <= a[1] + a[3]
                   for a in areas):
            n += 1
    return n


def _toward(pos: int, size: int, center2: int, off: int) -> int:
    """`pos` moved `off` px toward the work-area centre along one axis,
    never past it.  `center2` is the doubled centre (2*wa_pos + wa_size);
    the doubled window centre is 2*pos + size, so the comparison and the
    distance stay in exact integers.  A source already centred on this
    axis does not move."""
    p2 = 2 * pos + size
    if p2 == center2 or off <= 0:
        return pos
    step = min(off, abs(center2 - p2) // 2)       # //2 ⇒ never overshoots
    return pos + step if p2 < center2 else pos - step


def centerward_place(source, work_area,
                     offset) -> tuple[int, int, int, int] | None:
    """Where the NEXT window opens: `offset` px TOWARD THE CENTRE of the
    work area (R20 §A1 — replaces cascade_place at the call site).

    Same hard rules as cascade_place: the size NEVER changes (that is the
    whole user request), a source that is not a rect ⇒ None, an offset
    that is not a number behaves as 0, offset 0 ⇒ the identical position,
    and a `work_area` that is a LIST of monitor work areas is reduced to
    the one containing the source (pick_monitor) — the source monitor is
    the reference frame, never "monitor 0".

    The difference: instead of "prefer right, flip left", each axis moves
    toward the work-area centre by `offset` and stops at the centre (a
    source already centred on an axis does not move on that axis); the
    result is then clamped into the work area at the same size, so a
    window can never be placed fully off-screen.
    """
    r = norm_rect(source)
    if r is None:
        return None
    areas = _area_set(work_area)
    if areas is None:
        return None
    if len(areas) > 1:
        idx = pick_monitor(r, areas)
        wa = areas[idx] if idx is not None and idx < len(areas) else None
    else:
        wa = areas[0]
    if wa is None:
        return None
    off = _int(offset)
    if off is None or off < 0:
        off = 0
    if off == 0:
        return r                          # "exactly on top": identical
    x, y, w, h = r
    wx, wy, ww, wh = wa
    nx = _toward(x, w, 2 * wx + ww, off)
    ny = _toward(y, h, 2 * wy + wh, off)
    lo, hi = min(wx, wx + ww - w), max(wx, wx + ww - w)
    nx = _clamp(nx, lo, hi)
    lo, hi = min(wy, wy + wh - h), max(wy, wy + wh - h)
    ny = _clamp(ny, lo, hi)
    return (int(nx), int(ny), w, h)


def clamp_into_workarea(rect, work_area):
    """§A.3: a remembered rect brought back inside `work_area` WITHOUT
    ever growing — a window larger than the (possibly changed) monitor is
    shrunk to the work area, never "restored oversized"; position is then
    clamped so no part hangs off-screen.  Size stays exact whenever it
    fits.  None when either input is not a rect (caller defaults)."""
    r = valid_rect(rect)                          # garbage sizes ⇒ None,
    wa = valid_rect(work_area)                    # never silently repaired
    if r is None or wa is None:
        return None
    x, y, w, h = r
    wx, wy, ww, wh = wa
    w = min(w, ww)                                # never oversized
    h = min(h, wh)
    w = max(w, MIN_W)
    h = max(h, MIN_H)
    x = _clamp(x, wx, max(wx, wx + ww - w))
    y = _clamp(y, wy, max(wy, wy + wh - h))
    return (x, y, w, h)


def cascade_place(source, work_area, offset) -> tuple[int, int, int, int] | None:
    """Where the NEXT window opens, next to the source one (§B.2).

    `work_area` is the source monitor's work area, or the LIST of every
    monitor's work area — in that case the one that actually contains the
    source is picked first (pick_monitor), which is what makes the two-
    monitor rule testable: the source monitor is the reference frame,
    never "monitor 0".

    The rules, in order:
      * size NEVER changes — the new window is the source's exact pixel
        size (that is the whole user request);
      * prefer right:  x + w + offset <= work_right        →  x + offset
      * else left:     x - offset >= work_left             →  x - offset
      * else clamp x to [work_left, work_right - w] (the whole window
        inside the work area at the same size);
      * y unchanged, clamped into the work area;
      * the result MUST overlap the source: same size ⇒ overlap ⇔
        |dx| < w, so the horizontal shift is capped at w-1 px.  With the
        UI range (0–200 px) against any real window width the cap is
        inert; it is what keeps an absurd offset honest to the spec.
      * offset == 0 ⇒ the identical position (exactly on top).

    None for a source that is not a rect; an offset that is not a number
    behaves as 0.
    """
    r = norm_rect(source)
    if r is None:
        return None
    multi = (isinstance(work_area, (list, tuple)) and len(work_area) > 0
             and not isinstance(work_area[0], (int, float, str)))
    if multi:
        areas = work_areas(work_area)
        idx = pick_monitor(r, areas)
        wa = areas[idx] if idx is not None and idx < len(areas) else None
    else:
        wa = norm_rect(work_area)
    if wa is None:
        return None
    off = _int(offset)
    if off is None or off < 0:
        off = 0
    x, y, w, h = r
    wx, wy, ww, wh = wa
    left, right = wx, wx + ww
    top, bottom = wy, wy + wh
    if off == 0:
        nx = x
    elif x + w + off <= right:
        nx = x + off
    elif x - off >= left:
        nx = x - off
    else:
        lo, hi = min(left, right - w), max(left, right - w)
        nx = _clamp(x + off, lo, hi)
    # MUST overlap the source (brief §B.2): same-size rects overlap iff
    # |dx| < w — cap the shift at w-1 px, moving TOWARD the source, which
    # keeps the result between x and the clamped target (both on-screen).
    cap = max(w - 1, 0)
    if nx - x > cap:
        nx = x + cap
    elif x - nx > cap:
        nx = x - cap
    ny = _clamp(y, min(top, bottom - h), max(top, bottom - h))
    return (int(nx), int(ny), w, h)


# ------------------------------------------------------- R28-D: floor
#: The smallest rect worth REMEMBERING.  This is NOT the "is the stored
#: number parseable" gate (valid_rect, 32 px) — it is the "would a window
#: opened at this size be a window at all" floor, and it is deliberately
#: the app's own usable-window floor, ``aspect.MIN_W``/``MIN_H`` (320x180,
#: the size below which the R8-B correction itself refuses to go).  The
#: operator's symptom "I open the app and there is no window" was this
#: file holding 102x102/100x100 — an unusable rect remembered by an
#: earlier WM fight.  Kept here as plain literals (not an ``import
#: aspect``: wingeom stays import-free) and PINNED equal to aspect's
#: values by tests/test_geomfloor_r28.py — the day they drift, the test
#: fails, which is exactly the silent disagreement this project already
#: refuses to have twice.
FLOOR_W, FLOOR_H = 320, 180

#: What repair_tiny_rect assumes when nobody tells it the work area (the
#: store loads before any display exists).  The downstream open path
#: clamps the repaired rect into the REAL monitor anyway.
DEFAULT_WORK = (1920, 1080)


def below_floor(rect, floor_w: int = FLOOR_W, floor_h: int = FLOOR_H) -> bool:
    """True when this (parseable) rect is too small to ever be a window.

    Invalid input is NOT "below floor" — it is simply not a rect (the
    caller's valid_rect gate already handles it; do not merge two error
    classes into one)."""
    r = valid_rect(rect)
    if r is None:
        return False
    return r[2] < floor_w or r[3] < floor_h


def repair_tiny_rect(rect, work=None,
                     floor_w: int = FLOOR_W, floor_h: int = FLOOR_H):
    """A SANE 16:9 replacement for a remembered rect below the floor.

    R28-D: an already-broken ``window.json`` (102x102 — the operator's
    file) must heal itself on next start instead of re-opening the hole.
    The repair is the default opening shape: a 16:9 rect at half the work
    area, but never below the floor and never bigger than the work area;
    position centered.  Returns None when the rect is fine (nothing to
    repair) or unparseable (the caller's default behaviour stands); the
    caller distinguishes "repaired" from "untouched" by asking
    ``below_floor`` first, never by guessing from the result.
    """
    r = valid_rect(rect)
    if r is None or not below_floor(r, floor_w, floor_h):
        return None
    ww, wh = (work if work is not None else
              (DEFAULT_WORK[0], DEFAULT_WORK[1]))
    ww, wh = _int(ww), _int(wh)
    if not ww or ww <= 0:
        ww = DEFAULT_WORK[0]
    if not wh or wh <= 0:
        wh = DEFAULT_WORK[1]
    w, h = min(max(ww // 2, floor_w), ww), max(floor_h, min(wh // 2, wh))
    # 16:9 from that box (shrink-only, so both bars stay true):
    if w * 9 > h * 16:
        w = h * 16 // 9
    else:
        h = w * 9 // 16
    w, h = max(w, floor_w), max(h, floor_h)
    x = _clamp(r[0], 0, max(0, ww - w))
    y = _clamp(r[1], 0, max(0, wh - h))
    return (int(x), int(y), int(w), int(h))
