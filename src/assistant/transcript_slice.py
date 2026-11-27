"""Plain-text conversation slice of a Claude Code, Copilot, or Droid transcript.

The Observer reads this instead of raw JSONL: the first request plus the most
recent turns, with attachments, hook output, request records, and thinking
dropped, and tool calls shortened to one line each. User records the person
didn't type (background-task notices, other sessions' messages, command
output, conversation summaries) come out as SYSTEM turns.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

HEAD_BYTES = 262144
TAIL_STEPS = (2 << 20, 8 << 20, 32 << 20)
FIRST_REQUEST_CHARS = 1500
TURN_CHARS = 1500
LAST_TURN_CHARS = 4000
TOOL_CHARS = 160
REMINDER = re.compile(r"<system[-_]reminder>.*?</system[-_]reminder>", re.S)
COMMAND = re.compile(r"<command-name>\s*([^<]*?)\s*</command-name>")
COMMAND_ARGS = re.compile(r"<command-args>(.*?)</command-args>", re.S)
SYSTEM_PREFIXES = ("<task-notification>", "<local-command-", "<bash-stdout>",
                   "<bash-stderr>", "Caveat:", "This session is being continued",
                   "Another Claude session sent a message",
                   "[System: context from Assistant")


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _tool_input(value) -> str:
    if not isinstance(value, dict):
        return ""
    for key in ("description", "command", "file_path", "pattern", "prompt", "url"):
        if isinstance(value.get(key), str):
            return value[key]
    return json.dumps(value)


def _block_text(block) -> str:
    if isinstance(block, str):
        return block
    if not isinstance(block, dict):
        return ""
    kind = block.get("type")
    if kind == "text":
        return block.get("text") or ""
    if kind == "tool_use":
        return f"[tool {block.get('name')}: {_clip(_tool_input(block.get('input')), TOOL_CHARS)}]"
    if kind == "tool_result":
        content = block.get("content")
        if isinstance(content, list):
            content = " ".join(_block_text(c) for c in content)
        label = "tool error" if block.get("is_error") else "tool result"
        return f"[{label}: {_clip(str(content or ''), TOOL_CHARS)}]"
    if kind == "image":
        return "[image]"
    return ""


def _turn(record: dict) -> tuple[str, str, str] | None:
    """(role, timestamp, text) for a conversation record, else None."""
    if record.get("isMeta"):
        return None
    kind = record.get("type")
    if kind == "system" and record.get("subtype") == "away_summary":
        recap = REMINDER.sub("", str(record.get("content") or "")).strip()
        return ("system", str(record.get("timestamp") or ""), "Recap: " + recap) if recap else None
    if kind in ("user.message", "assistant.message"):
        return _copilot_turn(record, kind.split(".", 1)[0])
    if kind == "tool.execution_complete":
        data = record.get("data")
        if not isinstance(data, dict) or data.get("parentToolCallId"):
            return None
        result = data.get("result")
        content = result.get("content") if isinstance(result, dict) else result
        error = data.get("error")
        if not content and isinstance(error, dict):
            content = error.get("message")
        return _render(record, "user", [{"type": "tool_result", "content": content,
                                         "is_error": data.get("success") is False}])
    if kind in ("session.compaction_complete", "system.notification", "abort"):
        return _copilot_notice(record, kind)
    if kind == "session.error":
        data = record.get("data")
        message = data.get("message") if isinstance(data, dict) else None
        return _render(record, "assistant", [f"[error] {message or 'session error'}"])
    message = record.get("message")
    if not isinstance(message, dict):
        return None
    if kind in ("user", "assistant"):
        role = kind
    elif kind == "message" and message.get("role") in ("user", "assistant"):
        role = message["role"]
    else:
        return None
    content = message.get("content")
    blocks = [content] if isinstance(content, str) else content or []
    return _render(record, role, blocks)


def _copilot_notice(record: dict, kind: str) -> tuple[str, str, str] | None:
    """A SYSTEM turn for Copilot's compaction summary, background notice, or
    user abort."""
    data = record.get("data") if isinstance(record.get("data"), dict) else {}
    text = {"session.compaction_complete": "Compacted: " + str(data.get("summaryContent") or ""),
            "system.notification": str(data.get("content") or ""),
            "abort": f"[aborted: {data.get('reason') or 'user'}]"}[kind]
    text = REMINDER.sub("", text).strip()
    return ("system", str(record.get("timestamp") or ""), _clip(text, TURN_CHARS)) if text else None


def _copilot_turn(record: dict, role: str) -> tuple[str, str, str] | None:
    """A Copilot events.jsonl turn: plain-text content plus its tool requests.
    A subagent's own turns (parentToolCallId) and its prompt (source
    agent-<id>) are dropped; other non-typed user text (memory, skills,
    discovered instructions) is a SYSTEM turn."""
    data = record.get("data")
    if not isinstance(data, dict) or data.get("parentToolCallId"):
        return None
    source = data.get("source")
    if role == "user" and isinstance(source, str) and source.startswith("agent-"):
        return None
    content = data.get("content")
    blocks = [content] if isinstance(content, str) else []
    blocks += [{"type": "tool_use", "name": request.get("name"),
                "input": request.get("arguments")}
               for request in data.get("toolRequests") or []
               if isinstance(request, dict)]
    turn = _render(record, role, blocks)
    if turn and role == "user" and source not in (None, "user"):
        return ("system", turn[1], turn[2])
    return turn


def _render(record: dict, role: str, blocks) -> tuple[str, str, str] | None:
    text = " ".join(t for t in (_block_text(b) for b in blocks) if t)
    text = REMINDER.sub("", text).strip()
    if not text:
        return None
    if role == "user":
        role, text = _user_turn(record, text)
    return role, str(record.get("timestamp") or ""), text


def _user_turn(record: dict, text: str) -> tuple[str, str]:
    """("system", text) for user records the person didn't type; a slash
    command becomes ("user", "/name args")."""
    origin = record.get("origin")
    origin_kind = origin.get("kind") if isinstance(origin, dict) else origin
    if (record.get("isCompactSummary") or origin_kind not in (None, "human")
            or record.get("promptSource") == "system"
            or text.startswith(SYSTEM_PREFIXES)):
        return "system", text
    if command := COMMAND.search(text):
        args = COMMAND_ARGS.search(text)
        return "user", " ".join(
            part for part in (command.group(1), (args.group(1) if args else "").strip())
            if part)
    return "user", text


def _turns(data: bytes) -> list[tuple[str, str, str]]:
    out = []
    for line in data.splitlines():
        try:
            record = json.loads(line)
            turn = _turn(record) if isinstance(record, dict) else None
        except (ValueError, TypeError, AttributeError):
            continue
        if turn:
            out.append(turn)
    return out


def _is_request(turn: tuple[str, str, str]) -> bool:
    """A message the person typed, or a slash command they ran with arguments."""
    role, _, text = turn
    if role != "user" or text.startswith("[tool"):
        return False
    return not (text.startswith("/") and " " not in text)


def _format(turn: tuple[str, str, str], limit: int) -> str:
    role, stamp, text = turn
    return f"{role.upper()} {stamp}: {_clip(text, limit)}".replace("  ", " ")


def conversation_slice(data: bytes, *, head: bytes | None = None,
                       max_chars: int = 24000, max_turns: int = 60) -> str:
    """Render the first request and the latest turns that fit the budget."""
    turns = _turns(data)
    first = next((t for t in _turns(head if head is not None else data)
                  if _is_request(t)), None)
    kept: list[str] = []
    used = 0
    for index, turn in enumerate(reversed(turns)):
        if len(kept) >= max_turns:
            break
        line = _format(turn, LAST_TURN_CHARS if index == 0 else TURN_CHARS)
        if kept and used + len(line) > max_chars:
            break
        kept.append(line)
        used += len(line) + 1
    kept.reverse()
    parts = []
    if first is not None and first not in turns[len(turns) - len(kept):]:
        parts.append("FIRST REQUEST " + _format(first, FIRST_REQUEST_CHARS))
        parts.append("[… earlier turns omitted …]")
    return "\n".join(parts + kept)


def read_conversation(source: Path, *, end: int | None = None,
                      max_chars: int = 24000, max_turns: int = 60) -> str:
    """conversation_slice over a transcript file, reading only its ends.

    end truncates the file at that byte offset, to replay a past state."""
    with open(source, "rb") as stream:
        size = stream.seek(0, 2) if end is None else end
        stream.seek(0)
        head = stream.read(min(HEAD_BYTES, size))
        tail = b""
        for step in TAIL_STEPS:
            start = max(0, size - step)
            stream.seek(start)
            tail = stream.read(size - start)
            if start == 0 or len(_turns(tail)) >= max_turns:
                break
        if start > 0:
            tail = tail[tail.find(b"\n") + 1:]
    return conversation_slice(tail, head=head, max_chars=max_chars,
                              max_turns=max_turns)
