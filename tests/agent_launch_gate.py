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
    "--allow-all-paths", "--allow-all-urls", "--autopilot", "--trust-folder",
    "--allow-tool", "--deny-tool", "--no-custom-instructions",
}
SHELLS = {"sh", "bash", "zsh"}
_TRANSCRIPT_ROOT_RE = re.compile(r"\.claude/projects|\.factory/sessions|\.copilot/session-state")
_ROOT_PARTS = {(".claude", "projects"), (".factory", "sessions"), (".copilot", "session-state")}
# Calls whose string arguments are messages or option names, not commands.
_PROSE_CALLS = {"print", "debug", "info", "warning", "warn", "error", "exception",
                "critical", "log", "add_argument", "add_parser", "write", "echo",
                "fail", "skip", "notify", "compile", "search", "match", "fullmatch",
                "findall", "finditer", "sub"}
_TYPE_FIELD_ROLES = {"assistant", "user"}
# Names only the seam may use: agent binary env vars and Claude usage fields.
_SEAM_STRINGS = {"CLAUDE_BIN", "COPILOT_BIN", "DROID_BIN", "cache_read_input_tokens",
                 "cache_creation_input_tokens", "total_cost_usd"}
_TRANSCRIPT_GLOB_RE = re.compile(r"^(projects|sessions|session-state)/")
# Text before an agent name that makes the string a command, not a sentence.
_COMMAND_LEAD_RE = re.compile(r"(^|&&|;|\||\(|['\"`]|-l?i?c|\bexec|\bcommand|\bnohup|\benv|=\S*|\{\})\s*$")
# Flags other tools share; they count only right after an agent name.
SHARED_FLAGS = {"-p", "--print", "--resume", "-r", "--model", "--continue",
                "-c", "--output-format", "--settings", "--auto", "exec",
                "--add-dir", "--mcp-config", "-C"}
HARMLESS = {"--version", "--help", "-h", "-v"}

_BIN_NAME_RE = re.compile(r"^(DEFAULT_)?(CLAUDE|COPILOT|DROID)_(BIN|EXE|CMD)$")
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
    if re.search(r"@anthropic-ai/claude-code\b", word):
        return "claude"
    if re.search(r"@github/copilot\b", word):
        return "copilot"
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
    following = text[m.end():].split()[:1] if m else []
    quoted = m and text[:m.start()].count("`") % 2 == 1
    sentence = m and not _COMMAND_LEAD_RE.search(text[:m.start()])
    if m and m.group(2) not in HARMLESS and not (following and following[0] in HARMLESS) \
            and not quoted and not sentence:
        out.append(f"agent command '{m.group(0)}'")
    if _PINNED_RE.search(text):
        out.append("pinned agent binary path")
    if _TRANSCRIPT_ROOT_RE.search(text):
        out.append("agent transcript root")
    return out


def _first_word(text: str) -> str:
    parts = text.strip().split()
    return parts[0] if parts else ""


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
        for arg in node.args:
            if _const_str(arg) in AGENTS:
                return _const_str(arg)
        return _elt_word(node.func)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        return _elt_word(node.right)
    if isinstance(node, ast.Subscript):
        return _const_str(node.slice) or ""
    if isinstance(node, ast.Starred):
        return ""
    return ""


