"""Trace ids are minted once and never derived; a Work queue row's pointer stands in for its link."""
from __future__ import annotations

import ast
import hashlib
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SOURCE = REPO / "src/assistant/pointers.py"
sys.path.insert(0, str(REPO / "src"))

from assistant import pointers  # noqa: E402

LINK = "https://example.slack.com/archives/C0123456789/p1700000000000100?token=" + "-".join(["xoxb", "1", "2", "abc"])


def test_a_new_trace_id_has_one_shape_and_is_never_the_same_twice():
    made = {pointers.new_trace_id() for _ in range(2000)}
    assert len(made) == 2000
    assert all(re.fullmatch(r"tr-[0-9a-f]{16}", one) for one in made)
    assert all(pointers.is_trace_id(one) for one in made)


def test_a_new_trace_id_comes_from_the_systems_randomness_not_a_seeded_generator(monkeypatch):
    monkeypatch.setattr(pointers.secrets, "token_hex", lambda n: "ab" * n)
    assert pointers.new_trace_id() == "tr-abababababababab"


def test_a_legacy_trace_id_is_the_same_every_time_and_marked_as_not_minted():
    found = pointers.legacy_trace_id("task", "td-012")
    assert found == "tl-" + hashlib.sha256(b"task\0td-012").hexdigest()[:16]
    assert found == pointers.legacy_trace_id("task", "td-012") and pointers.is_trace_id(found)
    assert found != pointers.legacy_trace_id("task", "td-013") and found != pointers.legacy_trace_id("row", "td-012")
    assert pointers.legacy_trace_id("a", "bc") != pointers.legacy_trace_id("ab", "c"), "kind and key never run together"
    assert pointers.is_trace_id(pointers.legacy_trace_id("task", "caf\udce9"))


@pytest.mark.parametrize("value", [
    "", "tr-", "tr-abc", "tr-" + "g" * 16, "tr-" + "A" * 16, "tr-" + "a" * 17, " tr-" + "a" * 16, "tr-" + "a" * 16 + "\n",
    "tx-" + "a" * 16, "row:" + "a" * 16, None, 7, b"tr-aaaaaaaaaaaaaaaa", ["tr-" + "a" * 16],
])
def test_anything_else_is_not_a_trace_id(value):
    assert pointers.is_trace_id(value) is False


def test_a_row_pointer_is_a_short_id_with_nothing_of_the_link_in_it():
    found = pointers.row_pointer(LINK)
    assert found == "row:" + hashlib.sha256(LINK.encode()).hexdigest()[:16]
    for part in ("example", "slack", "archives", "C0123456789", "token", "xoxb"):
        assert part not in found


def test_the_same_row_gets_the_same_pointer_and_another_row_a_different_one():
    assert pointers.row_pointer(LINK) == pointers.row_pointer(LINK)
    assert pointers.row_pointer(LINK) != pointers.row_pointer(LINK + "1")
    assert pointers.row_pointer("") != pointers.row_pointer(" ")


def test_a_pointer_is_the_same_in_another_process():
    code = "import sys; from assistant import pointers; print(pointers.row_pointer(sys.argv[1]))"
    out = subprocess.run([sys.executable, "-c", code, LINK], capture_output=True, text=True, check=True,
                         env={"PYTHONPATH": str(REPO / "src"), "PYTHONHASHSEED": "random"})
    assert out.stdout.strip() == pointers.row_pointer(LINK)


def test_a_key_that_isnt_clean_text_still_gets_a_pointer():
    assert re.fullmatch(r"row:[0-9a-f]{16}", pointers.row_pointer("caf\udce9"))
    assert re.fullmatch(r"row:[0-9a-f]{16}", pointers.row_pointer(None))


def test_the_module_loads_on_the_oldest_python_a_service_can_fall_back_to():
    tree = ast.parse(SOURCE.read_text(), feature_version=(3, 9))
    imported = {name.name for node in ast.walk(tree) if isinstance(node, ast.Import) for name in node.names} | \
               {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert imported == {"__future__", "hashlib", "re", "secrets"}, "standard library only, nothing that reaches the network"


@pytest.mark.skipif(not Path("/usr/bin/python3").exists(), reason="no system Python on this machine")
def test_the_systems_own_python_can_use_it():
    code = ("from assistant import pointers as p; t = p.new_trace_id(); "
            "print(p.is_trace_id(t), p.is_trace_id(p.legacy_trace_id('a', 'b')), p.row_pointer('x'))")
    out = subprocess.run(["/usr/bin/python3", "-c", code], capture_output=True, text=True,
                         env={"PYTHONPATH": str(REPO / "src")})
    assert out.returncode == 0, out.stderr
    assert out.stdout.split() == ["True", "True", pointers.row_pointer("x")]
