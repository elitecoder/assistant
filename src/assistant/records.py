"""The records of one piece of work: what each step of it writes to telemetry, built in one place.

A step that wants to leave a record calls one function here (`records.task(...)`, `records.job_run(...)`) on a line
of its own, after the step it records. Every function builds its record's arguments from what the step already has
and hands them to `telemetry.emit`. A record holds ids, counts, timings and states from fixed lists, never a title,
a sender, message text, a link, a model's reason or a prompt: `telemetry` enforces that, and the builders here pass
only what the registry takes and leave out what they can't name.

Recording never changes what Assistant does. Nothing here raises: a mistake in building a record's arguments, a
telemetry module that can't be imported, an `emit` that raises, all cost that one record and nothing else. The one
exception is the tests' strict mode (ASSISTANT_TELEMETRY_STRICT=1 with a test running, the mode `telemetry.emit`
itself raises in), where the mistake is raised instead, so it is found before it ships.

Standard library only; `telemetry`, `todostore` and `pointers` are imported when a record is made, so a module that
calls this one does not depend on them to load.
"""
from __future__ import annotations

import os
import re
import time

STRICT_ENV = "ASSISTANT_TELEMETRY_STRICT"
_TASK_ID = re.compile(r"td-[0-9]{1,9}")
_WORKSPACE = re.compile(r"workspace:[0-9]{1,18}")  # cmux numbers sessions from 1000010000
# The caller named in a spend row, as the purpose a record lists. Anything else is "other".
PURPOSES = {"work_queue": "triage", "triage": "triage", "observer": "observer", "observer-audit": "observer",
            "strategist": "strategist", "narrator": "narrate", "brief": "brief", "planner": "planner",
            "lessons": "lesson"}
TIERS = (("opus", "frontier"), ("sonnet", "balanced"), ("haiku", "cheap"))
# What `metering.failure_reason` says, as the words an error field takes.
FAILURE_WORDS = {"timed out": "timeout", "not installed": "not_found"}


def loud() -> bool:
    """True in the tests' strict mode: a mistake in a record is raised instead of costing the record."""
    return os.environ.get(STRICT_ENV) == "1" and "PYTEST_CURRENT_TEST" in os.environ


def guard(fn, *args, **kwargs):
    """`fn(*args, **kwargs)`, or None when it raises: a record is never worth the step it sits in."""
    try:
        return fn(*args, **kwargs)
    except Exception:  # noqa: BLE001 - recording never breaks work
        if loud():
            raise
        return None


def emit(kind: str, trace: str = "", subject: str = "", component: str = "", **fields) -> None:
    """`telemetry.emit`, imported now and never raising. Fields that are None are left out."""
    try:
        from assistant import telemetry  # noqa: PLC0415
        telemetry.emit(kind, trace, subject, component, **{k: v for k, v in fields.items() if v is not None})
    except Exception:  # noqa: BLE001 - recording never breaks work
        if loud():
            raise


def emit_many(batch) -> None:
    """`telemetry.emit_many`: a list of (kind, fields) pairs, `fields` holding `trace`, `subject` and `component` too,
    written in one go, imported now and never raising. Fields that are None are left out."""
    try:
        from assistant import telemetry  # noqa: PLC0415
        telemetry.emit_many([(kind, {k: v for k, v in fields.items() if v is not None}) for kind, fields in batch])
    except Exception:  # noqa: BLE001 - recording never breaks work
        if loud():
            raise


def recording() -> bool:
    """Whether telemetry is switched on. A setting that can't be read counts as off, as it does for `telemetry.emit`."""
    try:
        return bool(_telemetry()._settings()["enabled"])
    except Exception:  # noqa: BLE001
        return False


def new_trace() -> str:
    """A trace id for one run of a job (a refresh, a pulse). "" when one can't be made."""
    def made() -> str:
        from assistant import pointers  # noqa: PLC0415

        return pointers.new_trace_id()

    return guard(made) or ""


def ms_since(began: float) -> int:
    """Whole milliseconds since `began`, a time on the monotonic clock."""
    return max(0, int((time.monotonic() - began) * 1000))


def _telemetry():
    from assistant import telemetry  # noqa: PLC0415

    return telemetry


def name_of(value):
    """`value` when telemetry takes it as an open name (a model's id, a check's name); else None."""
    return value if isinstance(value, str) and _telemetry()._clean_name(value) is not None else None


def error_class(exc) -> str | None:
    """The exception's class name when telemetry takes it as one, else None."""
    name = type(exc).__name__ if isinstance(exc, BaseException) else None
    return name if name and _telemetry()._clean_error(name) is not None else None


def one_of(value, listed: str):
    """`value` when it is on telemetry's list of that name (`TASK_STATE`, `SEND_MODE`), else None."""
    return value if isinstance(value, str) and value in getattr(_telemetry(), listed) else None


def task_id(item) -> str:
    """The id a record gives a task: its `td-` id, or "" when it has none of that shape (a policy's `td-auto-...`)."""
    found = item.get("id") if isinstance(item, dict) else None
    return found if isinstance(found, str) and _TASK_ID.fullmatch(found) else ""