def _argv_problem(elts: list[ast.AST], aliases: dict[str, str]) -> str | None:
    def word(e):
        if isinstance(e, ast.Starred) and isinstance(e.value, ast.Name):
            return aliases.get(e.value.id, "")
        if isinstance(e, ast.Name):
            return aliases.get(e.id, _elt_word(e))
        return _elt_word(e)
    words = [word(e) for e in elts]
    while words and (os.path.basename(words[0]) in ("env", "npx", "exec", "command", "nohup")
                     or re.fullmatch(r"[A-Za-z_]\w*=.*", words[0] or "")):
        words = words[1:]
    flags = [w for w in words if w in AGENT_FLAGS]
    if flags:
        return f"agent argv with {flags[0]}"
    for i, w in enumerate(words[:-1]):
        if w == "--command" and _agent_word(_first_word(words[i + 1])):
            return f"cmux launch of {words[i + 1]!r}"
    if words and os.path.basename(words[0]) in SHELLS:
        if any(_agent_word(_first_word(w)) for w in words[1:]):
            return "shell runs an agent"
    if words and _agent_word(words[0]) and len(words) > 1:
        rest = [w for w in words[1:] if w]
        if any(w in SHARED_FLAGS or (w.startswith("-") and len(w) > 1) for w in rest) \
                and not set(rest) <= HARMLESS:
            return f"agent argv starting {words[0]!r}"
    return None


def _joined_text(node: ast.AST) -> str | None:
    """The text a `+` chain of strings builds, "{}" for each non-literal part;
    None when no part is a literal."""
    parts, cur = [], node
    while isinstance(cur, ast.BinOp) and isinstance(cur.op, ast.Add):
        parts.insert(0, cur.right)
        cur = cur.left
    parts.insert(0, cur)
    texts = [_const_str(p) for p in parts]
    if all(t is None for t in texts):
        return None
    return "".join("{}" if t is None else t for t in texts)


def _assigned_agent(value: ast.AST, aliases: dict[str, str] | None = None) -> str | None:
    """The agent a value names: "claude", `x or "claude"`, `"droid" if c else
    "claude"`, a lookup whose default is "claude"
    (`os.environ.get("CLAUDE_BIN", "claude")`), a known alias, or a call on
    one (`shlex.quote(exe)`)."""
    aliases = aliases or {}
    if _const_str(value) in AGENTS:
        return _const_str(value)
    if isinstance(value, ast.Name):
        return aliases.get(value.id)
    if isinstance(value, ast.BoolOp):
        return next((a for v in value.values if (a := _assigned_agent(v, aliases))), None)
    if isinstance(value, ast.IfExp):
        return _assigned_agent(value.body, aliases) or _assigned_agent(value.orelse, aliases)
    if isinstance(value, ast.Call):
        if len(value.args) >= 2 and _const_str(value.args[-1]) in AGENTS:
            return _const_str(value.args[-1])
        if len(value.args) == 1 and isinstance(value.args[0], ast.Name):
            return aliases.get(value.args[0].id)
    return None


def _is_field(node: ast.AST, field: str) -> bool:
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
            and node.func.attr == "get" and node.args and _const_str(node.args[0]) == field:
        return True
    return isinstance(node, ast.Subscript) and _const_str(node.slice) == field


def _raw_role(node: ast.AST) -> bool:
    """A role read straight off a record: `m.get("role")`, or
    `rec["message"]["role"]` / `rec.get("message", {}).get("role")`, possibly
    inside a conditional expression."""
    if isinstance(node, ast.IfExp):
        return _raw_role(node.body) or _raw_role(node.orelse)
    if isinstance(node, ast.Call) and _is_field(node, "role"):
        return True
    if isinstance(node, ast.Subscript) and _const_str(node.slice) == "role":
        base = node.value
        return _is_field(base, "message")
    return False


def _is_type_field(node: ast.AST) -> bool:
    """`x.get("type")` or `x["type"]`."""
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
            and node.func.attr == "get" and node.args and _const_str(node.args[0]) == "type":
        return True
    return isinstance(node, ast.Subscript) and _const_str(node.slice) == "type"


def _path_parts_problem(parts: list[str | None]) -> str | None:
    pairs = set(zip(parts, parts[1:]))
    if pairs & _ROOT_PARTS:
        return "agent transcript root"
    if any(a == "bin" and b in AGENTS for a, b in pairs):
        return "pinned agent binary path"
    return None


