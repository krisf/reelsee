"""wingeom — pure window-placement math (R10).

Two questions, both answerable with no display, no GTK and no clock, so
every rule is unit-testable (tests/test_wingeom.py):

  * WHERE does a cascade window open?  (§B.2) Same size as the source,
    shifted +offset when it still fits inside the SOURCE monitor's work
    area, else -offset when that fits, else clamped to the work area at
    the same size; `y` unchanged, clamped into the work area; the result
    ALWAYS overlaps the source.  Chained windows re-run this from the
    most recent window, so the flip-at-the-edge cascades back left.
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