def task_trace(item) -> str:
    from assistant import todostore  # noqa: PLC0415

    return todostore.trace_of(item)


def origin_of(source) -> str | None:
    """The list value a task's `source` field stands for: the part before the first `:`, matched to a source, a
    goal, the Work queue, or a policy (an agent's own doing). A hand edit or an unknown source is `manual`."""
    head = str(source or "").partition(":")[0].lower()
    if head.startswith("slack"):
        head = "slack"
    head = {"policy": "agent"}.get(head, head)
    return head if head in _telemetry().ORIGIN else "manual"


# ---------------------------------------------------------------- the records


def _task_part(item, state: str, component: str, prev=None, origin=None, mode=None):
    """The (kind, fields) of a task record, or None when there is nothing to say."""
    if one_of(state, "TASK_STATE") is None:  # a status written by hand that no list holds: nothing to say
        return None
    item = item if isinstance(item, dict) else {}
    return ("task", {"trace": task_trace(item), "subject": task_id(item), "component": component, "state": state,
                     "prev_state": one_of(prev, "TASK_STATE"), "origin": origin,
                     "mode": one_of(mode if mode is not None else item.get("workQueueMode"), "SEND_MODE")})


def _task(item, state: str, component: str, prev=None, origin=None, mode=None) -> None:
    emit_many([part for part in [_task_part(item, state, component, prev, origin, mode)] if part])


def task(item, state: str, component: str, prev=None, origin=None, mode=None) -> None:
    """A task is now in `state` (`prev` is what it was). `item` is the task as the file holds it."""
    guard(_task, item, state, component, prev, origin, mode)


def _task_created(item, component: str, mode=None, state=None):
    return _task_part(item, state or "open", component, None, origin_of(item.get("source")), mode)


def task_created(item, component: str, mode=None) -> None:
    """A task was made, `open`, with the origin its `source` stands for."""
    guard(lambda: emit_many([part for part in [_task_created(item, component, mode)] if part]))


def _tasks_stamped(items) -> None:
    parts = [guard(lambda: _task_created(item, "pulse", None, one_of(item.get("status"), "TASK_STATE"))) for item in items]
    emit_many([part for part in parts if part])  # one write for all the tasks, not one for each


def tasks_stamped(items) -> None:
    """Tasks the pulse gave their first trace id: they arrived from the Slack reactor, a skill or a hand edit, so
    each is recorded in the state the file holds it in."""
    guard(_tasks_stamped, items)


def _dispatch(before, after, ok: bool, began: float, reason=None, run_trace: str = "", confirmed=None) -> None:
    item = after if isinstance(after, dict) and after else before
    if not isinstance(item, dict):
        return
    workspace = after.get("dispatchedWs") if isinstance(after, dict) else None
    moved = isinstance(before, dict) and isinstance(after, dict) and workspace != before.get("dispatchedWs")
    emit("dispatch", task_trace(item), task_id(item), "pulse", outcome="done" if ok else "failed", ok=ok,
         workspace=workspace if moved and isinstance(workspace, str) and _WORKSPACE.fullmatch(workspace) else None,
         mode=one_of(item.get("workQueueMode"), "SEND_MODE"), ms=ms_since(began),
         reason=None if ok else one_of(reason, "DISPATCH_REASON"), run_trace=run_trace or None,
         confirmed=confirmed if ok and isinstance(confirmed, bool) else None)
    if moved and after.get("status") == "in-progress":
        _task(after, "in-progress", "pulse", before.get("status"))


def dispatch(before, after, ok: bool, began: float, reason=None, run_trace: str = "", confirmed=None) -> None:
    """A task was dispatched (or the attempt failed). `before` and `after` are the task as the file held it before
    and after, so the workspace and the new state are what the file says, not what the code meant. `reason` is the
    word for what stopped a dispatch (`DISPATCH_REASON`, only when it failed), `run_trace` the pulse's, and
    `confirmed` whether a dispatch that went out was seen to arrive."""
    guard(_dispatch, before, after, ok, began, reason, run_trace, confirmed)


def _check(item, name: str, component: str = "work-queue") -> None:
    emit("check", task_trace(item), task_id(item), component, name=name_of(name), outcome="done", ok=True)


def check(item, name: str) -> None:
    """A check cleared a finished task (`name` is the check) or noted that what blocked it looks gone."""
    guard(_check, item, name)


def _job_run(job: str, trace: str, component: str, began: float, ok: bool, items=None, exc=None) -> None:
    emit("job_run", trace, "", component, job=name_of(job), outcome="done" if ok else "failed", ok=ok,
         ms=ms_since(began), items=items if isinstance(items, int) and not isinstance(items, bool) else None,
         error=error_class(exc) if exc is not None else None)


def job_run(job: str, trace: str, component: str, began: float, ok: bool, items=None, exc=None) -> None:
    """A job ran: a refresh, a pulse. `trace` is the run's, `items` how many it handled."""
    guard(_job_run, job, trace, component, began, ok, items, exc)