def python_problems(text: str) -> list[tuple[int, str]]:
    tree = ast.parse(text)
    docs = _docstring_nodes(tree)
    found: list[tuple[int, str]] = []
    aliases = {}
    seeded = set()
    type_names = set()
    assigns = sorted((n for n in ast.walk(tree) if isinstance(n, ast.Assign)),
                     key=lambda n: (n.lineno, n.col_offset))
    for _ in range(2):
        for node in assigns:
            agent = _assigned_agent(node.value, aliases)
            if agent:
                aliases.update({t.id: agent for t in node.targets if isinstance(t, ast.Name)})
    for node in assigns:
        names = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if isinstance(node.value, ast.List) and len(node.value.elts) == 1 \
                and _const_str(node.value.elts[0]) in AGENTS:
            seeded.update(names)
            aliases.update({n: _const_str(node.value.elts[0]) for n in names})
        if _is_type_field(node.value) or _raw_role(node.value):
            type_names.update(names)
    for node in ast.walk(tree):
        if isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name) \
                and node.target.id in seeded:
            found.append((node.lineno, f"agent argv grown from {node.target.id!r}"))
    in_fstring: set[int] = set()
    prose: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            in_fstring.update(id(v) for v in node.values)
        if isinstance(node, ast.Call) and _elt_word(node.func) in _PROSE_CALLS:
            for arg in [*node.args, *(k.value for k in node.keywords)]:
                prose.update(id(n) for n in ast.walk(arg))
        if isinstance(node, ast.keyword) and node.arg == "help":
            prose.update(id(n) for n in ast.walk(node.value))
        if isinstance(node, ast.Raise) and node.exc is not None:
            prose.update(id(n) for n in ast.walk(node.exc))
    for node in ast.walk(tree):
        line = getattr(node, "lineno", 0)
        if isinstance(node, (ast.List, ast.Tuple)):
            why = _argv_problem(node.elts, aliases)
            if why:
                found.append((line, why))
        elif isinstance(node, (ast.Constant, ast.JoinedStr)):
            if id(node) in docs or id(node) in in_fstring or id(node) in prose:
                continue
            s = _const_str(node)
            if isinstance(node, ast.JoinedStr):
                s = "".join(
                    v.value if isinstance(v, ast.Constant)
                    else aliases.get(v.value.id, "{}") if isinstance(v, ast.FormattedValue)
                    and isinstance(v.value, ast.Name) else "{}"
                    for v in node.values)
            if s:
                found += [(line, why) for why in _string_problems(s)]
            if s in _SEAM_STRINGS:
                found.append((line, f"{s} belongs to agent_session"))
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            parts = []
            cur = node
            while isinstance(cur, ast.BinOp) and isinstance(cur.op, ast.Div):
                parts.insert(0, _const_str(cur.right))
                cur = cur.left
            parts.insert(0, _const_str(cur))
            why = _path_parts_problem(parts)
            if why:
                found.append((line, why))
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add) \
                and id(node) not in prose:
            joined = _joined_text(node)
            if joined:
                found += [(line, f"joined {why}") for why in _string_problems(joined)]
            if isinstance(node.left, (ast.List, ast.Tuple)) or isinstance(node.right, (ast.List, ast.Tuple)):
                elts = [*(node.left.elts if isinstance(node.left, (ast.List, ast.Tuple)) else [node.left]),
                        *(node.right.elts if isinstance(node.right, (ast.List, ast.Tuple)) else [node.right])]
                why = _argv_problem(elts, aliases)
                if why:
                    found.append((line, f"joined {why}"))
        elif isinstance(node, ast.Compare) and (
                _is_type_field(node.left) or _raw_role(node.left)
                or (isinstance(node.left, ast.Name) and node.left.id in type_names)):
            values = set()
            for comp in node.comparators:
                for c in (comp.elts if isinstance(comp, (ast.Tuple, ast.List, ast.Set)) else [comp]):
                    values.add(_const_str(c))
            if values & _TYPE_FIELD_ROLES:
                found.append((line, "reads a transcript role by agent schema; use agent_session.record_role"))
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                name = t.id if isinstance(t, ast.Name) else getattr(t, "attr", "")
                if _BIN_NAME_RE.match(name or ""):
                    found.append((line, f"per-file agent binary {name}"))
        elif isinstance(node, ast.Call):
            fname = _elt_word(node.func)
            if fname in ("Path", "PurePath", "PosixPath"):
                why = _path_parts_problem([_const_str(a) for a in node.args])
                if why:
                    found.append((line, why))
            if isinstance(node.func, ast.Attribute):
                attr = node.func.attr
                base = _const_str(node.func.value)
                if attr == "format" and base is not None and id(node) not in prose:
                    filled = base
                    for arg in node.args:
                        filled = filled.replace("{}", _const_str(arg) or "<>", 1)
                    found += [(line, f"formatted {why}") for why in _string_problems(filled)]
                if attr in ("glob", "rglob") and node.args \
                        and _TRANSCRIPT_GLOB_RE.match(_const_str(node.args[0]) or ""):
                    found.append((line, "agent transcript root"))
                if attr in ("joinpath", "join"):
                    why = _path_parts_problem(
                        [_const_str(node.func.value), *(_const_str(a) for a in node.args)]
                        if attr == "joinpath" else [_const_str(a) for a in node.args])
                    if why:
                        found.append((line, why))
                if attr in ("append", "extend", "insert") and isinstance(node.func.value, ast.Name) \
                        and node.func.value.id in seeded:
                    found.append((line, f"agent argv grown from {node.func.value.id!r}"))
            if fname in _EXEC_FUNCS and node.args:
                first = node.args[0]
                word = _elt_word(first)
                if isinstance(first, ast.Name) and first.id in aliases:
                    word = aliases[first.id]
                if isinstance(first, (ast.List, ast.Tuple)):
                    continue
                if _agent_word(word) and (fname == "which" or fname.startswith(("exec", "spawn"))
                                          or _const_str(first) is not None):
                    found.append((line, f"{fname}({word!r}) runs an agent"))
    return found


