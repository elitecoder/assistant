"""Ground session return notes in current, identity-verified transcript evidence."""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from urllib.parse import urlsplit


RECOMMENDATIONS = {"continue", "answer", "review", "park", "close_candidate", "unknown"}

STATE_LABELS = {
    "Status unknown": "Not checked yet",
    "Question waiting for you": "Your answer is needed",
    "No action for you": "Nothing you need to do",
    "Next step not established": "Next step unclear",
    "Agent's next step": "Your assistant's next step",
    "Your next step": "What to do next",
    "Review close-out": "Check before closing",
    "Close-out evidence needs rechecking": "Check the result again",
    "Ready to park deliberately": "You can pause this work",
    "Latest recorded update": "Latest update",
    "Review the last response": "Read the latest reply",
    "Tools still running": "Waiting for a result",
    "Tool status unknown": "Not checked yet",
    "Request awaiting a response": "Waiting for a reply",
    "Parked intentionally": "Paused by you",
    "Pause needs confirmation": "Check whether this should stay paused",
    "Last signal: tool activity": "Waiting for a result",
    "In progress": "In progress",
    "Review before merging": "Review the code change",
    "Check before closing": "Check before closing",
    "Needs a decision": "Your answer is needed",
    "Check the session": "Check the latest update",
}


def read_notes(path: Path) -> tuple[list[dict], str]:
    try:
        document = json.loads(path.read_text())
    except FileNotFoundError:
        return [], ""
    except (OSError, ValueError) as exc:
        return [], f"Your saved session notes couldn't be read: {exc}"
    if not isinstance(document, dict) or not isinstance(document.get("sessions"), list):
        return [], "Your saved session notes have an unexpected format."
    notes = []
    invalid = 0
    for note in document["sessions"]:
        if not isinstance(note, dict) or any(
                not isinstance(note.get(key), str) or not note[key].strip()
                for key in ("workspace_id", "surface_id", "provider", "session_id",
                            "source_version", "goal", "progress")):
            invalid += 1
            continue
        if not isinstance(note.get("recommendation"), str) or note["recommendation"] not in RECOMMENDATIONS:
            invalid += 1
            continue
        if not isinstance(note.get("who"), str) or note["who"] not in {"user", "agent", "nobody", "unknown"}:
            invalid += 1
            continue
        if note["who"] in {"user", "agent"} and (
                not isinstance(note.get("next_action"), str) or not note["next_action"].strip()):
            invalid += 1
            continue
        if note.get("next_action") is not None and not isinstance(note["next_action"], str):
            invalid += 1
            continue
        evidence = note.get("completion_evidence", [])
        uncertainties = note.get("uncertainties", [])
        if (not isinstance(evidence, list) or not all(isinstance(item, dict) for item in evidence)
                or not isinstance(uncertainties, list) or not all(isinstance(item, str) for item in uncertainties)
                or not isinstance(note.get("rationale", ""), str)):
            invalid += 1
            continue
        malformed_evidence = False
        for item in evidence:
            if any(key in item and not isinstance(item[key], str)
                   for key in ("kind", "url", "path", "branch", "state")):
                malformed_evidence = True
                break
            if item.get("url"):
                try:
                    urlsplit(item["url"])
                except ValueError:
                    malformed_evidence = True
                    break
        if malformed_evidence:
            invalid += 1
            continue
        notes.append(note)
    return notes, f"{invalid} saved notes couldn't be used. Check their details." if invalid else ""


def matching_note(session: dict, notes: list[dict]) -> dict | None:
    context = session.get("guidance_context") or {}
    version = context.get("source_version")
    if not version:
        return None
    matches = [
        note for note in notes
        if note["workspace_id"].casefold() == str(session.get("workspace_id", "")).casefold()
        and note["surface_id"].casefold() == str(session.get("surface_id", "")).casefold()
        and note["provider"] == session.get("provider")
        and note["session_id"] == session.get("session_id")
        and note["source_version"] == version
    ]
    return matches[0] if len(matches) == 1 else None


def response_text(session: dict) -> str:
    context = session.get("guidance_context") or {}
    return ((context.get("last_response") or {}).get("text")
            or (session.get("last_assistant") or {}).get("text") or "")


def text_excerpt(text: str, limit: int = 220) -> str:
    compact = " ".join(text.replace("**", "").replace("`", "").split())
    if len(compact) <= limit:
        return compact
    return compact[:limit - 3].rstrip() + "..."


def has_completion_evidence(note: dict) -> bool:
    evidence = note.get("completion_evidence")
    if not isinstance(evidence, list):
        return False
    for item in evidence:
        if not isinstance(item, dict):
            continue
        if item.get("kind") == "artifact" and isinstance(item.get("path"), str):
            path = Path(item["path"])
            root = Path.home() / "dev/generated-docs"
            if not path.resolve().is_relative_to(root.resolve()) or path.suffix not in {".md", ".html"}:
                continue
            if path.is_file() and isinstance(item.get("sha256"), str):
                try:
                    with path.open("rb") as stream:
                        if hashlib.file_digest(stream, "sha256").hexdigest() == item["sha256"]:
                            return True
                except OSError as exc:
                    logging.getLogger(__name__).warning("Close-out artifact unavailable: %s", exc)
            continue
        if item.get("kind") != "pull_request" or item.get("state") != "MERGED":
            continue
        try:
            url = urlsplit(item.get("url", ""))
        except (TypeError, ValueError):
            continue
        if url.scheme == "https" and url.hostname == "github.com" and "/pull/" in url.path:
            return True
    return False


