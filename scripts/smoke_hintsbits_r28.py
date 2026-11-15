#!/usr/bin/python3
"""R28 Xvfb gate: what WM_NORMAL_HINTS looks like ON THE SERVER.

R28 §4: the constant fix alone must be proven where it ships — a REAL
GTK window under headless-smoke.sh, x11pos.set_aspect_ratio called for
real, and the property read back FROM THE X SERVER with python-xlib (the
bytes a real WM would read; Xvfb has no WM, so this gate audits the
publication, not the clamping — the clamping half is the operator's :0
repro, scripts/aspect_ratchet.py).

The pre-R28 publication was PAspect-at-bit-32, i.e. PMaxSize over max
words 0,0 — "never larger than 0x0" — which Mutter answered with a
content-minimum clamp (the user's 102x102 "missing window").  This gate
can no longer pass with that array, and can no longer pass vacuously:
every read below is a live get_full_property, the no-churn phase counts
PROPERTYNOTIFY events at a second client, and the seed phases plant the
exact legacy bytes the old code shipped and demand they come back healed.

Checks: window_mapped, hints_present, publish_bit_128, pin_pair_16_9,
no_dead_max_publication, foreign_words_survive, no_churn_on_reapply,
dead_max_healed_on_publish, live_pmax_survives_publish,
clear_keeps_live_pmax, clear_writes_nothing_twice, window_alive, run_rc.
"""
import time

from smoke_harness import check, finish

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gtk, GLib  # noqa: E402

import x11pos  # noqa: E402
from Xlib import X  # noqa: E402
from Xlib.display import Display  # noqa: E402

# ICCCM §4.1.2.4, spelled out here so the auditor never borrows the
# auditee's table (the exact discipline that caught R28 in its own copy).
ICCCM = {"US_POSITION": 1, "US_SIZE": 2, "PPOSITION": 4, "PSIZE": 8,
         "PMIN_SIZE": 16, "PMAX_SIZE": 32, "PRESIZE_INC": 64,
         "PASPECT": 128, "PBASE_SIZE": 256, "PWIN_GRAVITY": 512}
_PASPECT = ICCCM["PASPECT"]
_PMAX = ICCCM["PMAX_SIZE"]

state = {}
xd = Display()
ATOM = xd.intern_atom("WM_NORMAL_HINTS")
# ICCCM: the property WM_NORMAL_HINTS carries TYPE WM_SIZE_HINTS — GDK
# writes it that way, and XGetWMNormalHints (what every real WM, and
# x11pos, use) matches on that type.  Seeds below must be well-formed or
# they audit a shape no client will ever publish.
ATOM_TYPE = xd.intern_atom("WM_SIZE_HINTS")


def pump(ms):
    ctx = GLib.MainContext.default()
    end = time.monotonic() + ms / 1000.0
    while time.monotonic() < end:
        ctx.iteration(False)


def server_arr():
    """THE SERVER'S WM_NORMAL_HINTS (a real WM's view), 18 words or None."""
    xd.sync()
    p = state["xw"].get_full_property(ATOM, X.AnyPropertyType)
    if p is None or p.format != 32 or p.value is None:
        return None
    arr = [int(v) & 0xFFFFFFFF for v in p.value]
    if len(arr) < 18:
        arr += [0] * (18 - len(arr))
    return arr[:18]


def poll_arr(pred, ms=4000):
    end = time.monotonic() + ms / 1000.0
    while time.monotonic() < end:
        arr = server_arr()
        if arr is not None and pred(arr):
            return arr
        pump(15)
    return None


def seed(arr):
    state["xw"].change_property(ATOM, ATOM_TYPE, 32, list(arr),
                                X.PropModeReplace)
    xd.sync()


def snap(tag):
    print(f"DIAG:{tag}={server_arr()}", flush=True)


def drain_notices():
    n = 0
    while xd.pending_events():
        ev = xd.next_event()
        if getattr(ev, "type", None) == X.PropertyNotify \
                and getattr(ev, "atom", None) == ATOM:
            n += 1
    return n


def count_notices(ms):
    pump(ms)
    return drain_notices()


def dead_max(arr):
    return bool(arr) and (arr[0] & _PMAX) and arr[7] == 0 and arr[8] == 0