# ── shell ─────────────────────────────────────────────────────────────────────

_SEPARATORS = {";", "&&", "||", "|", "&", "(", ")", "$(", "`", "{", "}", "\n",
               "then", "do", "else", "elif", "!", "|&", ";;"}
_PREFIX_WORDS = {"command", "exec", "nohup", "time", "env", "sudo", "builtin", "xargs"}
_PROSE_COMMANDS = {"echo", "printf", "grep", "rg", "egrep", "fgrep", "sed", "awk",
                   "test", "[", "[[", "note", "warn", "log", "die", "say", "info"}


def _shell_words(text: str) -> list[str]:
    lex = shlex.shlex(text, posix=True, punctuation_chars=";&|()`")
    lex.whitespace = " \t\r"
    lex.commenters = "#"
    lex.wordchars += "$-./~{}=:@%+,*?[]^"
    try:
        return list(lex)
    except ValueError:
        return text.split()


def _blank_heredocs(text: str) -> str:
    """`text` with here-document bodies blanked (they're data, often help
    text); line numbers are kept."""
    out, end, keep = [], None, False
    for line in text.splitlines():
        if end is not None:
            if line.strip() == end:
                end = None
                out.append("")
            else:
                out.append(line if keep else "")
            continue
        out.append(line)
        m = re.search(r"<<-?\s*['\"]?([A-Za-z_]\w*)['\"]?", line)
        if m:
            end = m.group(1)
            first = _shell_words(line[:m.start()])
            keep = bool(first) and os.path.basename(first[0]) in SHELLS
    return "\n".join(out)


