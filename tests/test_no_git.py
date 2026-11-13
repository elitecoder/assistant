"""Assistant code never runs git.

Scans every Python, shell, and Swift file Assistant runs (bin/, src/, hooks/,
slack-reactor/, mac/Sources/) for a git command. PENDING lists files still
waiting on a replacement; remove each entry once its replacement lands.
ALLOWED lists files approved to keep running git.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
ROOTS = ("bin", "src", "hooks", "slack-reactor", "mac/Sources", "githooks")
PENDING = {"bin/self_update.py", "githooks/refuse-main-commit"}
ALLOWED = {"bin/assistant-curator.py"}
SHELL_GIT = re.compile(r"(^|[\s;&|(`]|\$\()git\s+-?[A-Za-z]")
SWIFT_GIT = re.compile(r'"(/usr/bin/)?git"')


def _is_git(node: ast.AST) -> bool:
    return (isinstance(node, ast.Constant) and isinstance(node.value, str)
            and Path(node.value).name == "git")


def python_git_calls(source: str) -> list[int]:
    """Lines where a command list starts with git, or a shell string runs git."""
    lines = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.List, ast.Tuple)) and node.elts and _is_git(node.elts[0]):
            lines.append(node.lineno)
        elif isinstance(node, ast.Call):
            shell = any(k.arg == "shell" and isinstance(k.value, ast.Constant)
                        and k.value.value is True for k in node.keywords)
            first = node.args[0] if node.args else None
            if (shell and isinstance(first, ast.Constant) and isinstance(first.value, str)
                    and SHELL_GIT.search(first.value)):
                lines.append(node.lineno)
    return lines


def shell_git_calls(source: str) -> list[int]:
    return [n for n, line in enumerate(source.splitlines(), 1)
            if not line.lstrip().startswith("#") and SHELL_GIT.search(line)
            and not re.match(r"\s*(echo|printf|note|log|warn|die)\b", line)]


def git_calls(path: Path) -> list[int]:
    text = path.read_text(errors="replace")
    if path.suffix == ".py" or text.startswith("#!") and "python" in text.split("\n", 1)[0]:
        return python_git_calls(text)
    if path.suffix == ".swift":
        return [n for n, line in enumerate(text.splitlines(), 1)
                if not line.lstrip().startswith("//") and SWIFT_GIT.search(line)]
    if path.suffix == ".sh" or text.startswith("#!"):
        return shell_git_calls(text)
    return []


def assistant_files():
    for root in ROOTS:
        base = REPO / root
        if base.is_dir():
            yield from (p for p in sorted(base.rglob("*"))
                        if p.is_file() and "__pycache__" not in p.parts)


def test_no_assistant_file_runs_git():
    found = {}
    for path in assistant_files():
        rel = str(path.relative_to(REPO))
        if rel in PENDING | ALLOWED:
            continue
        try:
            lines = git_calls(path)
        except (SyntaxError, UnicodeDecodeError):
            continue
        if lines:
            found[rel] = lines
    assert found == {}, f"Assistant files run git: {found}"


@pytest.mark.parametrize("source", [
    'subprocess.run(["git", "status"])',
    'subprocess.run(("/usr/bin/git", "-C", d, "log"))',
    'subprocess.run("cd x && git status --porcelain", shell=True)',
])
def test_python_git_calls_are_found(source):
    assert python_git_calls(source) == [1]


@pytest.mark.parametrize("source", [
    'subprocess.run(["gh", "pr", "view"])',
    'print("run git status yourself")',
    'subprocess.run("echo git status", shell=False)',
])
def test_python_non_git_calls_pass(source):
    assert python_git_calls(source) == []


def test_shell_git_calls_are_found():
    src = "#!/bin/sh\n# git status in a comment\necho 'try git pull'\nbranch=$(git rev-parse HEAD)\n  git fetch\n"
    assert shell_git_calls(src) == [4, 5]
