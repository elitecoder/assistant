"""Finds code outside bin/agent_session.py that builds a coding-agent command.

Python is read as a syntax tree; shell, skill code blocks, and workflow files
are split into shell words; JavaScript and launchd files are matched after
their comments are removed. Prose (comments, docstrings, Markdown text) never
counts.
"""
from __future__ import annotations

import ast
import os
import re
import shlex
import subprocess
from pathlib import Path

AGENTS = ("claude", "copilot", "droid")

# Flags only a coding agent takes. Any of them in code means an agent command.
AGENT_FLAGS = {
    "--dangerously-skip-permissions", "--permission-mode", "--allow-all",
    "--allow-all-tools", "--available-tools", "--yolo", "--json-schema",
    "--strict-mcp-config", "--list-tools", "--append-system-prompt-file",
    "--disable-builtin-mcps", "--disable-slash-commands", "--no-ask-user",
}
# Flags other tools share; they count only right after an agent name.
SHARED_FLAGS = {"-p", "--print", "--resume", "-r", "--model", "--continue",
                "-c", "--output-format", "--settings", "--auto", "exec",
                "--add-dir", "--mcp-config", "-C"}
HARMLESS = {"--version", "--help", "-h", "-v"}

_BIN_NAME_RE = re.compile(r"^(DEFAULT_)?(CLAUDE|COPILOT|DROID)_(BIN|EXE|PATH|CMD)$")
_PINNED_RE = re.compile(r"(\.local/bin|/opt/homebrew/bin|/usr/local/bin|\.nvm/[^\s'\"]*/bin)/(claude|copilot|droid)\b")
_FLAG_TOKEN_RE = re.compile(r"(?<![\w-])(--[a-z][a-z-]*)")
_AGENT_THEN_FLAG_RE = re.compile(
    r"(?<![\w./-])(?:[\w./~${}-]*/)?(claude|copilot|droid)['\"]?\s+(-{1,2}[A-Za-z][\w-]*|exec\b)")
_EXEC_FUNCS = {"run", "Popen", "call", "check_call", "check_output", "spawn",
               "execv", "execvp", "execvpe", "execl", "execlp", "execle",
               "execlpe", "spawnv", "spawnvp", "spawnl", "spawnlp", "system",
               "popen", "create_subprocess_exec", "create_subprocess_shell",
               "getoutput", "getstatusoutput", "which"}


def _agent_word(word: str) -> str | None:
    """The agent a shell word or path names (`$CLAUDE_BIN`, `~/x/copilot`)."""
    base = os.path.basename(word.strip("'\"{}$()").rstrip("}"))
    low = base.lower()
    if low in AGENTS:
        return low
    m = re.fullmatch(r"(?:default_)?(claude|copilot|droid)(?:_(?:bin|exe|path|cmd))?", low)
    return m.group(1) if m else None


def _flags_in(text: str) -> set[str]:
    return set(_FLAG_TOKEN_RE.findall(text)) & AGENT_FLAGS


def _string_problems(text: str) -> list[str]:
    out = []
    if _flags_in(text):
        out.append(f"agent flag {sorted(_flags_in(text))[0]}")
    m = _AGENT_THEN_FLAG_RE.search(text)
    if m and m.group(2) not in HARMLESS:
        out.append(f"agent command '{m.group(0)}'")
    if _PINNED_RE.search(text):
        out.append("pinned agent binary path")
    return out


# ── Python ────────────────────────────────────────────────────────────────────

def _docstring_nodes(tree: ast.AST) -> set[int]:
    ids = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(body, list) and body and isinstance(body[0], ast.Expr) \
                and isinstance(body[0].value, ast.Constant) \
                and isinstance(body[0].value.value, str):
            ids.add(id(body[0].value))
    return ids


def _const_str(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(v.value if isinstance(v, ast.Constant) else "{}"
                       for v in node.values)
    return None


def _elt_word(node: ast.AST) -> str:
    s = _const_str(node)
    if s is not None:
        return s
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Call):
        return _elt_word(node.func)
    if isinstance(node, ast.Starred):
        return ""
    return ""


def _argv_problem(elts: list[ast.AST]) -> str | None:
    words = [_elt_word(e) for e in elts]
    flags = [w for w in words if w in AGENT_FLAGS]
    if flags:
        return f"agent argv with {flags[0]}"
    if words and _agent_word(words[0]) and len(words) > 1:
        rest = [w for w in words[1:] if w]
        if any(w in SHARED_FLAGS or w.startswith("-") for w in rest) \
                and not set(rest) <= HARMLESS:
            return f"agent argv starting {words[0]!r}"
    return None


