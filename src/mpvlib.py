"""Vendored ctypes bindings for libmpv (client API + render API).

Ground truth for every constant and struct layout: vendor/usr/include/mpv/
{client.h,render.h,render_gl.h} (libmpv 0.41.0, client API 2.5).  Tests compile
a gcc probe against those headers and compare sizeof/offsetof
(tests/test_mpvlib_structs.py) — never invent a constant here.

No pip, no python-mpv (PLAN R3).  Load ONLY through load_libmpv(), which
walks an ordered candidate list (R4-P1, FINDINGS-INSTALL-CRASH):

    1. REELSEE_LIBMPV                 (explicit override, tests/diagnostics)
    2. <dir of this file>/vendor/usr/lib/x86_64-linux-gnu/libmpv.so.2
                                      (package-adjacent vendor dir — a
                                      self-contained .deb ships one)
    3. <repo>/vendor/usr/lib/x86_64-linux-gnu/libmpv.so.2
                                      (dev tree: src/..'s vendor/)
    4. system libmpv2: ctypes.util.find_library("mpv"), falling back to the
       canonical install dirs (the .deb Depends: libmpv2)

The first candidate that EXISTS wins.  A vendored pick additionally needs
LD_LIBRARY_PATH=<that vendor libdir> for its six vendored runtime deps
(PLAN §2) — load_libmpv() warns loudly only in that case; a system libmpv2
needs no vendored deps.  Nothing may hardcode a single path: the dev-only
assumption killed the installed app once already (FINDINGS P1).
"""
from __future__ import annotations

import ctypes
import ctypes.util
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
VENDOR_REL = Path("vendor") / "usr" / "lib" / "x86_64-linux-gnu"
VENDOR_LIBDIR = REPO / VENDOR_REL          # dev-tree vendor (candidate 3)

# System libmpv2 (Debian/Ubuntu package "libmpv2"): canonical soname + dirs.
# Kept as module constants so unit tests can point them at scratch fixtures.
SYSTEM_SONAME = "libmpv.so.2"
SYSTEM_LIBDIRS = ("/usr/lib/x86_64-linux-gnu", "/usr/lib64", "/usr/lib",
                  "/usr/local/lib", "/lib/x86_64-linux-gnu")
SYSTEM_PATHS = tuple(f"{d}/{SYSTEM_SONAME}" for d in SYSTEM_LIBDIRS)


class LibmpvNotFound(FileNotFoundError):
    """No libmpv candidate resolved.  Carries every searched path (R4-P3
    turns this into a friendly dialog + exit 2 instead of an Apport crash)."""

    def __init__(self, searched: list[tuple[Path, str]]):
        self.searched = [str(p) for p, _kind in searched]
        self.kinds = {str(p): kind for p, kind in searched}
        lines = "\n".join(f"  [{kind}] {p}" for p, kind in searched)
        super().__init__(
            "libmpv not found — searched:\n" + lines +
            "\nRemedy: install libmpv2 (sudo apt install libmpv2) or export "
            "REELSEE_LIBMPV=/path/to/libmpv.so.2")

# ---------------------------------------------------------------- formats --
# enum mpv_format (client.h)
MPV_FORMAT_NONE = 0
MPV_FORMAT_STRING = 1
MPV_FORMAT_OSD_STRING = 2
MPV_FORMAT_FLAG = 3
MPV_FORMAT_INT64 = 4
MPV_FORMAT_DOUBLE = 5
MPV_FORMAT_NODE = 6
MPV_FORMAT_NODE_ARRAY = 7
MPV_FORMAT_NODE_MAP = 8
MPV_FORMAT_BYTE_ARRAY = 9

# ------------------------------------------------------------ event ids ---
# enum mpv_event_id (client.h, 0.41 numbering — PROPERTY_CHANGE is 22!)
MPV_EVENT_NONE = 0
MPV_EVENT_SHUTDOWN = 1
MPV_EVENT_LOG_MESSAGE = 2
MPV_EVENT_GET_PROPERTY_REPLY = 3
MPV_EVENT_SET_PROPERTY_REPLY = 4
MPV_EVENT_COMMAND_REPLY = 5
MPV_EVENT_START_FILE = 6
MPV_EVENT_END_FILE = 7
MPV_EVENT_FILE_LOADED = 8
MPV_EVENT_IDLE = 11
MPV_EVENT_TICK = 14
MPV_EVENT_CLIENT_MESSAGE = 16
MPV_EVENT_VIDEO_RECONFIG = 17
MPV_EVENT_AUDIO_RECONFIG = 18
MPV_EVENT_SEEK = 20
MPV_EVENT_PLAYBACK_RESTART = 21
MPV_EVENT_PROPERTY_CHANGE = 22
MPV_EVENT_QUEUE_OVERFLOW = 24
MPV_EVENT_HOOK = 25

