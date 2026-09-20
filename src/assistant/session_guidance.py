"""Ground session return notes in current, identity-verified transcript evidence."""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlsplit


RECOMMENDATIONS = {"continue", "answer", "review", "park", "close_candidate", "unknown"}


def read_notes(path: Path) -> tuple[list[dict], str]:
    try:
        document = json.loads(path.read_text())
    except FileNotFoundError:
        return [], ""
    except (OSError, ValueError) as exc:
        return [], f"Session return notes could not be read: {exc}"
    if not isinstance(document, dict) or not isinstance(document.get("sessions"), list):
        return [], "Session return notes have an invalid format."
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
    return notes, f"{invalid} invalid session return notes were ignored." if invalid else ""


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


def has_merged_evidence(note: dict) -> bool:
    evidence = note.get("completion_evidence")
    if not isinstance(evidence, list):
        return False
    for item in evidence:
        if not isinstance(item, dict) or item.get("kind") != "pull_request" or item.get("state") != "MERGED":
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
                      action="Session evidence is out of date; this is not a decision for you.",
                      wrap_eligible=False)
        return result
    if card["lane"] == "parked" or card["pause_uncertain"]:
        return result
    if not current_sessions:
        result.update(lane="unknown", state="Status unknown",
                      action="No reliable current session context is available.",
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
                      source_kind="Outstanding question from the session")
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
        next_action = note.get("next_action") or "No further action is recorded for this task."
        result.update(guidance_note=note, goal=note["goal"], summary=note["progress"],
                      next=next_action, action=next_action,
                      source_kind="Reviewed return note; current conversation matches")
        if note.get("who") == "nobody":
            result.update(lane="updates", state="No action for you", wrap_eligible=False)
        elif note.get("who") == "unknown":
            result.update(lane="unknown", state="Next step not established", wrap_eligible=False)
        elif note.get("who") == "agent" or recommendation == "continue":
            result.update(lane="working", state="Agent's next step", wrap_eligible=False)
        elif recommendation in ("answer", "review") and tools_complete:
            result.update(lane="needs-you", state="Your next step", wrap_eligible=True)
        elif recommendation == "close_candidate" and tools_complete and has_merged_evidence(note):
            result.update(lane="ready", state="Review close-out", wrap_eligible=True)
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
        result.update(source_kind="Current observed next step", action=card["next"],
                      summary=card["summary"], next=card["next"])
        return result
    if card["lane"] == "working":
        result.update(action=text_excerpt(reply or request) or "A recorded tool call is still pending.",
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
                      action="The session has no verified text response to summarize.",
                      wrap_eligible=False)
    return result
