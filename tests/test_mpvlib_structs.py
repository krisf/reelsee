"""P1 gate: ctypes structs must match the vendored mpv headers exactly.

Compiles a small C probe against vendor/usr/include/mpv/*.h (the ground truth,
PLAN §2) and compares sizeof/offsetof for every ctypes struct in mpvlib, plus
every enum constant.  Any mismatch = fail.
"""
from __future__ import annotations

import ctypes
import subprocess
import unittest
from pathlib import Path

import _bootstrap  # noqa: F401  (sys.path)
import mpvlib

INCLUDE = _bootstrap.REPO / "vendor" / "usr" / "include"

FIELDS = {
    "mpv_render_param": [("type", "type"), ("data", "data")],
    "mpv_opengl_init_params": [("get_proc_address", "get_proc_address"),
                               ("get_proc_address_ctx", "get_proc_address_ctx")],
    "mpv_opengl_fbo": [("fbo", "fbo"), ("w", "w"), ("h", "h"),
                       ("internal_format", "internal_format")],
    "mpv_node": [("u", "u"), ("format", "format")],
    "mpv_node_list": [("num", "num"), ("values", "values"), ("keys", "keys")],
    "mpv_byte_array": [("data", "data"), ("size", "size")],
    "mpv_event": [("event_id", "event_id"), ("error", "error"),
                  ("reply_userdata", "reply_userdata"), ("data", "data")],
    "mpv_event_property": [("name", "name"), ("format", "format"), ("data", "data")],
    "mpv_event_end_file": [("reason", "reason"), ("error", "error"),
                           ("playlist_entry_id", "playlist_entry_id"),
                           ("playlist_insert_id", "playlist_insert_id"),
                           ("playlist_insert_num_entries",
                            "playlist_insert_num_entries")],
}

CTYPES_STRUCTS = {
    "mpv_render_param": mpvlib.MpvRenderParam,
    "mpv_opengl_init_params": mpvlib.MpvOpenGLInitParams,
    "mpv_opengl_fbo": mpvlib.MpvOpenGLFbo,
    "mpv_node": mpvlib.MpvNode,
    "mpv_node_list": mpvlib.MpvNodeList,
    "mpv_byte_array": mpvlib.MpvByteArray,
    "mpv_event": mpvlib.MpvEvent,
    "mpv_event_property": mpvlib.MpvEventProperty,
    "mpv_event_end_file": mpvlib.MpvEventEndFile,
}

ENUM_CHECKS: list[tuple[str, int]] = [
    # formats
    ("MPV_FORMAT_NONE", mpvlib.MPV_FORMAT_NONE),
    ("MPV_FORMAT_STRING", mpvlib.MPV_FORMAT_STRING),
    ("MPV_FORMAT_OSD_STRING", mpvlib.MPV_FORMAT_OSD_STRING),
    ("MPV_FORMAT_FLAG", mpvlib.MPV_FORMAT_FLAG),
    ("MPV_FORMAT_INT64", mpvlib.MPV_FORMAT_INT64),
    ("MPV_FORMAT_DOUBLE", mpvlib.MPV_FORMAT_DOUBLE),
    ("MPV_FORMAT_NODE", mpvlib.MPV_FORMAT_NODE),
    ("MPV_FORMAT_NODE_ARRAY", mpvlib.MPV_FORMAT_NODE_ARRAY),
    ("MPV_FORMAT_NODE_MAP", mpvlib.MPV_FORMAT_NODE_MAP),
    ("MPV_FORMAT_BYTE_ARRAY", mpvlib.MPV_FORMAT_BYTE_ARRAY),
    # events (0.41 numbering — PROPERTY_CHANGE is 22 here)
    ("MPV_EVENT_NONE", mpvlib.MPV_EVENT_NONE),
    ("MPV_EVENT_SHUTDOWN", mpvlib.MPV_EVENT_SHUTDOWN),
    ("MPV_EVENT_LOG_MESSAGE", mpvlib.MPV_EVENT_LOG_MESSAGE),
    ("MPV_EVENT_GET_PROPERTY_REPLY", mpvlib.MPV_EVENT_GET_PROPERTY_REPLY),
    ("MPV_EVENT_SET_PROPERTY_REPLY", mpvlib.MPV_EVENT_SET_PROPERTY_REPLY),
    ("MPV_EVENT_COMMAND_REPLY", mpvlib.MPV_EVENT_COMMAND_REPLY),
    ("MPV_EVENT_START_FILE", mpvlib.MPV_EVENT_START_FILE),
    ("MPV_EVENT_END_FILE", mpvlib.MPV_EVENT_END_FILE),
    ("MPV_EVENT_FILE_LOADED", mpvlib.MPV_EVENT_FILE_LOADED),
    ("MPV_EVENT_IDLE", mpvlib.MPV_EVENT_IDLE),
    ("MPV_EVENT_TICK", mpvlib.MPV_EVENT_TICK),
    ("MPV_EVENT_CLIENT_MESSAGE", mpvlib.MPV_EVENT_CLIENT_MESSAGE),
    ("MPV_EVENT_VIDEO_RECONFIG", mpvlib.MPV_EVENT_VIDEO_RECONFIG),
    ("MPV_EVENT_AUDIO_RECONFIG", mpvlib.MPV_EVENT_AUDIO_RECONFIG),
    ("MPV_EVENT_SEEK", mpvlib.MPV_EVENT_SEEK),
    ("MPV_EVENT_PLAYBACK_RESTART", mpvlib.MPV_EVENT_PLAYBACK_RESTART),
    ("MPV_EVENT_PROPERTY_CHANGE", mpvlib.MPV_EVENT_PROPERTY_CHANGE),
    ("MPV_EVENT_QUEUE_OVERFLOW", mpvlib.MPV_EVENT_QUEUE_OVERFLOW),
    ("MPV_EVENT_HOOK", mpvlib.MPV_EVENT_HOOK),
    # render params
    ("MPV_RENDER_PARAM_INVALID", mpvlib.MPV_RENDER_PARAM_INVALID),
    ("MPV_RENDER_PARAM_API_TYPE", mpvlib.MPV_RENDER_PARAM_API_TYPE),
    ("MPV_RENDER_PARAM_OPENGL_INIT_PARAMS", mpvlib.MPV_RENDER_PARAM_OPENGL_INIT_PARAMS),
    ("MPV_RENDER_PARAM_OPENGL_FBO", mpvlib.MPV_RENDER_PARAM_OPENGL_FBO),
    ("MPV_RENDER_PARAM_FLIP_Y", mpvlib.MPV_RENDER_PARAM_FLIP_Y),
    ("MPV_RENDER_PARAM_DEPTH", mpvlib.MPV_RENDER_PARAM_DEPTH),
    ("MPV_RENDER_PARAM_ADVANCED_CONTROL", mpvlib.MPV_RENDER_PARAM_ADVANCED_CONTROL),
    ("MPV_RENDER_PARAM_SW_POINTER", mpvlib.MPV_RENDER_PARAM_SW_POINTER),
    ("MPV_RENDER_UPDATE_FRAME", mpvlib.MPV_RENDER_UPDATE_FRAME),
]