def main():
    win = Gtk.Window()
    win.set_title("REELSEE-R28-HINTSBITS")
    win.set_default_size(480, 300)
    win.present()
    end = time.monotonic() + 10.0
    xid = None
    while time.monotonic() < end and xid is None:
        pump(30)
        xid = x11pos.surface_xid(win)
    check("window_mapped", xid is not None and win.get_mapped(), str(xid))
    if xid is None:
        check("run_rc", False, "no xid")
        finish()
        return
    state["xw"] = xd.create_resource_object("window", xid)

    # GDK publishes WM_NORMAL_HINTS at realize/map; wait for it — this is
    # the array whose foreign words the merge-write must preserve.
    base = poll_arr(lambda a: True, 5000)
    check("hints_present", base is not None,
          f"flags={base[0] if base else None}")

    snap("after_map")
    # ---------------------------------------------------------- publish
    pre = server_arr() or [0] * 18
    ok = x11pos.set_aspect_ratio(win, 16, 9)
    arr = poll_arr(lambda a: a[0] & _PASPECT, 4000)
    check("publish_bit_128", bool(ok) and arr is not None,
          f"flags={arr[0] if arr else None}")
    check("pin_pair_16_9",
          arr is not None and (arr[11], arr[12], arr[13], arr[14])
          == (16, 9, 16, 9),
          f"words={arr[11:15] if arr else None}")
    check("no_dead_max_publication", arr is not None and not dead_max(arr),
          f"flags={arr[0] if arr else None} max={arr[7:9] if arr else None}")
    snap("after_publish")
    foreign = [i for i in range(18)
               if i not in (0, 11, 12, 13, 14)]
    check("foreign_words_survive",
          arr is not None and all(arr[i] == pre[i] for i in foreign),
          f"pre={pre}")

    # ------------------------------------------------- no-churn (§3-C)
    # A second client selects PropertyChange on our window: identical
    # re-publishes must produce NO announcement at all (the pre-R28 watch
    # re-wrote the identical array on every drawn frame — one property
    # announcement and one WM re-constrain per frame).
    state["xw"].change_attributes(event_mask=X.PropertyChangeMask)
    xd.sync()
    pump(200)
    drain_notices()
    for _ in range(3):
        x11pos.set_aspect_ratio(win, 16, 9)
        snap(f"reapply_{_}")
    n = count_notices(500)
    snap("after_churn")
    check("no_churn_on_reapply", n == 0, f"property_notify={n}")

    # --------------------------------------- heal the legacy dead max
    arr = server_arr()
    legacy = list(arr)
    legacy[0] |= _PMAX                    # 0,0 max words already there:
    seed(legacy)                           # the exact pre-R28 signature
    snap("after_legacy_seed")
    pump(50)
    drain_notices()
    x11pos.set_aspect_ratio(win, 16, 9)    # must heal: drop dead PMaxSize
    snap("after_heal_set")
    healed = poll_arr(lambda a: (a[0] & _PASPECT) and not (a[0] & _PMAX),
                      4000)
    check("dead_max_healed_on_publish", healed is not None,
          f"flags={healed[0] if healed else None}")

    # ------------------------------- live PMaxSize survives both edges
    arr = server_arr()
    live = list(arr)
    live[0] |= _PMAX
    live[7], live[8] = 1920, 1080
    seed(live)
    snap("after_live_seed")
    pump(50)
    drain_notices()
    snap("after_live_pump")
    # a REAL change (ratio flips) so the write actually happens: the live
    # client maximum must ride through it byte-exactly.
    x11pos.set_aspect_ratio(win, 9, 16)
    snap("after_live_set")
    kept = poll_arr(lambda a: (a[0] & _PASPECT) and (a[0] & _PMAX)
                    and (a[7], a[8]) == (1920, 1080)
                    and (a[11], a[12], a[13], a[14]) == (9, 16, 9, 16),
                    4000)
    check("live_pmax_survives_publish", kept is not None,
          f"arr={kept[0:15] if kept else None}")

    # clear: PAspect bit ONLY — the live PMaxSize (flag AND words) lives.
    snap("before_clear")
    before = server_arr()
    x11pos.clear_aspect_ratio(win)
    snap("after_clear")
    cleared = poll_arr(lambda a: not (a[0] & _PASPECT), 4000)
    check("clear_keeps_live_pmax",
          cleared is not None and (cleared[0] & _PMAX)
          and (cleared[7], cleared[8]) == (1920, 1080)
          and all(cleared[i] == before[i] for i in
                  [j for j in range(1, 11)] + [15, 16, 17]),
          f"arr={cleared[0:9] if cleared else None}")

    # a second identical clear must announce NOTHING either (§3-C).
    # drain the queued notices from the seed + the legitimate clear write
    # FIRST, or this counter audits the past instead of the writes below.
    pump(100)
    drain_notices()
    for _ in range(3):
        x11pos.clear_aspect_ratio(win)
    n = count_notices(500)
    check("clear_writes_nothing_twice", n == 0, f"property_notify={n}")

    check("window_alive", win.get_realized(), "True")
    win.destroy()
    pump(100)


try:
    main()
except Exception as exc:                            # noqa: BLE001
    import traceback
    traceback.print_exc()
    check("run_rc", False, repr(exc))
else:
    check("run_rc", True, "0")
finish()
