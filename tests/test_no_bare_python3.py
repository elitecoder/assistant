"""A launchd service's PATH can put the system Python 3.9 first, so a subprocess
must name the running interpreter (sys.executable), not the bare word "python3".

Caught: an argument list that starts with "python3", one that goes through
env (["/usr/bin/env", "python3", ...]), and a shell=True command string that
runs python3. Left alone on purpose:
- docstrings and usage text that tell a person what to type;
- remedies shown to the user ("run: python3 bin/assistant-llm.py ...");
- work_queue/server.py's RESULT_CMD, a command line handed to an agent session
  to type in the user's own shell, not something a service runs;
- bin/tools/mem0_backend.py's VENV_PYTHON, a path to the Python in the mem0 venv;
- shell scripts, plists and install.sh, which this Python scan doesn't read.
"""
import ast
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
FOLDERS = ("bin", "src", "work_queue", "subsystems", "hooks")
SHELL_PYTHON = re.compile(r"(^|[\s;&|(`])python3(\s|$)")
ENV = {"env", "/usr/bin/env"}


def _text(node):
    """The leading literal text of a string or f-string, else None."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr) and node.values and isinstance(node.values[0], ast.Constant):
        return node.values[0].value
    return None


def bare_python3(source: str) -> list[int]:
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.List, ast.Tuple)) and node.elts:
            words = [e.value if isinstance(e, ast.Constant) else None for e in node.elts]
            if words[0] == "python3" or (words[0] in ENV and "python3" in words[1:3]):
                found.append(node.lineno)
        elif isinstance(node, ast.Call) and node.args and any(
                k.arg == "shell" and isinstance(k.value, ast.Constant) and k.value.value is True
                for k in node.keywords):
            text = _text(node.args[0])
            if text is not None and SHELL_PYTHON.search(text):
                found.append(node.lineno)
    return found


def test_no_subprocess_argument_list_starts_with_the_bare_word_python3():
    found = [f"{path.relative_to(REPO)}:{line}"
             for folder in FOLDERS for path in sorted((REPO / folder).rglob("*.py"))
             for line in bare_python3(path.read_text())]
    assert found == [], "use sys.executable instead of \"python3\": " + ", ".join(found)


def test_the_fence_sees_lists_tuples_env_and_shell_strings_and_ignores_other_uses():
    assert bare_python3('run(["python3", "x"])\nrun(("python3", "x"))') == [1, 2]
    assert bare_python3('run(["/usr/bin/env", "python3", "x"])\nrun(["env", "-S", "python3", "x"])') == [1, 2]
    assert bare_python3('run("python3 x.py", shell=True)\nrun(f"cd {d} && python3 x.py", shell=True)\n'
                        'run(f"python3 {x}", shell=True)') == [1, 3]
    assert bare_python3('run([sys.executable, "python3"])\nname = "python3"\nrun([])\n'
                        'run("python3 x.py")\nrun("echo python3x", shell=True)\nrun(f"{x} python3", shell=True)\n'
                        'run(["env", "FOO=1", "node"])\nrun(shell_cmd, shell=True)\nrun(["git"], shell=False)') == []
