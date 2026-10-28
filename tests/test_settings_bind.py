"""P4 gate: every schema key has EXACTLY ONE settings widget binding.

This is the structural invariant that keeps Settings and config.json from
ever diverging (later phases may swap a binding's WIDGET kind — e.g. the P6
GPU selector — but never its count).
"""
import _bootstrap  # noqa: F401

import unittest
import unittest.mock

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
                                     "engine", "renderer",
                                     # R16-E §E.2 render-node picker (same
                                     # precedent: widget swap, count 1)
                                     "vanode"), b["key"])

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


class NodePicker(unittest.TestCase):
    """R16-E §E.2: the settings row renders the render nodes it is TOLD about.

    Enumeration is mocked (this sandbox has no /dev/dri and the row must not
    depend on the machine it runs on), so the test drives the real
    `_selector_choices` code path with a fake engine list."""

    class _Picker:
        _engines_cached = None

        def _engines(self):
            return self._engines_cached

        _selector_choices = S.SettingsWindow._selector_choices

    def picker(self, engines):
        p = self._Picker()
        p._engines_cached = engines
        return p

    def test_row_is_bound_once_on_the_decode_page(self):
        rows = [b for b in S.BINDINGS if b["key"] == "playback.vaapi_device"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "vanode")
        self.assertEqual(rows[0]["page"], "decode")
        self.assertEqual(_default("playback.vaapi_device"), "")

    def test_choices_are_the_enumerated_nodes_plus_auto(self):
        from engine import Engine
        engines = [
            Engine(id="auto", label="Auto", kind="auto"),
            Engine(id="0000:03:00.0", label="R9700 (VAAPI)", kind="vaapi",
                   pci="0000:03:00.0", render_node="/dev/dri/renderD128",
                   is_display_gpu=True, is_discrete=True),
            Engine(id="0000:13:00.0", label="Raphael (VAAPI)", kind="vaapi",
                   pci="0000:13:00.0", render_node="/dev/dri/renderD130"),
            Engine(id="cpu", label="CPU decode (software)", kind="cpu"),
        ]
        values, labels = self.picker(engines)._selector_choices("vanode", "")
        self.assertEqual(values[0], "", "empty value = auto (§E.1)")
        self.assertIn("Auto", labels[0])
        self.assertEqual(values[1:], ["/dev/dri/renderD128",
                                      "/dev/dri/renderD130"])
        for v, lb in zip(values[1:], labels[1:]):
            self.assertIn(v, lb)                       # the node itself
        self.assertIn("0000:13:00.0", labels[2])       # PCI slot label
        self.assertIn("Raphael", labels[2])            # card name label
        self.assertIn("discrete", labels[1],
                      "ROUND18: the node auto never picks is the DISCRETE "
                      "one — the row must say so by discrete-ness")
        self.assertIn("never picks", labels[1])
        self.assertNotIn("cpu", values)                # not a node choice

    def test_real_rig_igpu_row_not_skipped_for_driving_the_display(self):
        """ROUND18 gate (truth table case 8, UI half): on the operator's rig
        the Granite Ridge iGPU carries the displays AND is what auto picks.
        Its row must be labelled as the auto pick — and must NOT carry the
        old "drives the display ⇒ auto never picks it" tag, which told the
        operator to distrust the correct choice."""
        from engine import Engine
        import engine as E
        igpu = Engine(id="0000:13:00.0", label="Granite Ridge (VAAPI)",
                      kind="vaapi", pci="0000:13:00.0",
                      render_node="/dev/dri/renderD130",
                      is_display_gpu=True, is_discrete=False)
        dgpu = Engine(id="0000:03:00.0", label="R9700 (VAAPI)", kind="vaapi",
                      pci="0000:03:00.0", render_node="/dev/dri/renderD128",
                      is_display_gpu=True, is_discrete=True)
        engines = [Engine(id="auto", label="Auto", kind="auto"),
                   dgpu, igpu,
                   Engine(id="cpu", label="CPU decode (software)", kind="cpu")]
        with unittest.mock.patch.object(E, "pick_decode_node",
                                        lambda *a, **k: "/dev/dri/renderD130"):
            values, labels = self.picker(engines)._selector_choices(
                "vanode", "")
        self.assertIn("auto picks this", labels[values.index("/dev/dri/renderD130")])
        self.assertNotIn("never picks", labels[values.index("/dev/dri/renderD130")])
        self.assertNotIn("drives the display",
                         labels[values.index("/dev/dri/renderD130")])
        self.assertIn("discrete GPU (auto never picks it)",
                      labels[values.index("/dev/dri/renderD128")])

    def test_saved_value_that_is_not_enumerated_stays_selectable(self):
        values, labels = self.picker([])._selector_choices(
            "vanode", "/dev/dri/renderD199")
        self.assertIn("/dev/dri/renderD199", values)
        self.assertIn("(saved)", labels[values.index("/dev/dri/renderD199")])


if __name__ == "__main__":
    unittest.main()