def python_problems(text: str) -> list[tuple[int, str]]:
    tree = ast.parse(text)
    docs = _docstring_nodes(tree)
    found: list[tuple[int, str]] = []
    in_fstring: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            in_fstring.update(id(v) for v in node.values)
    for node in ast.walk(tree):
        line = getattr(node, "lineno", 0)
        if isinstance(node, (ast.List, ast.Tuple)):
            why = _argv_problem(node.elts)
            if why:
                found.append((line, why))
        elif isinstance(node, (ast.Constant, ast.JoinedStr)):
            if id(node) in docs or id(node) in in_fstring:
                continue
            s = _const_str(node)
            if s:
                found += [(line, why) for why in _string_problems(s)]
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                name = t.id if isinstance(t, ast.Name) else getattr(t, "attr", "")
                if _BIN_NAME_RE.match(name or ""):
                    found.append((line, f"per-file agent binary {name}"))
        elif isinstance(node, ast.Call):
            fname = _elt_word(node.func)
            if fname in _EXEC_FUNCS and node.args:
                first = node.args[0]
                word = _elt_word(first)
                if isinstance(first, (ast.List, ast.Tuple)):
                    continue
                if _agent_word(word) and (fname == "which" or fname.startswith(("exec", "spawn"))
                                          or _const_str(first) is not None):
                    found.append((line, f"{fname}({word!r}) runs an agent"))
    return found


# ── shell ─────────────────────────────────────────────────────────────────────

_SEPARATORS = {";", "&&", "||", "|", "&", "(", ")", "$(", "`", "{", "}", "\n",
               "then", "do", "else", "elif", "!", "|&", ";;"}
_PREFIX_WORDS = {"command", "exec", "nohup", "time", "env", "sudo", "builtin"}


def _shell_words(text: str) -> list[str]:
    lex = shlex.shlex(text, posix=True, punctuation_chars=";&|()`")
    lex.whitespace = " \t\r"
    lex.commenters = "#"
    lex.wordchars += "$-./~{}=:@%+,*?[]^"
    try:
        return list(lex)
    except ValueError:
        return text.split()


def shell_problems(text: str) -> list[tuple[int, str]]:
    found = []
    for n, raw in enumerate(_join_continuations(text), 1):
        if not raw.strip():
            continue
        words = _shell_words(raw)
        code = " ".join(words)
        found += [(n, why) for why in _string_problems(code) if "pinned" in why
                  or "flag" in why]
        at_command = True
        i = 0
        while i < len(words):
            w = words[i]
            if w in _SEPARATORS or w.endswith(("$(", "`")):
                at_command = True
                i += 1
                continue
            if at_command and (w in _PREFIX_WORDS or re.fullmatch(r"[A-Za-z_]\w*=.*", w, re.S)):
                if "=" in w:
                    found += [(n, f"assigned {why}")
                              for why in _string_problems(w.split("=", 1)[1])]
                i += 1
                if w == "command" and i < len(words) and words[i] == "-v":
                    at_command = False
                continue
            if at_command:
                agent = _agent_word(w)
                rest = []
                j = i + 1
                while j < len(words) and words[j] not in _SEPARATORS:
                    rest.append(words[j])
                    j += 1
                if agent and rest and not set(rest) <= HARMLESS | {">/dev/null", "2>&1"} \
                        and any(r.startswith("-") or r in SHARED_FLAGS for r in rest):
                    found.append((n, f"shell runs {agent} {' '.join(rest)[:40]}"))
                for r in rest:
                    for sub in _string_problems(r):
                        found.append((n, f"quoted {sub}"))
                at_command = False
                i = j
                continue
            i += 1
    return found


def _join_continuations(text: str) -> list[str]:
    """Lines with trailing-backslash continuations folded into the first line;
    the folded lines become blank so numbering stays put."""
    out: list[str] = []
    pending = None
    for line in text.splitlines():
        if pending is not None:
            out.append("")
            pending += " " + line.rstrip("\\")
            if not line.endswith("\\"):
                out[pending_idx] = pending
                pending = None
            continue
        if line.endswith("\\"):
            pending_idx = len(out)
            out.append("")
            pending = line[:-1]
        else:
            out.append(line)
    if pending is not None:
        out[pending_idx] = pending
    return out