def shell_problems(text: str) -> list[tuple[int, str]]:
    text = _blank_heredocs(text)
    found = []
    aliases = {m.group(1): m.group(2) for m in re.finditer(
        r"(?:^|[\s;&|(])([A-Za-z_]\w*)=['\"]?(claude|copilot|droid)['\"]?(?=[\s;&|)]|$)", text)}
    aliases.update({m.group(1): m.group(2) for m in re.finditer(
        r"(?:^|[\s;&|(])([A-Za-z_]\w*)=['\"]?\$\((?:command -v|which) (claude|copilot|droid)\)", text)})
    for n, raw in enumerate(_join_continuations(text), 1):
        if not raw.strip():
            continue
        words = _shell_words(raw)
        at_command = True
        i = 0
        while i < len(words):
            w = words[i]
            if w in _SEPARATORS or w.endswith(("$(", "`")) or re.fullmatch(r"[;&|()`]+", w):
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
                if w in _PREFIX_WORDS:
                    while i < len(words) and words[i].startswith("-"):
                        i += 1
                continue
            if at_command and w == "eval":
                j = i + 1
                while j < len(words) and words[j] not in _SEPARATORS:
                    j += 1
                code = " ".join(words[i + 1:j])
                for alias, agent in aliases.items():
                    code = re.sub(r"\$\{?" + alias + r"\b\}?", agent, code)
                found += [(n, f"eval {why}") for _, why in shell_problems(code)]
                i = j
                at_command = False
                continue
            if at_command:
                var = re.fullmatch(r"\$\{?([A-Za-z_]\w*)\}?", w)
                agent = aliases.get(var.group(1)) if var else None
                agent = agent or _agent_word(w)
                rest = []
                j = i + 1
                while j < len(words) and words[j] not in _SEPARATORS \
                        and not re.fullmatch(r"[;&|()`]+", words[j]):
                    rest.append(words[j])
                    j += 1
                if agent and rest and not set(rest) <= HARMLESS | {">/dev/null", "2>&1"} \
                        and any(r.startswith("-") or r in SHARED_FLAGS for r in rest):
                    found.append((n, f"shell runs {agent} {' '.join(rest)[:40]}"))
                elif agent and _PINNED_RE.search(w):
                    found.append((n, f"pinned agent binary {w}"))
                if os.path.basename(w) in _PROSE_COMMANDS:
                    piped_to_shell = (j + 1 < len(words) and words[j] == "|"
                                      and os.path.basename(words[j + 1]) in SHELLS)
                    if piped_to_shell:
                        for r in rest:
                            found += [(n, f"piped {why}") for _, why in shell_problems(r)]
                    rest = []
                if os.path.basename(w) in SHELLS and any(
                        _agent_word(_first_word(r)) for r in rest if not r.startswith("-")):
                    found.append((n, "shell runs an agent"))
                for k, r in enumerate(rest):
                    if r == "--command" and k + 1 < len(rest) and _agent_word(_first_word(rest[k + 1])):
                        found.append((n, f"cmux launch of {rest[k + 1]!r}"))
                    if r in AGENT_FLAGS:
                        found.append((n, f"agent flag {r}"))
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
    for m in re.finditer(r"\b(?:const|let|var)\s+(\w+)\s*=\s*[`'\"](claude|copilot|droid)[`'\"]", code):
        call = re.search(r"\b(?:spawn|spawnSync|execFile|execFileSync|fork)\s*\(\s*" + m.group(1) + r"\b", code)
        if call:
            found.append((code.count("\n", 0, call.start()) + 1, f"spawns {m.group(2)}"))
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


# Readers that can't import bin/agent_session.py (src/ never reaches into
# bin/) keep their own role parsing; a test pins their verdicts to
# agent_session.record_role.
ROLE_PARSERS = {"src/assistant/transcript_slice.py"}


def scan(repo: Path) -> list[str]:
    problems = []
    for path in gated_files(repo):
        try:
            text = path.read_text()
        except (OSError, UnicodeDecodeError):
            continue
        rel = str(path.relative_to(repo))
        for line, why in scanner_for(path)(text):
            if rel in ROLE_PARSERS and "transcript role" in why:
                continue
            problems.append(f"{rel}:{line}: {why}")
    return problems