def count_of(found) -> int:
    """How many rows a source returned: a list's length, or the lengths of a dict's lists added."""
    return sum(map(len, found.values())) if isinstance(found, dict) else len(found)


def rows_in(snap: dict):
    """How many rows a refresh's sources returned (everything in the snapshot but its status lines); None when that
    can't be counted."""
    return guard(lambda: sum(count_of(v) for k, v in snap.items() if k != "status"))


_NOT_SIGNED_IN = re.compile(r"not_authed|invalid_auth|token_revoked|token_expired|account_inactive|log(?:ged)? ?in|sign(?:ed)? ?in|"
                            r"unauthori[sz]ed|authenticat|credential|\b40[13]\b", re.I)


def source_reason(exc) -> str:
    """The word for why a source couldn't be read, from the error alone (its text is never kept): the tool isn't
    installed, it ran out of time, answered in a shape that isn't JSON, another job holds the Slack browser, or its
    message says it isn't signed in."""
    text = str(exc)
    if type(exc).__name__ == "ToolMissing":
        return "tool_missing"
    if type(exc).__name__ == "Busy":
        return "busy"
    if "didn't answer within" in text or isinstance(exc, TimeoutError):
        return "timeout"
    if "didn't return JSON" in text:
        return "bad_answer"
    return "not_signed_in" if _NOT_SIGNED_IN.search(text) else "other"


def _source_fetch(source: str, trace: str, ok: bool, items, began: float, exc=None) -> None:
    emit("source_fetch", trace, "", "work-queue", source=one_of(source, "SOURCE"), outcome="done" if ok else "failed",
         ok=ok, items=items, ms=ms_since(began), error=error_class(exc) if exc is not None else None,
         reason=source_reason(exc) if exc is not None else None)


def fetching(source: str, read, trace: str):
    """`read` (a source's reader), recording each read: the source, how many rows, how long, and the class of the
    error when it failed. The reader's rows and errors go through as they were."""
    def recorded(entry):
        began = time.monotonic()
        try:
            found = read(entry)
        except Exception as exc:
            guard(_source_fetch, source, trace, False, None, began, exc)
            raise
        guard(lambda: _source_fetch(source, trace, True, count_of(found), began))
        return found
    return recorded


MODEL_FAILURE_KINDS = {"timed out": "timeout", "not installed": "tool_missing", "exit code 0": "bad_answer"}


def _model_call(caller: str, model, status: str, usage: dict, wall_ms, trace: str, error, batch=None, kind=None) -> None:
    ok = status == "ok"
    model = model if isinstance(model, str) else ""
    tier = next((tier for word, tier in TIERS if word in model), None)
    word = FAILURE_WORDS.get(error, "other") if not ok and isinstance(error, str) and error else None
    emit("model_call", trace if isinstance(trace, str) else "", "", "work-queue" if caller == "work_queue" else "pulse",
         model=name_of(model), tier=tier, purpose=PURPOSES.get(caller, "other"),
         outcome="done" if ok else ("timeout" if word == "timeout" else "failed"), ok=ok,
         tokens_in=int(usage.get("tokens_in") or 0), tokens_out=int(usage.get("tokens_out") or 0),
         cost_usd=float(usage.get("cost_usd") or 0.0), ms=int(wall_ms), error=word,
         batch=batch if isinstance(batch, int) and not isinstance(batch, bool) and batch > 0 else None,
         reason=None if ok else one_of(kind or MODEL_FAILURE_KINDS.get(error), "SOURCE_REASON") or "other")


def model_call(caller: str, model, status: str, usage: dict, wall_ms, trace: str = "", error=None, batch=None,
               kind=None) -> None:
    """A model call, as the spend row has it: the same estimate of cost, for a call that failed too. The error is a
    word from a short list, never the model's or the tool's text. `batch` is which of the run's calls this was.
    A failed call's `reason` is `kind` when the caller knows the kind of failure without reading the tool's output
    (it timed out, the tool is missing, it answered with something unusable), else `other`: a call that failed
    because it wasn't signed in or was rate limited can only be told apart by reading that output, which isn't done."""
    guard(_model_call, caller, model, status, usage, wall_ms, trace, error, batch, kind)


def _effect_finished(effect, outcome: str) -> None:
    import json  # noqa: PLC0415

    from assistant import effects  # noqa: PLC0415

    target = json.loads(effect.key)
    target_id = next((target[k] for k in ("row", "task", "session") if isinstance(target.get(k), str)), None)
    spec = effects.CLASSES.get(effect.cls)
    emit("effect", effect.trace, effect.id, "pulse" if effect.cls.startswith("session.") else "work-queue",
         action=effect.cls, outcome=outcome, ok=outcome == "done", target=target_id,
         reversible=spec.reversible if spec else None)


def effect_finished(effect, outcome: str) -> None:
    """An effect got its result in the ledger. `effect` is the ledger's own entry for it."""
    guard(_effect_finished, effect, outcome)
