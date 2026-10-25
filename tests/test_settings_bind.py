"""P4 gate: every schema key has EXACTLY ONE settings widget binding.

This is the structural invariant that keeps Settings and config.json from
ever diverging (later phases may swap a binding's WIDGET kind — e.g. the P6
GPU selector — but never its count).
"""
import _bootstrap  # noqa: F401

import unittest

import config as C
import settings_ui as S


def _default(key: str):
    node = C.DEFAULTS
    for part in key.split("."):
        node = node[part]
    return node


class BindingLint(unittest.TestCase):
    def test_validate_clean(self):
        self.assertEqual(S.validate_bindings(), [])

    def test_exact_one_binding_per_schema_key(self):
        schema = C.schema_leaf_keys()
        bound = S.binding_keys()
        self.assertEqual(sorted(bound), sorted(schema))
        self.assertEqual(len(bound), len(set(bound)))
        self.assertGreaterEqual(len(schema), 30)

    def test_version_is_not_bound(self):
        self.assertNotIn("version", S.binding_keys())

    def test_every_page_has_content(self):
        pages = [p for p, _t in S.PAGES]
        self.assertEqual(pages, "playback audio video decode subtitles interface "
                                "advanced".split())
        used = {b["page"] for b in S.BINDINGS}
        self.assertEqual(used, set(pages))

    def test_widget_kind_matches_default_type(self):
        for b in S.BINDINGS:
            d = _default(b["key"])
            kind = b["kind"]
            if isinstance(d, bool):
                self.assertEqual(kind, "switch", b["key"])
            elif d is None:
                self.assertEqual(kind, "fallback", b["key"])
            elif isinstance(d, dict):
                self.assertEqual(kind, "options", b["key"])
            else:
                self.assertIn(kind, ("scale", "combo", "entry",
                                     # P6 GPU selectors (widget swap, count 1)
                                     "engine", "renderer"), b["key"])

    def test_scale_defaults_in_range(self):
        for b in S.BINDINGS:
            if b["kind"] != "scale":
                continue
            d = _default(b["key"])
            self.assertGreaterEqual(b["minimum"], -1e9)
            if isinstance(d, (int, float)):
                self.assertTrue(b["minimum"] <= d <= b["maximum"],
                                f"{b['key']} default {d} outside "
                                f"[{b['minimum']}, {b['maximum']}]")

    def test_combo_defaults_selectable(self):
        for b in S.BINDINGS:
            if b["kind"] != "combo":
                continue
            self.assertIn(str(_default(b["key"])), b["choices"], b["key"])

    def test_live_apply_keys_exist(self):
        for key in S.LIVE_PROPERTIES:
            self.assertIn(key, C.schema_leaf_keys(), key)


if __name__ == "__main__":
    unittest.main()