# ----------------------------------------------------- render param types --
# enum mpv_render_param_type (render.h)
MPV_RENDER_PARAM_INVALID = 0
MPV_RENDER_PARAM_API_TYPE = 1
MPV_RENDER_PARAM_OPENGL_INIT_PARAMS = 2
MPV_RENDER_PARAM_OPENGL_FBO = 3
MPV_RENDER_PARAM_FLIP_Y = 4
MPV_RENDER_PARAM_DEPTH = 5
MPV_RENDER_PARAM_ICC_PROFILE = 6
MPV_RENDER_PARAM_AMBIENT_LIGHT = 7
MPV_RENDER_PARAM_X11_DISPLAY = 8
MPV_RENDER_PARAM_WL_DISPLAY = 9
MPV_RENDER_PARAM_ADVANCED_CONTROL = 10
MPV_RENDER_PARAM_NEXT_FRAME_INFO = 11
MPV_RENDER_PARAM_BLOCK_FOR_TARGET_TIME = 12
MPV_RENDER_PARAM_SKIP_RENDERING = 13
MPV_RENDER_PARAM_DRM_DRAW_SURFACE_SIZE = 15
MPV_RENDER_PARAM_SW_SIZE = 17
MPV_RENDER_PARAM_SW_FORMAT = 18
MPV_RENDER_PARAM_SW_STRIDE = 19
MPV_RENDER_PARAM_SW_POINTER = 20

MPV_RENDER_API_TYPE_OPENGL = b"opengl"
MPV_RENDER_UPDATE_FRAME = 1 << 0  # render.h

# GL token (no mpv header for it; same value the PROVEN spike uses, PLAN §0.4)
GL_DRAW_FRAMEBUFFER_BINDING = 0x8CA6

# ---------------------------------------------------------------- structs --
GET_PROC_ADDRESS = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_char_p)


class MpvOpenGLInitParams(ctypes.Structure):
    """struct mpv_opengl_init_params (render_gl.h)."""
    _fields_ = [
        ("get_proc_address", GET_PROC_ADDRESS),
        ("get_proc_address_ctx", ctypes.c_void_p),
    ]


class MpvOpenGLFbo(ctypes.Structure):
    """struct mpv_opengl_fbo (render_gl.h): int fbo; int w, h; int internal_format."""
    _fields_ = [
        ("fbo", ctypes.c_int),
        ("w", ctypes.c_int),
        ("h", ctypes.c_int),
        ("internal_format", ctypes.c_int),
    ]


class MpvRenderParam(ctypes.Structure):
    """struct mpv_render_param (render.h): {enum type; void *data;}."""
    _fields_ = [
        ("type", ctypes.c_int),
        ("data", ctypes.c_void_p),
    ]


class MpvNode(ctypes.Structure):
    """struct mpv_node (client.h) — union u first, then mpv_format format."""


class MpvNodeList(ctypes.Structure):
    """struct mpv_node_list (client.h): int num; mpv_node *values; char **keys."""
    _fields_ = [
        ("num", ctypes.c_int),
        ("values", ctypes.POINTER(MpvNode)),
        ("keys", ctypes.POINTER(ctypes.c_char_p)),
    ]


class _MpvNodeUnion(ctypes.Union):
    _fields_ = [
        ("string", ctypes.c_char_p),
        ("flag", ctypes.c_int),
        ("int64", ctypes.c_int64),
        ("double_", ctypes.c_double),
        ("list", ctypes.POINTER(MpvNodeList)),
        ("ba", ctypes.c_void_p),
    ]


MpvNode._fields_ = [
    ("u", _MpvNodeUnion),
    ("format", ctypes.c_int),
]


class MpvByteArray(ctypes.Structure):
    """struct mpv_byte_array (client.h): void *data; size_t size."""
    _fields_ = [
        ("data", ctypes.c_void_p),
        ("size", ctypes.c_size_t),
    ]