def _probe_source() -> str:
    lines = [
        "#include <stdio.h>",
        "#include <stddef.h>",
        "#include <mpv/client.h>",
        "#include <mpv/render.h>",
        "#include <mpv/render_gl.h>",
        "int main(void) {",
    ]
    for name in FIELDS:
        lines.append(f'    printf("sizeof %s %zu\\n", "{name}", sizeof({name}));')
        for _, cfield in FIELDS[name]:
            lines.append(f'    printf("offset {name}.{cfield} %zu\\n", '
                         f'offsetof({name}, {cfield}));')
    for cname, _py in ENUM_CHECKS:
        lines.append(f'    printf("enum {cname} %lld\\n", (long long){cname});')
    lines.append('    printf("str MPV_RENDER_API_TYPE_OPENGL %s\\n", '
                 'MPV_RENDER_API_TYPE_OPENGL);')
    lines.append("    return 0;")
    lines.append("}")
    return "\n".join(lines) + "\n"


class HeaderConformance(unittest.TestCase):
    probe: dict[str, str] = {}

    @classmethod
    def setUpClass(cls):
        if not INCLUDE.exists():
            raise unittest.SkipTest(f"vendored headers missing at {INCLUDE}")
        src = _bootstrap.REPO / "scratch" / "probe_mpv_structs.c"
        exe = src.with_suffix(".out")
        src.parent.mkdir(exist_ok=True)
        src.write_text(_probe_source())
        cp = subprocess.run(
            ["gcc", f"-I{INCLUDE}", "-o", str(exe), str(src)],
            capture_output=True, text=True)
        if cp.returncode != 0:
            raise AssertionError(f"gcc probe failed:\n{cp.stderr}")
        run = subprocess.run([str(exe)], capture_output=True, text=True, timeout=30)
        assert run.returncode == 0, run.stderr
        cls.probe = {}
        for line in run.stdout.splitlines():
            parts = line.split(" ", 2)
            if len(parts) == 3:
                cls.probe[f"{parts[0]} {parts[1]}"] = parts[2]

    def test_struct_sizes(self):
        for name, struct in CTYPES_STRUCTS.items():
            with self.subTest(struct=name):
                want = int(self.probe[f"sizeof {name}"])
                self.assertEqual(ctypes.sizeof(struct), want,
                                 f"sizeof({name}) ctypes != header")

    def test_field_offsets(self):
        for name, fields in FIELDS.items():
            struct = CTYPES_STRUCTS[name]
            mapping = dict(fields)
            for ctype_field, cfield in mapping.items():
                with self.subTest(struct=name, field=cfield):
                    want = int(self.probe[f"offset {name}.{cfield}"])
                    self.assertEqual(getattr(struct, ctype_field).offset, want,
                                     f"offsetof({name}.{cfield}) mismatch")

    def test_enum_constants(self):
        for cname, pyval in ENUM_CHECKS:
            with self.subTest(const=cname):
                self.assertEqual(int(self.probe[f"enum {cname}"]), pyval,
                                 f"constant {cname} differs from header")

    def test_api_type_string(self):
        self.assertEqual(self.probe["str MPV_RENDER_API_TYPE_OPENGL"].encode(),
                         mpvlib.MPV_RENDER_API_TYPE_OPENGL)


if __name__ == "__main__":
    unittest.main()
