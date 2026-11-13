"""Assistant code never runs git.

Scans every Python, shell, JavaScript, Swift, and LaunchAgent file Assistant
runs for a git command. APPROVED maps the files allowed to run git to their
exact number of git lines; any other file, or a new git line in an approved
file, fails.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
ROOTS = ("bin", "src", "hooks", "slack-reactor", "mac/Sources", "githooks",
         "launchagents", "scripts")
APPROVED = {
    "bin/assistant-curator.py": 1,
    "bin/assistant-doctor.py": 2,
    "bin/self_update.py": 5,
    "githooks/refuse-main-commit": 1,
}
GIT_PATH = r"(?:/[\w.+-]+)*/git"
GIT_SUBCOMMANDS = (
    "add am apply bisect blame branch cat-file checkout cherry-pick clean clone "
    "commit config describe diff fetch for-each-ref gc grep init log ls-files "
    "ls-remote merge merge-base merge-tree mv notes pull push rebase reflog "
    "remote reset restore rev-list rev-parse revert rm show stash status "
    "submodule switch symbolic-ref tag update-index update-ref worktree").split()
SUBCOMMAND = (r"\s+(?:-{1,2}[A-Za-z][\w-]*|"
              + "|".join(map(re.escape, GIT_SUBCOMMANDS))
              + r""")(?=[\s;&|)'"`]|$)""")
GIT_COMMAND = re.compile(
    rf"""(?:^|[\s;&|(`'"\\=]|\$\()(?:{GIT_PATH}|git)['"]?{SUBCOMMAND}""")
PYTHON_GIT_COMMAND = re.compile(
    rf"(?:^|&&|\|\||[;|(`]|\$\()\s*(?:{GIT_PATH}|git){SUBCOMMAND}")
GIT_NAME = re.compile(
    rf"""=\s*['"]?(?:{GIT_PATH}|git)['"]?(?:\s|;|$)|\b(?:command\s+-v|which|type)\s+git\b""")
QUOTED_GIT = re.compile(rf"""(['"`])(?:{GIT_PATH}|git)\1""")
QUOTED_GIT_COMMAND = re.compile(
    rf"""(['"`])\s*(?:{GIT_PATH}|git){SUBCOMMAND}""")
PLIST_GIT = re.compile(rf"<string>\s*(?:{GIT_PATH}|git)(?:\s|</string>)")
GIT_LIBRARIES = {"git", "pygit2", "dulwich"}
SHELL_SEGMENT = re.compile(r"&&|\|\||[;|]")
SHELL_MESSAGE = re.compile(r"\s*(echo|printf|note|log|warn|die|info|error)\b")


def _docstrings(tree: ast.AST) -> set[int]:
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                found.add(id(first.value))
    return found


def python_git_calls(source: str) -> list[int]:
    """Lines holding a string that names git or starts a git command."""
    tree = ast.parse(source)
    skip = _docstrings(tree)
    lines = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
            if any(n.split(".")[0] in GIT_LIBRARIES for n in names):
                lines.add(node.lineno)
        elif (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and id(node) not in skip):
            value = node.value
            if Path(value).name == "git" or PYTHON_GIT_COMMAND.search(value):
                lines.add(node.lineno)
    return sorted(lines)


def shell_git_calls(source: str) -> list[int]:
    lines, start, pending = [], 0, ""
    for n, line in enumerate(source.splitlines(), 1):
        if not pending:
            start = n
        if line.rstrip().endswith("\\"):
            pending += line.rstrip()[:-1] + " "
            continue
        command, pending = pending + line, ""
        if command.lstrip().startswith("#"):
            continue
        for segment in SHELL_SEGMENT.split(command.split(" #", 1)[0]):
            if SHELL_MESSAGE.match(segment):
                continue
            if GIT_COMMAND.search(segment) or GIT_NAME.search(segment):
                lines.append(start)
                break
    return lines


def string_git_calls(source: str, comment: str) -> list[int]:
    return [n for n, line in enumerate(source.splitlines(), 1)
            if not line.lstrip().startswith(comment)
            and (QUOTED_GIT.search(line) or QUOTED_GIT_COMMAND.search(line))]


def git_calls(path: Path) -> list[int]:
    text = path.read_text(errors="replace")
    shebang = text.split("\n", 1)[0] if text.startswith("#!") else ""
    if path.suffix == ".py" or "python" in shebang or "uv run" in shebang:
        return python_git_calls(text)
    if path.suffix in (".swift", ".js", ".mjs", ".cjs", ".ts"):
        return string_git_calls(text, "//")
    if path.suffix == ".plist":
        return [n for n, line in enumerate(text.splitlines(), 1) if PLIST_GIT.search(line)]
    if path.suffix in (".sh", ".bash", ".zsh") or shebang:
        return shell_git_calls(text)
    return []


def assistant_files():
    for root in ROOTS:
        base = REPO / root
        if base.is_dir():
            yield from (p for p in sorted(base.rglob("*"))
                        if p.is_file() and "__pycache__" not in p.parts
                        and "node_modules" not in p.parts)


def test_no_assistant_file_runs_git():
    found = {}
    for path in assistant_files():
        lines = git_calls(path)
        if lines:
            found[str(path.relative_to(REPO))] = len(lines)
    assert found == APPROVED


@pytest.mark.parametrize("source", [
    'subprocess.run(["git", "status"])',
    'subprocess.run(("/usr/bin/git", "-C", d, "log"))',
    'subprocess.run("cd x && git status --porcelain", shell=True)',
    'os.system("git --no-optional-locks status")',
    'os.popen("git pull")',
    'subprocess.run(f"git -C {d} status", shell=True)',
    'GIT = "git"',
    'shutil.which("git")',
    'subprocess.run(["/usr/bin/env", "git", "status"])',
    'subprocess.run(["sh", "-c", "git status"])',
    'subprocess.run("git status".split())',
    'subprocess.run(["/opt/homebrew/bin/git", "fetch"])',
    'import git',
    'from pygit2 import Repository',
])
def test_python_git_calls_are_found(source):
    assert python_git_calls(source) == [1]


@pytest.mark.parametrize("source", [
    'subprocess.run(["gh", "pr", "view"])',
    '"""Never run git status here."""',
    'Path(".git")',
    'x = "digit status"',
    'log("cleared a stale git lock")',
    'log(f"git {args} timed out")',
    'note = "git checkout/reset/clean are destructive"',
    'label = "git on PATH"',
])
def test_python_non_git_calls_pass(source):
    assert python_git_calls(source) == []


def test_unparsable_python_fails_the_scan():
    with pytest.raises(SyntaxError):
        python_git_calls("def broken(:\n")


@pytest.mark.parametrize("source", [
    "branch=$(git rev-parse HEAD)",
    "  git fetch",
    "log 'syncing'; git pull",
    "echo hi && git push",
    '"git" status',
    "\\git status",
    "G=git",
    "G=$(command -v git)",
    "git \\\n  status",
    "/opt/homebrew/bin/git status",
    "git --no-optional-locks status",
])
def test_shell_git_calls_are_found(source):
    assert shell_git_calls(source) == [1]


@pytest.mark.parametrize("source", [
    "# git status in a comment",
    "echo 'try git pull'",
    "gh pr view 12",
    "cd .git/hooks",
])
def test_shell_non_git_calls_pass(source):
    assert shell_git_calls(source) == []


@pytest.mark.parametrize("source", [
    'execSync("git status")',
    "spawn('git', ['status'])",
    'let p = "/usr/bin/git"',
    'process.arguments = ["sh", "-c", "git status"]',
])
def test_js_and_swift_git_calls_are_found(source):
    assert string_git_calls(source, "//") == [1]


def test_plist_git_calls_are_found():
    assert [n for n, line in enumerate(
        ["<string>/usr/bin/git</string>", "<string>git pull</string>",
         "<string>/usr/bin/python3</string>"], 1) if PLIST_GIT.search(line)] == [1, 2]


@pytest.mark.parametrize("name,text", [
    ("x.zsh", "git status\n"),
    ("x", "#!/usr/bin/env -S uv run\nsubprocess.run(['git', 'status'])\n"),
    ("x.js", "execSync('git status')\n"),
    ("x.plist", "<string>git</string>\n"),
])
def test_every_file_type_is_scanned(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text)
    assert git_calls(path)