class MpvEventProperty(ctypes.Structure):
    """struct mpv_event_property (client.h)."""
    _fields_ = [
        ("name", ctypes.c_char_p),
        ("format", ctypes.c_int),
        ("data", ctypes.c_void_p),
    ]


class MpvEvent(ctypes.Structure):
    """struct mpv_event (client.h, 0.41): exactly 4 fields."""
    _fields_ = [
        ("event_id", ctypes.c_int),
        ("error", ctypes.c_int),
        ("reply_userdata", ctypes.c_uint64),
        ("data", ctypes.c_void_p),
    ]


class MpvEventEndFile(ctypes.Structure):
    """struct mpv_event_end_file (client.h)."""
    _fields_ = [
        ("reason", ctypes.c_int),
        ("error", ctypes.c_int),
        ("playlist_entry_id", ctypes.c_int64),
        ("playlist_insert_id", ctypes.c_int64),
        ("playlist_insert_num_entries", ctypes.c_int),
    ]


# ------------------------------------------------------------- functions --
VOIDP = ctypes.c_void_p
C_CHAR_P = ctypes.c_char_p
C_INT = ctypes.c_int
C_UINT64 = ctypes.c_uint64

PROTOTYPES: dict[str, tuple] = {
    # name: (restype, argtypes)
    "mpv_client_api_version": (C_UINT64, []),
    "mpv_error_string": (C_CHAR_P, [C_INT]),
    "mpv_free": (None, [VOIDP]),
    "mpv_client_name": (C_CHAR_P, [VOIDP]),
    "mpv_create": (VOIDP, []),
    "mpv_initialize": (C_INT, [VOIDP]),
    "mpv_terminate_destroy": (None, [VOIDP]),
    "mpv_destroy": (None, [VOIDP]),
    "mpv_set_option": (C_INT, [VOIDP, C_CHAR_P, C_INT, VOIDP]),
    "mpv_set_option_string": (C_INT, [VOIDP, C_CHAR_P, C_CHAR_P]),
    "mpv_command": (C_INT, [VOIDP, ctypes.POINTER(C_CHAR_P)]),
    "mpv_command_string": (C_INT, [VOIDP, C_CHAR_P]),
    # 0.41 finding (P1, measured): the value-returning "get_property"/"get"
    # COMMANDS no longer exist (mpv_command* → MPV_ERROR_INVALID_PARAMETER).
    # Reads go through observed properties (snapshot) or mpv_get_property* on
    # a worker thread; writes through mpv_command_async / *_property_async.
    "mpv_command_async": (C_INT, [VOIDP, C_UINT64, ctypes.POINTER(C_CHAR_P)]),
    "mpv_command_ret": (C_INT, [VOIDP, ctypes.POINTER(C_CHAR_P), ctypes.POINTER(MpvNode)]),
    "mpv_abort_async_command": (None, [VOIDP, C_UINT64]),
    "mpv_set_property": (C_INT, [VOIDP, C_CHAR_P, C_INT, VOIDP]),
    "mpv_set_property_string": (C_INT, [VOIDP, C_CHAR_P, C_CHAR_P]),
    "mpv_del_property": (C_INT, [VOIDP, C_CHAR_P]),
    "mpv_set_property_async": (C_INT, [VOIDP, C_UINT64, C_CHAR_P, C_INT, VOIDP]),
    "mpv_get_property": (C_INT, [VOIDP, C_CHAR_P, C_INT, VOIDP]),
    # VOIDP (not c_char_p) on purpose: the string is libmpv-allocated and
    # must be released with mpv_free(); use get_property_string() below.
    "mpv_get_property_string": (VOIDP, [VOIDP, C_CHAR_P]),
    "mpv_get_property_async": (C_INT, [VOIDP, C_UINT64, C_CHAR_P, C_INT]),
    "mpv_observe_property": (C_INT, [VOIDP, C_UINT64, C_CHAR_P, C_INT]),
    "mpv_unobserve_property": (C_INT, [VOIDP, C_UINT64]),
    "mpv_wait_event": (ctypes.POINTER(MpvEvent), [VOIDP, ctypes.c_double]),
    "mpv_event_name": (C_CHAR_P, [C_INT]),
    "mpv_free_node_contents": (None, [ctypes.POINTER(MpvNode)]),
    "mpv_wakeup": (None, [VOIDP]),
    # render API
    "mpv_render_context_create": (C_INT, [ctypes.POINTER(VOIDP), VOIDP,
                                           ctypes.POINTER(MpvRenderParam)]),
    "mpv_render_context_set_parameter": (C_INT, [VOIDP, MpvRenderParam]),
    "mpv_render_context_set_update_callback": (None, [VOIDP, ctypes.c_void_p, VOIDP]),
    "mpv_render_context_update": (C_UINT64, [VOIDP]),
    "mpv_render_context_render": (C_INT, [VOIDP, ctypes.POINTER(MpvRenderParam)]),
    "mpv_render_context_report_swap": (None, [VOIDP]),
    "mpv_render_context_free": (None, [VOIDP]),
}

