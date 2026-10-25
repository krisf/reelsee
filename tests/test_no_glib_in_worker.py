"""R3-P3(a) lint: no GLib call may live in a worker thread.

The P0 anti-pattern this project keeps dying of: a worker thread that
"reports back" by calling GLib.  Simple, honest AST rules over src/,
each checked per LEXICAL function scope (nested defs are separate
scopes):

  * A — a function scope that constructs a `threading.Thread` must not
    itself reference GLib;
  * B — a worker target resolvable by name (`target=self.<meth>` → the
    sibling method, or `target=<func>` → a module function) must not
    reference GLib anywhere in its body — that method IS the worker.

Targets that cannot be resolved by name (e.g. `target=w.core.destroy`
across objects in reelsee.py) are SKIPPED — honestly: that one target is
the mpv terminate worker, verified by inspection to touch no GLib.
"""
import _bootstrap  # noqa: F401

import ast
import unittest

from _bootstrap import SRC


def _is_thread_ctor(node: ast.Call) -> bool:
    f = node.func
    return isinstance(f, ast.Attribute) and f.attr == "Thread"


def _thread_target_name(node: ast.Call):
    for kw in node.keywords:
        if kw.arg == "target":
            t = kw.value
            if isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name) \
                    and t.value.id == "self":
                return t.attr
            if isinstance(t, ast.Name):
                return t.id
    return None


def _scope_nodes(fn):
    """Nodes lexically inside fn, EXCLUDING nested function defs (those
    are their own scopes and get their own check)."""
    stack = [fn]
    while stack:
        n = stack.pop()
        for child in ast.iter_child_nodes(n):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            yield child
            stack.append(child)


def _uses_glib(nodes) -> bool:
    return any(isinstance(n, ast.Name) and n.id == "GLib" for n in nodes)


def scan_source(src: str, name: str = "<src>"):
    tree = ast.parse(src)
    found = []

    # scopes: (qualname, fn, sibling-resolvable targets {name: fn})
    scopes = []
    methods_of = {}            # id(ClassDef) -> {method name: node}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            methods = {m.name: m for m in node.body
                       if isinstance(m, (ast.FunctionDef,
                                         ast.AsyncFunctionDef))}
            methods_of[id(node)] = methods
            for mname, m in methods.items():
                scopes.append((f"{node.name}.{mname}", m, methods))
    claimed = {id(m) for methods in methods_of.values() for m in
               methods.values()}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and id(node) not in claimed:
            scopes.append((node.name, node, {}))

    for qual, fn, siblings in scopes:
        nodes = list(_scope_nodes(fn))
        spawns = [n for n in nodes
                  if isinstance(n, ast.Call) and _is_thread_ctor(n)]
        if not spawns:
            continue
        glib_here = _uses_glib(nodes)
        for call in spawns:
            if glib_here:                      # rule A
                found.append((call.lineno,
                              f"{qual}: spawns a Thread in a function that "
                              "uses GLib"))
            tgt = _thread_target_name(call)    # rule B
            target_fn = siblings.get(tgt) if tgt else None
            if target_fn is not None and _uses_glib(ast.walk(target_fn)):
                found.append((call.lineno,
                              f"{qual}: thread target {tgt}() uses GLib "
                              "(worker thread)"))
    return sorted(set(found))


class LintGreenOverSrc(unittest.TestCase):
    def test_all_src_clean(self):
        offenders = []
        for p in sorted(SRC.glob("*.py")):
            for ln, msg in scan_source(p.read_text(), p.name):
                offenders.append(f"{p.name}:{ln} {msg}")
        self.assertEqual(offenders, [],
                         "GLib reachable from a worker thread:\n"
                         + "\n".join(offenders))

    def test_known_workers_exist_and_are_clean(self):
        # the lint must actually SEE the worker constructors (green because
        # nothing matched is a lie): reelsee_io runs exactly two threads,
        # both queue+generation workers, both GLib-free
        io_src = (SRC / "reelsee_io.py").read_text()
        ctors = [n for n in ast.walk(ast.parse(io_src))
                 if isinstance(n, ast.Call) and _is_thread_ctor(n)]
        self.assertEqual(len(ctors), 2)        # NextProbe + UrlResolver
        self.assertEqual(scan_source(io_src, "reelsee_io.py"), [])


class LintActuallyLints(unittest.TestCase):
    BAD_SAME_FN = (
        "import threading\nfrom gi.repository import GLib\n"
        "class P:\n"
        "    def start(self):\n"
        "        self.t = threading.Thread(target=self._run)\n"
        "        GLib.timeout_add(0, lambda: None)\n"
        "    def _run(self):\n"
        "        pass\n")

    BAD_TARGET = (
        "import threading\nfrom gi.repository import GLib\n"
        "class P:\n"
        "    def start(self):\n"
        "        self.t = threading.Thread(target=self._run)\n"
        "    def _run(self):\n"
        "        GLib.timeout_add(0, lambda: None)\n")

    GOOD_QUEUE = (
        "import threading\nimport queue\nfrom gi.repository import GLib\n"
        "class P:\n"
        "    def start(self):\n"
        "        self.t = threading.Thread(target=self._run)\n"
        "    def _run(self):\n"
        "        self.q.put(('done',))\n"
        "    def poll(self):  # main-thread drain may use GLib\n"
        "        GLib.idle_add(lambda: None)\n")

    def test_flags_glib_in_spawning_function(self):
        v = scan_source(self.BAD_SAME_FN)
        self.assertEqual([m for _, m in v],
                         ["P.start: spawns a Thread in a function that "
                          "uses GLib"])

    def test_flags_glib_in_worker_target(self):
        v = scan_source(self.BAD_TARGET)
        self.assertEqual([m for _, m in v],
                         ["P.start: thread target _run() uses GLib "
                          "(worker thread)"])

    def test_clean_queue_pattern_passes(self):
        self.assertEqual(scan_source(self.GOOD_QUEUE), [])


if __name__ == "__main__":
    unittest.main()