def guide_card(card: dict, current_sessions: list[dict], notes: list[dict],
               *, snapshot_fresh: bool, tools_complete: bool,
               new_request: bool) -> dict:
    """Select display-only guidance; never send input or change a workspace."""
    result = dict(card)
    result.update({"questions": [], "guidance_note": None, "goal": "", "source_kind": ""})
    if not snapshot_fresh:
        result.update(lane="unknown", state="Status unknown",
                      action="This information is old. Check for updates before acting on it.",
                      wrap_eligible=False)
        return result
    if card["lane"] == "parked" or card["pause_uncertain"]:
        return result
    if not current_sessions:
        result.update(lane="unknown", state="Status unknown",
                      action="This session hasn't been checked yet.",
                      wrap_eligible=False)
        return result
    questions = [
        question
        for session in current_sessions
        for question in (session.get("guidance_context") or {}).get("pending_questions", [])
    ]
    if questions:
        result.update(lane="needs-you", state="Question waiting for you",
                      action=questions[0]["question"], questions=questions,
                      next=questions[0]["question"], wrap_eligible=False,
                      source_kind="Question from your session")
        if len(current_sessions) == len(card["sessions"]) == 1:
            note = matching_note(current_sessions[0], notes)
            if note and note["who"] == "user" and note["recommendation"] == "answer":
                result.update(guidance_note=note, goal=note["goal"], summary=note["progress"],
                              action=note["next_action"], next=note["next_action"])
        return result
    latest = current_sessions[0]
    context = latest.get("guidance_context") or {}
    reply = response_text(latest)
    request = (context.get("last_request") or {}).get("text") or card["request"]
    initial = (context.get("initial_request") or {}).get("text") or ""
    result.update(goal=initial, request=request)
    if reply:
        result["summary"] = reply
        result["next"] = reply
    note = matching_note(latest, notes)
    if note and not new_request and len(current_sessions) == len(card["sessions"]) == 1:
        recommendation = note["recommendation"]
        next_action = note.get("next_action") or "No further action is listed for this task."
        result.update(guidance_note=note, goal=note["goal"], summary=note["progress"],
                      next=next_action, action=next_action,
                      source_kind="Saved note checked against this conversation")
        if note.get("who") == "nobody":
            result.update(lane="updates", state="No action for you", wrap_eligible=False)
        elif note.get("who") == "unknown":
            result.update(lane="unknown", state="Next step not established", wrap_eligible=False)
        elif note.get("who") == "agent" or recommendation == "continue":
            result.update(lane="working", state="Agent's next step", wrap_eligible=False)
        elif recommendation in ("answer", "review") and tools_complete:
            result.update(lane="needs-you", state="Your next step", wrap_eligible=True)
        elif recommendation == "close_candidate" and tools_complete and has_completion_evidence(note):
            result.update(lane="ready", state="Review close-out", wrap_eligible=True)
        elif recommendation == "close_candidate" and tools_complete:
            result.update(lane="updates", state="Close-out evidence needs rechecking",
                          action="The saved result changed or couldn't be found. Check it before closing.",
                          wrap_eligible=False)
        elif recommendation == "park" and tools_complete:
            result.update(lane="needs-you", state="Ready to park deliberately", wrap_eligible=True)
        else:
            result.update(lane="updates", state="Latest recorded update", wrap_eligible=False)
        if card["lane"] == "working" and recommendation != "continue":
            result.update(lane="working", state="Tools still running", wrap_eligible=False)
        return result
    if card.get("observation_current"):
        if card["state"] == "Tool status unknown":
            result.update(lane="unknown", wrap_eligible=False)
            return result
        result.update(source_kind="Next step from the latest check", action=card["next"],
                      summary=card["summary"], next=card["next"])
        return result
    if card["lane"] == "working":
        result.update(action=text_excerpt(reply or request) or "Your assistant is waiting for a result.",
                      source_kind="Last session update", wrap_eligible=False)
    elif new_request:
        result.update(lane="updates", state="Request awaiting a response",
                      action=text_excerpt(request), source_kind="Your last request",
                      wrap_eligible=False)
    elif reply:
        result.update(lane="updates",
                      state="Latest recorded update" if tools_complete else "Tool status unknown",
                      action=text_excerpt(reply), source_kind="Quoted from the session",
                      wrap_eligible=False)
    else:
        result.update(lane="unknown", state="Status unknown",
                      action="No reply is available to summarize yet.",
                      wrap_eligible=False)
    return result
