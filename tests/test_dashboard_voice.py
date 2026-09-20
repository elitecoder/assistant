"""Keep dashboard-owned wording clear without changing quoted session evidence."""

from __future__ import annotations

import ast
import re
from pathlib import Path

from assistant import session_guidance


REPO = Path(__file__).resolve().parents[1]
INTERNAL_WORDS = re.compile(
    r"\b(snapshot|canonical|verified|unverified|provenance|corpus|invariant|handoff|reverify)\b"
    r"|\bclose-out\b",
    re.IGNORECASE,
)


def state_constants(expression):
    if isinstance(expression, ast.Constant) and isinstance(expression.value, str):
        return {expression.value}
    if isinstance(expression, ast.IfExp):
        return state_constants(expression.body) | state_constants(expression.orelse)
    return set()


def test_each_guidance_state_has_a_plain_display_label():
    tree = ast.parse(Path(session_guidance.__file__).read_text())
    states = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg == "state":
            states.update(state_constants(node.value))
    renderer = ast.parse((REPO / "bin/render-assistant-page.py").read_text())
    overview = next(node for node in renderer.body
                    if isinstance(node, ast.FunctionDef) and node.name == "overview_cards")
    for node in ast.walk(overview):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if isinstance(target, ast.Name) and target.id == "state":
            states.update(state_constants(node.value))
        if isinstance(target, ast.Tuple) and isinstance(node.value, ast.Tuple):
            for name, value in zip(target.elts, node.value.elts):
                if isinstance(name, ast.Name) and name.id == "state":
                    states.update(state_constants(value))
    assert states
    assert states <= session_guidance.STATE_LABELS.keys(), states - session_guidance.STATE_LABELS.keys()
    for label in session_guidance.STATE_LABELS.values():
        assert not INTERNAL_WORDS.search(label), label
        assert "!" not in label
        assert len(label.split()) <= 9


def test_display_labels_do_not_change_machine_states():
    assert session_guidance.STATE_LABELS["Review close-out"] == "Check before closing"
    assert session_guidance.STATE_LABELS["Status unknown"] == "Not checked yet"
    assert session_guidance.STATE_LABELS["Question waiting for you"] == "Your answer is needed"
    assert "close_candidate" in session_guidance.RECOMMENDATIONS
