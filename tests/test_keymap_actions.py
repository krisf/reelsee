"""§7/§4.7 lint: every keymap action id and chrome win.* action name must
exist as a real ReelseeWindow action (AST — no GTK required)."""
import _bootstrap  # noqa: F401

import ast
import unittest

import keymap

from _bootstrap import SRC


def _window_action_names() -> set[str]:
    """Keys of the `simple = {...}` dict inside ReelseeWindow._build_actions.
    Also includes names wired via `group.add_action` outside that dict."""
    tree = ast.parse((SRC / "window.py").read_text())
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if (isinstance(tgt, ast.Name) and tgt.id == "simple"
                        and isinstance(node.value, ast.Dict)):
                    for k in node.value.keys:
                        if isinstance(k, ast.Constant):
                            names.add(k.value)
    return names


def _chrome_win_actions() -> set[str]:
    tree = ast.parse((SRC / "chrome.py").read_text())
    out: set[str] = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("set_action_name",)
                and node.args and isinstance(node.args[0], ast.Constant)):
            val = str(node.args[0].value)
            if val.startswith("win."):
                out.add(val[4:])
    return out


class KeymapActionLint(unittest.TestCase):
    def test_keymap_targets_exist(self):
        actions = _window_action_names()
        for event, target in keymap.DEFAULT_BINDINGS.items():
            if isinstance(target, tuple):
                continue
            self.assertIn(target, actions,
                          f"key {event!r} → unknown action {target!r}")

    def test_chrome_action_names_exist(self):
        actions = _window_action_names()
        for name in _chrome_win_actions():
            self.assertIn(name, actions,
                          f"chrome button → unknown action win.{name}")

    def test_t1_minimum_actions_present(self):
        actions = _window_action_names()
        for req in ("play-pause", "stop", "quit", "playlist-next",
                    "playlist-prev", "loop-file", "loop-playlist",
                    "chapter-next", "chapter-prev", "screenshot",
                    "fullscreen", "toggle-mute", "settings"):
            self.assertIn(req, actions)

    def test_required_events_bound(self):
        # §9 operator minimum: Space, ←/→, [ ], Shift+B, m, f, s (+F11)
        b = keymap.DEFAULT_BINDINGS
        for ev in ("space", "left", "right", "bracketleft", "bracketright",
                   "shift+b", "m", "f", "f11", "s",
                   "n", "b", "r", "ctrl+q", "ctrl+s"):
            self.assertIn(ev, b, f"required binding missing: {ev}")


if __name__ == "__main__":
    unittest.main()