UPDATE_CALLBACK = ctypes.CFUNCTYPE(None, ctypes.c_void_p)


def libmpv_candidates() -> list[tuple[Path, str]]:
    """Ordered (path, kind) candidates — the FIRST one that exists wins.

    kind is one of "env" | "vendored" | "system"; only "vendored" picks get
    the LD_LIBRARY_PATH warning (their runtime deps sit next to the .so).
    """
    out: list[tuple[Path, str]] = []
    seen: set[str] = set()

    def add(p, kind: str) -> None:
        p = Path(p)
        if str(p) not in seen:
            seen.add(str(p))
            out.append((p, kind))

    env = os.environ.get("REELSEE_LIBMPV")
    if env:
        add(env, "env")
    # package-adjacent vendor dir (self-contained .deb layout), then dev tree
    add(HERE / VENDOR_REL / "libmpv.so.2", "vendored")
    add(REPO / VENDOR_REL / "libmpv.so.2", "vendored")
    # system libmpv2 (the .deb Depends: libmpv2 — what an installed user has)
    try:
        found = ctypes.util.find_library("mpv")
    except Exception:               # never let discovery crash resolution
        found = None
    if found:
        if os.path.isabs(found):
            add(found, "system")
        else:                       # a bare soname (ldconfig answer): resolve
            for d in SYSTEM_LIBDIRS:
                add(Path(d) / found, "system")
    for p in SYSTEM_PATHS:
        add(p, "system")
    return out


def find_libmpv() -> Path:
    """Path of the libmpv to load, by the candidate order above (module
    docstring).  Raises LibmpvNotFound (a FileNotFoundError) listing every
    path searched when nothing exists."""
    return resolve_libmpv()[0]


def resolve_libmpv() -> tuple[Path, str]:
    """(path, kind) of the first candidate that exists."""
    searched = libmpv_candidates()
    for p, kind in searched:
        if p.exists():
            return p, kind
    raise LibmpvNotFound(searched)


def _is_vendored(p: Path) -> bool:
    """True when p is one of this checkout/install's own vendor-dir files."""
    return p in (HERE / VENDOR_REL / "libmpv.so.2",
                 REPO / VENDOR_REL / "libmpv.so.2")


def load_libmpv(path: str | os.PathLike | None = None) -> ctypes.CDLL:
    """Load libmpv (candidate resolution above unless `path` pins it) and
    attach every prototype in PROTOTYPES.

    A VENDORED pick needs LD_LIBRARY_PATH=<vendor libdir> for its six
    vendored runtime deps — we check and say so loudly ONLY then; a system
    libmpv2 resolves its own deps through the loader and needs nothing.
    """
    if path is not None:
        p = Path(path)
        if not p.exists():
            raise LibmpvNotFound([(p, "explicit")])
        kind = "vendored" if _is_vendored(p) else "explicit"
    else:
        p, kind = resolve_libmpv()
    if kind == "vendored":
        ld = os.environ.get("LD_LIBRARY_PATH", "")
        libdir = str(p.parent)
        if libdir not in ld.split(":"):
            print(
                f"mpvlib: WARNING: {libdir} not on LD_LIBRARY_PATH — "
                "vendored runtime deps may fail to resolve "
                "(run via scripts/headless-smoke.sh or export it)",
                file=sys.stderr,
            )
    lib = ctypes.CDLL(str(p))
    for name, (restype, argtypes) in PROTOTYPES.items():
        fn = getattr(lib, name)
        fn.restype = restype
        fn.argtypes = argtypes
    print(f"mpvlib: loaded {p} [{kind}]", file=sys.stderr)
    return lib