def markdown_problems(text: str) -> list[tuple[int, str]]:
    found = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        m = re.match(r"^\s*```\s*(bash|sh|zsh|shell|console)?\s*$", lines[i])
        if m and m.group(1):
            start = i + 1
            j = start
            while j < len(lines) and not lines[j].strip().startswith("```"):
                j += 1
            block = "\n".join(lines[start:j])
            found += [(start + n, why) for n, why in shell_problems(block)]
            i = j + 1
            continue
        i += 1
    return found


# ── JavaScript, launchd ───────────────────────────────────────────────────────

_JS_COMMENT_RE = re.compile(r"/\*.*?\*/|(?<![:\"'`\\])//[^\n]*", re.S)
_JS_STRING_RE = re.compile(r"'(?:\\.|[^'\\\n])*'|\"(?:\\.|[^\"\\\n])*\"|`(?:\\.|[^`\\])*`", re.S)
_JS_SPAWN_RE = re.compile(
    r"\b(?:spawn|spawnSync|execFile|execFileSync|exec|execSync|fork)\s*\(\s*[`'\"]"
    r"(?:[^`'\"]*/)?(claude|copilot|droid)[`'\"\s]")


def js_problems(text: str) -> list[tuple[int, str]]:
    code = _JS_COMMENT_RE.sub(lambda m: "\n" * m.group(0).count("\n"), text)
    found = []
    for m in _JS_SPAWN_RE.finditer(code):
        found.append((code.count("\n", 0, m.start()) + 1, f"spawns {m.group(1)}"))
    for m in _JS_STRING_RE.finditer(code):
        body = re.sub(r"\$\{[^}]*\}", "{}", m.group(0)[1:-1])
        for why in _string_problems(body):
            found.append((code.count("\n", 0, m.start()) + 1, why))
    for m in re.finditer(r"\[\s*[`'\"](claude|copilot|droid)[`'\"]\s*,\s*[`'\"]-", code):
        found.append((code.count("\n", 0, m.start()) + 1, f"agent argv starting {m.group(1)}"))
    return found


def plist_problems(text: str) -> list[tuple[int, str]]:
    code = re.sub(r"<!--.*?-->", lambda m: "\n" * m.group(0).count("\n"), text, flags=re.S)
    found = []
    for m in re.finditer(r"<string>([^<]*)</string>", code):
        line = code.count("\n", 0, m.start()) + 1
        value = m.group(1)
        if _agent_word(value.strip()) and "/" in value or value.strip() in AGENTS:
            found.append((line, f"launchd runs {value.strip()}"))
        found += [(line, why) for why in _string_problems(value)]
    return found


# ── which files, and the scan ─────────────────────────────────────────────────

SEAM = "bin/agent_session.py"


def gated_files(repo: Path) -> list[Path]:
    out = subprocess.run(["git", "ls-files"], cwd=repo, capture_output=True,
                         text=True, check=True).stdout.split()
    files = []
    for rel in out:
        if rel == SEAM or rel.startswith(("tests/", "docs/")):
            continue
        if "/reports/" in rel or "/fixtures/" in rel:
            continue
        if scanner_for(repo / rel):
            files.append(repo / rel)
    return files


def scanner_for(path: Path):
    suffix = path.suffix
    if suffix == ".py":
        return python_problems
    if suffix in (".sh", ".bash", ".zsh", ".yml", ".yaml"):
        return shell_problems
    if suffix in (".js", ".mjs", ".cjs", ".ts"):
        return js_problems
    if suffix == ".plist":
        return plist_problems
    if suffix == ".md" and (path.name == "SKILL.md" or "prompts" in path.parts
                            or "skills" in path.parts or "commands" in path.parts):
        return markdown_problems
    if suffix == "":
        try:
            first = path.read_text(errors="replace").split("\n", 1)[0]
        except OSError:
            return None
        if first.startswith("#!") and "python" in first:
            return python_problems
        if first.startswith("#!") and re.search(r"\b(ba|z)?sh\b", first):
            return shell_problems
    return None


def scan(repo: Path) -> list[str]:
    problems = []
    for path in gated_files(repo):
        try:
            text = path.read_text()
        except (OSError, UnicodeDecodeError):
            continue
        for line, why in scanner_for(path)(text):
            problems.append(f"{path.relative_to(repo)}:{line}: {why}")
    return problems
