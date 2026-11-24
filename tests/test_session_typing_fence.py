"""Nothing in Assistant's own code types into an agent session except src/assistant/session_effects.py.

That module writes the intent to the effects ledger before the first keystroke, so a typing that isn't in the
ledger can't happen from here. The calls that type are `surface.send_text` and `surface.send_key` (cmux rpc),
and `cmux send` / `cmux send-key`. Caught: a string that names an rpc method, an argument list that runs
`cmux send` or `send-key`, and a command string that runs it. Left alone on purpose: docstrings and comments that
describe those calls; the `spawn-claude-workspace` skill's shell snippet and the warm session's own model, which
are not Python and are not recorded yet (docs/effects.md says so); cmux's `new-workspace --command`, which starts
a session with a command instead of typing.
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
METHODS = {"surface.send_text", "surface.send_key"}
VERBS = {"send", "send-key"}
SHELL = re.compile(r"\bcmux\s+send(-key)?(\s+[-<$\"']|\s+[A-Z]\w*\s*$|\s*$)")


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


def typing_calls(source: str) -> list:
    """Line numbers of code that types into a session."""
    tree = ast.parse(source)
    docs, found = _doc_nodes(tree), []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs:
            if node.value in METHODS or SHELL.search(node.value):
                found.append(node.lineno)
        elif isinstance(node, (ast.List, ast.Tuple)) and node.elts:
            words = [e.value for e in node.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]
            if VERBS & set(words) and any(_mentions_cmux(e) for e in node.elts):
                found.append(node.lineno)
    return sorted(set(found))


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


def test_the_fence_sees_rpc_names_argument_lists_and_shell_strings_and_ignores_docs_and_other_calls():
    assert typing_calls('rpc("surface.send_text", {})\nx = "surface.send_key"') == [1, 2]
    assert typing_calls('run([CMUX_BIN, "send", "--workspace", w, t])\nrun(["/x/cmux", "send-key", "Enter"])') == [1, 2]
    assert typing_calls('run(f"cmux send --workspace {w} hi", shell=True)\nrun("cmux send-key Enter")') == [1, 2]
    assert typing_calls('def f():\n    """Types with cmux send --workspace and surface.send_key."""\n') == []
    assert typing_calls('run([CMUX_BIN, "tree", "--all"])\nsay("cmux sends an alert")\nsend = ["send", "me"]') == []
    assert typing_calls('run([CMUX_BIN, "new-workspace", "--command", c])\nrun(["git", "send-key"])') == []