def client_api_version(lib: ctypes.CDLL) -> tuple[int, int]:
    v = lib.mpv_client_api_version()
    return (v >> 16, v & 0xFFFF)


def error_string(lib: ctypes.CDLL, code: int) -> str:
    s = lib.mpv_error_string(code)
    return s.decode(errors="replace") if s else f"error {code}"


# ------------------------------------------------------------- node I/O ---
def node_to_python(node: MpvNode):
    """Convert an mpv_node (read-only) to plain Python objects."""
    fmt = node.format
    if fmt == MPV_FORMAT_NONE:
        return None
    if fmt in (MPV_FORMAT_STRING, MPV_FORMAT_OSD_STRING):
        v = node.u.string
        return v.decode(errors="replace") if v else None
    if fmt == MPV_FORMAT_FLAG:
        return bool(node.u.flag)
    if fmt == MPV_FORMAT_INT64:
        return node.u.int64
    if fmt == MPV_FORMAT_DOUBLE:
        return node.u.double_
    if fmt in (MPV_FORMAT_NODE_ARRAY, MPV_FORMAT_NODE_MAP):
        lst = node.u.list
        if not lst:
            return {} if fmt == MPV_FORMAT_NODE_MAP else []
        n = lst.contents.num
        out = {} if fmt == MPV_FORMAT_NODE_MAP else []
        for i in range(n):
            item = lst.contents.values[i]
            if fmt == MPV_FORMAT_NODE_MAP:
                # keys live in the parallel keys[i] char* array — NOT in the
                # value node's union (reading it there segfaults on 0.41)
                k = lst.contents.keys[i] if lst.contents.keys else None
                out[k.decode(errors="replace") if k else ""] = node_to_python(item)
            else:
                out.append(node_to_python(item))
        return out
    if fmt == MPV_FORMAT_BYTE_ARRAY:
        ba = ctypes.cast(node.u.ba, ctypes.POINTER(MpvByteArray)).contents
        return ctypes.string_at(ba.data, ba.size)
    return None


def event_property_value(lib: ctypes.CDLL, ev_prop: MpvEventProperty):
    """Decode the payload of an mpv_event_property (property-change events).

    Per client.h the value pointer is "like the pointer argument passed to
    mpv_get_property()", i.e. for STRING it is a char** (double pointer).
    """
    fmt = ev_prop.format
    if fmt == MPV_FORMAT_NONE or not ev_prop.data:
        return None
    if fmt in (MPV_FORMAT_STRING, MPV_FORMAT_OSD_STRING):
        pp = ctypes.cast(ev_prop.data, ctypes.POINTER(ctypes.c_char_p)).contents
        return pp.value.decode(errors="replace") if pp.value else None
    if fmt == MPV_FORMAT_FLAG:
        return bool(ctypes.cast(ev_prop.data, ctypes.POINTER(ctypes.c_int)).contents.value)
    if fmt == MPV_FORMAT_INT64:
        return ctypes.cast(ev_prop.data, ctypes.POINTER(ctypes.c_int64)).contents.value
    if fmt == MPV_FORMAT_DOUBLE:
        return ctypes.cast(ev_prop.data, ctypes.POINTER(ctypes.c_double)).contents.value
    if fmt in (MPV_FORMAT_NODE, MPV_FORMAT_NODE_ARRAY, MPV_FORMAT_NODE_MAP):
        node = ctypes.cast(ev_prop.data, ctypes.POINTER(MpvNode)).contents
        try:
            return node_to_python(node)
        finally:
            lib.mpv_free_node_contents(ctypes.cast(ev_prop.data, ctypes.POINTER(MpvNode)))
    return None


def make_command(*args: str) -> ctypes.Array:
    """NULL-terminated char* array for mpv_command*/mpv_command_async."""
    return (ctypes.c_char_p * (len(args) + 1))(*[a.encode() for a in args], None)


def get_property_string(lib: ctypes.CDLL, handle, name: str) -> str | None:  # SYNC-MPV
    """Sync string getter — WORKER THREADS ONLY (R9). Frees with mpv_free."""
    p = lib.mpv_get_property_string(handle, name.encode())
    if not p:
        return None
    try:
        return ctypes.c_char_p(p).value.decode(errors="replace")
    finally:
        lib.mpv_free(p)
