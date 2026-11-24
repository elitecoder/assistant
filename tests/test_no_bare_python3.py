"""A launchd service's PATH can put the system Python 3.9 first, so a subprocess
argument list must name the running interpreter, not the bare word "python3"."""
import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
FOLDERS = ("bin", "src", "work_queue", "subsystems", "hooks")


def bare_python3(source: str) -> list[int]:
    return [node.lineno for node in ast.walk(ast.parse(source))
            if isinstance(node, (ast.List, ast.Tuple)) and node.elts
            and isinstance(node.elts[0], ast.Constant) and node.elts[0].value == "python3"]


def test_no_subprocess_argument_list_starts_with_the_bare_word_python3():
    found = [f"{path.relative_to(REPO)}:{line}"
             for folder in FOLDERS for path in sorted((REPO / folder).rglob("*.py"))
             for line in bare_python3(path.read_text())]
    assert found == [], "use sys.executable instead of \"python3\": " + ", ".join(found)


def test_the_fence_sees_both_list_and_tuple_forms_and_ignores_other_uses():
    assert bare_python3('run(["python3", "x"])\nrun(("python3", "x"))') == [1, 2]
    assert bare_python3('run([sys.executable, "python3"])\nname = "python3"\nrun([])') == []
