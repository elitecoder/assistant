"""Nothing in Assistant's own code types into an agent session except src/assistant/session_effects.py.

That module writes the intent to the effects ledger before the first keystroke, so a typing that isn't in the
ledger can't happen from here. Caught, in Python files under bin/, src/, work_queue/ and hooks/:
- any string (a docstring excepted, an f-string's pieces included) that holds `surface.send_`, so the rpc method
  names can't be written out, split across an f-string, or put in a shell command;
- any string that runs `cmux send` or holds the verbs `send-text`, `send-panel`, `send-key`, `send-keys` (which is
  also tmux's) or an AppleScript `keystroke`/`key code`, and any argument list that has `send` with something named cmux;
- a call to one of the rpc wrappers whose method isn't a literal string, except inside the transports the one module
  is given (the functions named in TRANSPORTS).
Left alone on purpose: docstrings and comments that describe those calls; cmux's `new-workspace --command`, which
starts a session with a command instead of typing.

What it can't see, and the other tests and docs/effects.md cover instead: a method name built at run time by
joining pieces or reading it from a file or a setting, then passed to a wrapper from inside a transport;
`getattr(obj, "send_" + name)`; text typed by a program that isn't Python (the `spawn-claude-workspace` skill's shell
snippet, the warm session's own model running `cmux`); and code that isn't under the four folders.
"""
import ast
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
FOLDERS = ("bin", "src", "work_queue", "hooks")
THE_ONE = "src/assistant/session_effects.py"
# Nothing is listed here: every path that types is routed. A path that is not yet routed must be named in this
# list, with the reason, so nothing is skipped silently.
NOT_YET_ROUTED: dict = {}
VERBS = {"send"}  # the other verbs are caught as strings
STRING = re.compile(r"surface\.send_|\bsend-(text|panel|keys?)\b(?!-)|\bkeystroke\b|\bkey code\b|"
                    r"\bcmux\s+send(-\w+)?(\s+[-<$\"'A-Z]|\s*$)", re.S)
WRAPPERS = {"_cmux_rpc", "_cmux_type_rpc", "rpc", "run_cmux", "cmux_rpc"}
# The functions that carry a method name from `Keys` to cmux: they are handed the name, and `Keys` is what names it.
TRANSPORTS = {"transport", "_cmux_type_rpc", "rpc"}


def _doc_nodes(tree):
    docs = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.body \
                and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant):
            docs.add(id(node.body[0].value))
    return docs


def _mentions_cmux(node) -> bool:
    name = node.id if isinstance(node, ast.Name) else node.attr if isinstance(node, ast.Attribute) \
        else node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else ""
    return "cmux" in name.lower()


def _callee(node) -> str:
    return node.id if isinstance(node, ast.Name) else node.attr if isinstance(node, ast.Attribute) else ""


def _method_argument(call):
    args = [a for a in call.args if not (isinstance(a, ast.Name) and a.id == "paths")]
    if _callee(call.func) == "run_cmux":
        args = args[1:]  # run_cmux("rpc", method, params)
    return args[0] if args else None


class _Walker(ast.NodeVisitor):
    def __init__(self, docs):
        self.docs, self.found, self.inside = docs, [], []

    def visit_FunctionDef(self, node):
        self.inside.append(node.name)
        self.generic_visit(node)
        self.inside.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Constant(self, node):
        if isinstance(node.value, str) and id(node) not in self.docs and STRING.search(node.value):
            self.found.append(node.lineno)

    def visit_List(self, node):
        words = [e.value for e in node.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]
        if VERBS & set(words) and any(_mentions_cmux(e) for e in node.elts):
            self.found.append(node.lineno)
        self.generic_visit(node)

    visit_Tuple = visit_List

    def visit_Call(self, node):
        if _callee(node.func) in WRAPPERS and not TRANSPORTS & set(self.inside):
            method = _method_argument(node)
            if method is not None and not (isinstance(method, ast.Constant) and isinstance(method.value, str)):
                self.found.append(node.lineno)
        self.generic_visit(node)


def typing_calls(source: str) -> list:
    """Line numbers of code that types into a session, or that may."""
    tree = ast.parse(source)
    walker = _Walker(_doc_nodes(tree))
    walker.visit(tree)
    return sorted(set(walker.found))


def test_only_the_one_module_names_the_calls_that_type():
    found = []
    for folder in FOLDERS:
        for path in sorted((REPO / folder).rglob("*.py")):
            rel = str(path.relative_to(REPO))
            if rel != THE_ONE and rel not in NOT_YET_ROUTED:
                found += [f"{rel}:{line}" for line in typing_calls(path.read_text())]
    assert found == [], "type through assistant.session_effects (it records the intent first): " + ", ".join(found)


def test_the_one_module_is_where_the_calls_live_and_every_listed_exception_still_types():
    assert typing_calls((REPO / THE_ONE).read_text())
    for rel in NOT_YET_ROUTED:  # a path that is now routed must be taken off the list
        assert typing_calls((REPO / rel).read_text()), rel


def test_every_transport_the_fence_allows_exists():
    names = set()
    for folder in FOLDERS:
        for path in (REPO / folder).rglob("*.py"):
            names |= {n.name for n in ast.walk(ast.parse(path.read_text())) if isinstance(n, ast.FunctionDef)}
    assert TRANSPORTS <= names


def test_the_fence_sees_names_in_any_string_f_strings_shell_text_verbs_and_dynamic_methods():
    assert typing_calls('rpc("surface.send_text", {})\nx = "surface.send_key"') == [1, 2]
    assert typing_calls('m = f"surface.send_{kind}"\nrun(["sh", "-c", "cmux rpc surface.send_text \'{}\'"])') == [1, 2]
    assert typing_calls('run([CMUX_BIN, "send", "--workspace", w, t])\nrun(["/x/cmux", "send-key", "Enter"])') == [1, 2]
    assert typing_calls('run(f"cmux send --workspace {w} hi", shell=True)\nrun("cmux send-key Enter")') == [1, 2]
    assert typing_calls('run(["cmux", "send-text", t])\nrun(["cmux", "send-panel", t])') == [1, 2]
    assert typing_calls('run(["tmux", "send-keys", "-t", t, "x"])\nrun("tmux send-keys -t x")') == [1, 2]
    assert typing_calls('run(["osascript", "-e", \'tell app "System Events" to keystroke "x"\'])') == [1]
    assert typing_calls('_cmux_rpc(method, params)\n_cmux_rpc(paths, name, {})\nrun_cmux("rpc", m, "{}")\n'
                        'rpc(method + "_text", {})') == [1, 2, 3, 4]


def test_the_fence_ignores_docs_literal_reads_transports_and_other_calls():
    assert typing_calls('def f():\n    """Types with cmux send --workspace and surface.send_key."""\n') == []
    assert typing_calls('run([CMUX_BIN, "tree", "--all"])\nsay("cmux sends an alert")\nsend = ["send", "me"]') == []
    assert typing_calls('run([CMUX_BIN, "new-workspace", "--command", c])\nrun(["git", "send-key"])') == [2]
    assert typing_calls('x = "send-text-to-session"\ny = {"rpc_send_key_failed": 1}') == []
    assert typing_calls('_cmux_rpc("surface.read_text", {})\n_cmux_rpc(paths, "surface.read_text", {})') == []
    assert typing_calls('def transport(method, params):\n    return _cmux_rpc(paths, method, params)') == []
    assert typing_calls('def rpc(method, payload):\n    return run(method)') == []
