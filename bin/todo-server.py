#!/usr/bin/env python3
"""Tiny HTTP daemon for the Assistant dashboards (TODO + main).

Bound to 127.0.0.1:9876 (localhost only). Endpoints:
  POST /toggle/<id>?flag=autoDispatch&value=true|false   flip a boolean flag
  POST /remove/<id>                                       soft-delete an item
                                                          (moved to removed[]
                                                          with removedAt stamp)
  POST /focus/<workspace_ref>                             switch the active
                                                          cmux workspace
    Optional ?workspace_id=<UUID> rejects a changed identity with HTTP 409.
                                                          (workspace:N only)
  POST /decision/list                                     decision queue view
                                                          (Keel M2; open set +
                                                          totals as JSON)
  POST /decision/act/<dec-id>?action=accept|edit|reject|snooze|wrong_lane
                                                          &minutes=N
                                                          one-tap decision
                                                          transition (id-regex
                                                          validated, POST-only;
                                                          body = optional note;
                                                          wrong_lane also files
                                                          a confirmation-gated
                                                          type=policy proposal)
  POST /goal/list                                        goals store, rank order
                                                          (Keel M4)
  POST /goal/add                                          add a goal (body=JSON;
                                                          HUMAN edit path)
  POST /goal/update/<goal-id>                             edit a goal directly
                                                          (id-regex validated;
                                                          HUMAN edit path —
                                                          automation files a
                                                          confirmation-gated
                                                          goal_update proposal
                                                          instead)
  POST /goal/rerank                                       reassign ranks
                                                          (body={order:[...]})
  POST /goal/pause | /goal/resume                         flip the _paused kill
                                                          switch (Keel M4; the
                                                          REAL switch, not a
                                                          no-op ping)
  POST /brief/refresh                                     rebuild the page now
  POST /mode?value=pro|default                            switch Pro mode
  POST /brief/seen?date=YYYY-MM-DD                        Brief-tab view signal
                                                          (Keel M3): stamps the
                                                          seen sidecar next to
                                                          the brief file so the
                                                          unseen-degradation
                                                          pass knows the brief
                                                          was looked at
  POST /slack-focus/status                                Slack tab refresh state
                                                          (JSON)
  POST /slack-focus/refresh                               start a background
                                                          Slack focus refresh
                                                          (no-op while one runs)
  POST /slack-focus/done | /slack-focus/undone            check an item off or
                                                          back on (body =
                                                          {"fp": "<sha256>"})
  GET  /                                                  health check ("ok")

On every successful TODO mutation, the JSON file is atomically rewritten and the
static HTML is re-rendered so the next dashboard load reflects the new state.
Live POST signals (the one-tap decision routes, /brief/seen) require THIS running
server: a dashboard opened as a bare file:// page reaches them only because the
renderer posts to the absolute http://127.0.0.1:9876 origin — a relative fetch
from file:// would resolve to file:///… and never arrive (F19).
/focus is a side-effect-only command (shells out to `cmux select-workspace`).

Every request must name this server as 127.0.0.1 or localhost, on any port (a
forwarded port works; a DNS-rebinding page's own name does not). Every POST
passes one guard first (_post_refused): the page this server serves is let
through by its Origin, which must be 127.0.0.1 or localhost on the port it was reached by; a
missing Origin or Origin: null (the page opened as a local file, curl) needs the
per-install token in ~/.assistant/api-token, sent as X-Assistant-Token; any
other Origin gets 403. The page is never served to a foreign Origin and cannot
be framed.

Stdlib only. Run by LaunchAgent com.assistant.assistant-todo-server.
"""

import datetime
import hmac
import json
import os
import plistlib
import re
import shutil
import signal
import subprocess
import sys
import time
import unicodedata
import urllib.parse
from uuid import UUID
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

WORKSPACE_REF_RE = re.compile(r"^workspace:\d+$")
TD_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
PROPOSAL_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,128}$")
# Decision ids are `dec-` + a hex digest prefix (src/assistant/decisions.py).
DEC_ID_RE = re.compile(r"^dec-[a-f0-9]{8,64}$")
# Goal ids are `goal-` + a bounded integer (src/assistant/goals.GOAL_ID_RE).
# Mirrored here so the /goal/update route validates the path segment BEFORE any
# store read, exactly like DEC_ID_RE gates /decision/act.
GOAL_ID_RE = re.compile(r"^goal-[0-9]{1,6}$")
# Dashboard action → decision status. The vocabulary is closed here AND in
# decisions.transition (auto_done/open are never reachable via HTTP).
# wrong_lane (Keel M3) resolves the decision as rejected AND files a
# confirmation-gated type=policy proposal for its (source, kind) — the tap
# that teaches the policy engine instead of just dismissing the row.
DECISION_ACTIONS = {"accept": "accepted", "edit": "edited",
                    "reject": "rejected", "snooze": "snoozed",
                    "wrong_lane": "rejected"}
# /brief/seen date param: strict ISO date only.
BRIEF_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SRC_DIR = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC_DIR))
from assistant.redact import install_crash_hooks, redact  # noqa: E402 - request lines can carry ?token=...
SEND_SCRIPT = Path(__file__).resolve().parent / "cmux-send.py"
RUN_NOW_MAX = 500
# cmux-send.py's worst case: the terminal lookup (10 s), two keystrokes (10 s each) and its 2.5 s wait.
SEND_SCRIPT_TIMEOUT_SEC = 45
CMUX_BIN = shutil.which("cmux") or "/Applications/cmux.app/Contents/Resources/bin/cmux"

HOME = Path(os.path.expanduser("~"))
JSON_PATH = HOME / ".claude" / "assistant-todo.json"
DASHBOARD_HTML_PATH = HOME / ".claude" / "assistant-dashboard.html"
# Both rerender targets point at the live single-Renderer script. The legacy
# render-todo.py / render-dashboard.py paths were retired 2026-05-22; keeping
# both names so existing call sites still work, both now resolve to the live
# renderer.
# Derive the renderer from THIS file's location, not a hardcoded ~/dev/assistant
# — the repo is not assumed to live there (portability: a checkout elsewhere
# must still re-render the dashboard). The renderer is always a sibling in bin/.
RENDER_SCRIPT = Path(__file__).resolve().parent / "render-assistant-page.py"
RENDER_DASHBOARD_SCRIPT = RENDER_SCRIPT
PROPOSALS_DIR = HOME / ".architect" / "orchestrator-proposals"
SLACK_FOCUS_SCRIPT = Path(__file__).resolve().parent / "slack-focus.py"
SLACK_FOCUS_LAUNCH = '[ -f "$HOME/.zprofile" ] && . "$HOME/.zprofile" >/dev/null 2>&1; exec "$0" "$@"'
SLACK_FOCUS_FP_RE = re.compile(r"^[a-f0-9]{64}$")
_slack_focus_proc = None
ALLOWED_FLAGS = {"autoDispatch", "closeOnMerge"}
PORT = 9876
HOST = "127.0.0.1"
# The only names the dashboard answers under: a DNS-rebinding page arrives under its own name.
# Any port is fine, so a forwarded port (ssh -L 9877:127.0.0.1:9876) keeps working.
HOST_RE = re.compile(r"(127\.0\.0\.1|localhost):[0-9]{1,5}")
# /decision/list snippet cap: the list view is a queue overview, not the
# store — 120 chars is plenty for a row; full content stays in the store.
LIST_SNIPPET_MAX = 120


def load_json():
    return json.loads(JSON_PATH.read_text())


def save_json(data):
    tmp = JSON_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    os.replace(tmp, JSON_PATH)


def _todo_lock():
    """The ONE shared todo-file lock (M3). Every route that read-modify-writes
    assistant-todo.json wraps its load_json()+save_json() in this so a concurrent
    pulse dispatch stamp / triage create / goals stage can't lose an update. A
    broken import degrades to a null context (never block a dashboard edit)."""
    try:
        if str(SRC_DIR) not in sys.path:
            sys.path.insert(0, str(SRC_DIR))
        from assistant import todostore  # noqa: PLC0415
        return todostore.todo_lock()
    except Exception:
        import contextlib
        return contextlib.nullcontext()


def _record(name, *args, **kwargs):
    """One telemetry record, built by assistant.records.<name>. Imported here, inside the try: a broken module costs a
    record, never a dashboard edit. In the tests' strict mode a mistake in a record is raised."""
    try:
        from assistant import records  # noqa: PLC0415
        getattr(records, name)(*args, **kwargs)
    except Exception:
        if os.environ.get("ASSISTANT_TELEMETRY_STRICT") == "1" and "PYTEST_CURRENT_TEST" in os.environ:
            raise


def _count_render_failure(error):
    """Count a failed re-render so a repeat shows once on the dashboard. Never
    raises: this runs on the request path."""
    try:
        if str(SRC_DIR) not in sys.path:
            sys.path.insert(0, str(SRC_DIR))
        from assistant import failwatch  # noqa: PLC0415
        if error is None:
            failwatch.record_ok("dashboard-render")
        else:
            # Nothing retries a render by itself, so there is nothing to pause.
            failwatch.record_failure("dashboard-render", error, pause=False)
    except Exception:
        pass


def rerender():
    try:
        done = subprocess.run(
            [sys.executable, str(RENDER_SCRIPT)],
            check=False,
            timeout=4,
            capture_output=True,
            text=True,
        )
        lines = (done.stderr or "").strip().splitlines()
        _count_render_failure(None if done.returncode == 0 else
                              (lines[-1] if lines else f"the page render exited with code {done.returncode}"))
    except subprocess.TimeoutExpired:
        _count_render_failure("the page render took longer than 4 seconds")
    except Exception as exc:
        _count_render_failure(f"the page render could not start: {exc}")


def set_flag(td_id, flag, value):
    """Set a TODO's boolean-ish flag. value is True, False, or None."""
    with _todo_lock():
        data = load_json()
        items = data.get("items", []) or []
        target = next((i for i in items if i.get("id") == td_id), None)
        if target is None:
            return False, f"id {td_id!r} not found"
        target[flag] = value  # None means "user hasn't decided" (Bucket C)
        save_json(data)
    rerender()
    return True, f"{td_id}.{flag} = {value!r}"


def append_detail(td_id, text):
    text = (text or "").strip()
    if not text:
        return False, "empty body"
    with _todo_lock():
        data = load_json()
        items = data.get("items", []) or []
        target = next((i for i in items if i.get("id") == td_id), None)
        if target is None:
            return False, f"id {td_id!r} not found"
        now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        prior = (target.get("detail") or "").rstrip()
        sep = "\n\n" if prior else ""
        target["detail"] = f"{prior}{sep}[mukul {now}] {text}"
        save_json(data)
    rerender()
    return True, f"{td_id}: appended {len(text)} chars"


def dispatch_now(td_id):
    """Force the Assistant's Bucket B path to fire on td_id at the next pulse.

    Sets autoDispatch=true and clears dispatchedAt + dispatchedWs so the
    Assistant treats the TODO as Bucket B (autoDispatch=true, dispatchedAt
    empty). Also flips status back to 'open' if it was deferred or blocked,
    because Mukul is explicitly asking for a fresh attempt.
    """
    with _todo_lock():
        data = load_json()
        items = data.get("items", []) or []
        target = next((i for i in items if i.get("id") == td_id), None)
        if target is None:
            return False, f"id {td_id!r} not found"
        now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        before = target.get("status")
        target["autoDispatch"] = True
        target["dispatchedAt"] = None
        target["dispatchedWs"] = None
        if target.get("status") in ("deferred", "blocked", "done"):
            target["status"] = "open"
            target["statusUpdatedAt"] = now
            target["statusReason"] = f"Re-opened via dashboard 'Dispatch now' at {now}"
        save_json(data)
    if target.get("status") != before:
        _record("task", target, target.get("status"), "dashboard", before)
    rerender()
    return True, f"{td_id}: queued for next-pulse Bucket B dispatch"


WORK_QUEUE_LABEL = "com.assistant.work-queue"


def work_queue_python(plist):
    """Why the Work queue's plist can't start it: its python3 is older than 3.11, doesn't run, or can't be read;
    "" when it can start it."""
    try:
        python = plistlib.loads(plist.read_bytes())["ProgramArguments"][0]
    except (OSError, ValueError, KeyError, IndexError, TypeError):
        return f"the python3 in {plist} can't be read"
    try:
        check = subprocess.run([python, "-c", "import sys; sys.exit(sys.version_info < (3, 11))"],
                               capture_output=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return f"{python} doesn't run"
    return "" if check.returncode == 0 else f"{python} is older"


def sync_work_queue(pro):
    """Load the Work queue LaunchAgent on a switch to Pro mode, and unload it on a
    switch to default mode. Acts only when install.sh copied its plist into this
    HOME, and never starts it on a python3 older than 3.11: it says why instead.
    A failed launchctl call leaves the mode switched: the server itself stops
    within seconds once Pro mode is off."""
    plist = Path(os.path.expanduser("~")) / "Library/LaunchAgents" / f"{WORK_QUEUE_LABEL}.plist"
    if not plist.exists():
        return ""
    if pro and (why := work_queue_python(plist)):
        return (f"The Work queue needs Python 3.11 or newer, and {why}, so it wasn't started. "
                "Install Python 3.11+, then run ./install.sh --apply --pro.")
    domain = f"gui/{os.getuid()}"
    try:
        if not pro:
            cmd = ["launchctl", "bootout", f"{domain}/{WORK_QUEUE_LABEL}"]
        elif subprocess.run(["launchctl", "print", f"{domain}/{WORK_QUEUE_LABEL}"],
                            capture_output=True, timeout=10, check=False).returncode == 0:
            cmd = ["launchctl", "kickstart", f"{domain}/{WORK_QUEUE_LABEL}"]
        else:
            cmd = ["launchctl", "bootstrap", domain, str(plist)]
        subprocess.run(cmd, capture_output=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        pass
    return ""


def set_mode(value):
    """Switch default/Pro mode, sync the Pro skills and the Work queue service,
    and rebuild the page."""
    try:
        if str(SRC_DIR) not in sys.path:
            sys.path.insert(0, str(SRC_DIR))
        from assistant import mode  # noqa: PLC0415
        if value not in mode.MODES:
            return False, f"mode must be one of {', '.join(mode.MODES)}"
        mode.switch(value)
    except (OSError, ValueError) as exc:
        return False, f"Couldn't change the mode: {exc}"
    held = sync_work_queue(value == "pro")
    if not rerender_dashboard():
        return True, f"Switched to {value}. The page updates within 5 minutes.{' ' + held if held else ''}"
    return True, f"Switched to {value}. {held}" if held else value


def rerender_dashboard():
    try:
        return subprocess.run(
            [sys.executable, str(RENDER_DASHBOARD_SCRIPT)],
            check=False, timeout=6, capture_output=True,
        ).returncode == 0
    except Exception:
        return False


def load_proposal(prop_id):
    if not PROPOSALS_DIR.exists():
        return None, None
    for p in PROPOSALS_DIR.glob("*.json"):
        try:
            d = json.loads(p.read_text())
            if d.get("id") == prop_id:
                return p, d
        except Exception:
            pass
    return None, None


def save_proposal(path, data):
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def _run_now_failure(sent) -> str:
    """What to tell the person when cmux-send.py didn't finish typing: what actually happened."""
    try:
        record = json.loads(sent.stdout or "{}")
    except (ValueError, TypeError):
        record = {}
    outcome = record.get("outcome") if isinstance(record, dict) else None
    if outcome in ("rpc_send_text_timeout", "rpc_send_key_timeout"):
        return ("Assistant sent this but didn't hear back, so it may have been typed. Check the session before you "
                "send it again.")
    if outcome == "rpc_send_key_failed":
        return "run-now typed the text into the session but couldn't press Enter, so it is waiting there unsent"
    if outcome == "rpc_send_text_failed":
        return "run-now couldn't type into the session"
    if outcome == "no_terminal_surface":
        return "run-now couldn't find a terminal in that session, so nothing was typed"
    last = ((sent.stderr or "").strip().splitlines() or ["see the send record"])[-1][:200]
    return f"run-now didn't type into the session: {last}"


def proposal_action(prop_id, action, params):
    path, data = load_proposal(prop_id)
    if data is None:
        return False, f"proposal {prop_id!r} not found"
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if action == "hold":
        data["held"] = True
        data.setdefault("thread", []).append({"ts": now, "actor": "mukul", "note": "Held via dashboard."})
    elif action == "unhold":
        data["held"] = False
        data.setdefault("thread", []).append({"ts": now, "actor": "mukul", "note": "Unheld via dashboard."})
    elif action == "snooze":
        minutes = int(params.get("minutes", [30])[0])
        snooze_until = (
            datetime.datetime.now(datetime.timezone.utc)
            + datetime.timedelta(minutes=minutes)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        data["snoozed_to"] = snooze_until
        data.setdefault("thread", []).append({"ts": now, "actor": "mukul", "note": f"Snoozed {minutes}m via dashboard."})
    elif action == "veto":
        data["status"] = "vetoed"
        data.setdefault("thread", []).append({"ts": now, "actor": "mukul", "note": "Vetoed via dashboard."})
    elif action == "run-now":
        execute_via = data.get("execute_via", "")
        touches = data.get("touches", []) or []
        if execute_via == "send-text-to-session":
            ws_ref = next((t["ref"] for t in touches if t.get("type") == "session"), None)
            if ws_ref:
                # The file is written by another tool, so it is checked before anything is typed: a real session, and
                # one short line. The typing itself goes through cmux-send.py, which records it first.
                text = data.get("action", "")
                if not re.fullmatch(r"workspace:[0-9]{1,18}", str(ws_ref)):
                    return False, "run-now needs the session as workspace:N, so nothing was typed"
                if not isinstance(text, str) or not text or len(text) > RUN_NOW_MAX or any(
                        unicodedata.category(c) in ("Cc", "Cf", "Zl", "Zp") for c in text):
                    return False, f"run-now types one line of up to {RUN_NOW_MAX} characters, so nothing was typed"
                try:
                    sent = subprocess.run(
                        [sys.executable, str(SEND_SCRIPT), "--ws", ws_ref, "--text", text, "--enter",
                         "--caller", "todo-server", "--source", "run-now", "--auth-kind", "tap",
                         "--auth-ref", "/proposal/run-now"],
                        check=False, timeout=SEND_SCRIPT_TIMEOUT_SEC, capture_output=True, text=True,
                    )
                except Exception as e:
                    return False, f"cmux send failed: {e}"
                if sent.returncode != 0:
                    return False, _run_now_failure(sent)
        data["status"] = "working"
        data.setdefault("thread", []).append({"ts": now, "actor": "assistant", "note": "Dispatched via dashboard Run button."})
    else:
        return False, f"unknown action {action!r}"
    save_proposal(path, data)
    rerender_dashboard()
    return True, f"{prop_id}.{action} ok"


def _api_token_mod():
    """Import src/assistant/apitoken lazily, like the other src modules."""
    if str(SRC_DIR) not in sys.path:
        sys.path.insert(0, str(SRC_DIR))
    from assistant import apitoken  # noqa: PLC0415
    return apitoken


def _decisions_mod():
    """Import src/assistant/decisions lazily — the decision routes must not
    keep the whole server from starting if the module is broken/absent."""
    if str(SRC_DIR) not in sys.path:
        sys.path.insert(0, str(SRC_DIR))
    from assistant import decisions  # noqa: PLC0415
    return decisions


def decision_list():
    """The materialized queue view: open decisions (queue order) + totals."""
    try:
        decisions = _decisions_mod()
        view = decisions.load_queue()
        opens = decisions.open_decisions(view)
    except Exception as e:
        return False, f"decision store unavailable: {e}"
    # Truncate snippets for the wire: the list response is an overview and
    # anything embedding it (dashboard HTML, logs) shouldn't ship 500 chars
    # of raw screen scrape per row. The store keeps the full snippet.
    opens = [
        {**d, "snippet": (d.get("snippet") or "")[:LIST_SNIPPET_MAX]}
        if isinstance(d.get("snippet"), str) else dict(d)
        for d in opens
    ]
    return True, json.dumps({
        "open": opens,
        "n_open": len(opens),
        "n_total": len(view.get("decisions", [])),
        "ts": view.get("ts"),
    }, ensure_ascii=False)


def decision_act(dec_id, action, params, note):
    """One-tap transition on a decision. Appends a new record via
    decisions.transition (append-only log, flock'd, ledgered) — this server
    never rewrites decision state directly. wrong_lane additionally files a
    confirmation-gated type=policy proposal (design section 5: the tap
    teaches the policy engine); the proposal is best-effort — the decision
    transition is the authoritative outcome either way."""
    status = DECISION_ACTIONS.get(action)
    if status is None:
        return False, f"unknown action {action!r} (want one of {sorted(DECISION_ACTIONS)})"
    wake_ts = None
    if action == "snooze":
        try:
            minutes = int(params.get("minutes", [30])[0])
        except (ValueError, TypeError):
            return False, "snooze minutes must be an integer"
        wake_ts = time.time() + max(1, minutes) * 60
    try:
        decisions = _decisions_mod()
        rec, err = decisions.transition(
            dec_id, status, via=f"todo-server:{action}",
            note=(note or "").strip() or None, wake_ts=wake_ts)
    except Exception as e:
        return False, f"decision store unavailable: {e}"
    if err:
        return False, err
    proposal_note = ""
    if action == "wrong_lane":
        try:
            if str(SRC_DIR) not in sys.path:
                sys.path.insert(0, str(SRC_DIR))
            from assistant import policy  # noqa: PLC0415
            entry = policy.file_wrong_lane_proposal(rec)
            proposal_note = ("; policy proposal filed"
                             if entry is not None
                             else "; policy proposal already pending")
        except Exception as e:
            proposal_note = f"; policy proposal failed: {e}"
    rerender_dashboard()
    return True, f"{dec_id} -> {rec['status']}{proposal_note}"


def _goals_mod():
    """Import src/assistant/goals lazily — the goal routes must not keep the
    whole server from starting if the module is broken/absent (mirrors
    _decisions_mod)."""
    if str(SRC_DIR) not in sys.path:
        sys.path.insert(0, str(SRC_DIR))
    from assistant import goals  # noqa: PLC0415
    return goals


def goal_list():
    """The goals store in rank order (the /goal skill's `list` + dashboard)."""
    try:
        goals = _goals_mod()
        store = goals.load_goals()
        rows = goals.list_goals()
    except Exception as e:
        return False, f"goals store unavailable: {e}"
    return True, json.dumps({
        "goals": rows,
        "n": len(rows),
        "paused": bool(store.get("_paused", False)),
    }, ensure_ascii=False)


def _parse_json_body(body):
    try:
        obj = json.loads(body) if body else {}
    except json.JSONDecodeError:
        return None, "body must be JSON"
    if not isinstance(obj, dict):
        return None, "body must be a JSON object"
    return obj, None


def goal_add(body):
    """POST /goal/add — HUMAN path (localhost only, like every route). Body is a
    JSON object {title, outcome, rank?, horizon?, links?, playbook?, budget?}.
    Automation NEVER hits this; it files a goal_update proposal instead."""
    obj, err = _parse_json_body(body)
    if err:
        return False, err
    try:
        goals = _goals_mod()
        goal, gerr = goals.add_goal(
            title=obj.get("title") or "", outcome=obj.get("outcome") or "",
            rank=obj.get("rank"), horizon=obj.get("horizon"),
            links=obj.get("links"), playbook=obj.get("playbook"),
            budget=obj.get("budget"),
            stall_after_hours=obj.get("stallAfterHours"))
    except Exception as e:
        return False, f"goals store unavailable: {e}"
    if gerr:
        return False, gerr
    return True, json.dumps({"added": goal.get("id"), "rank": goal.get("rank")},
                            ensure_ascii=False)


# The design's central automation invariant: automation CANNOT directly flip a
# goal's status, repoint its links, or widen its playbook (m18). A direct edit
# of these fields is applied ONLY with an explicit human-origin assertion (the
# dashboard sets X-Assistant-Human; automation's localhost curl does not).
# Without it, the edit is FILED as a confirmation-gated goal_update proposal
# (m15 wires the producer) instead of applied.
_GOAL_SENSITIVE_FIELDS = {"status", "links", "playbook"}


def goal_update(goal_id, body, is_human=True):
    """POST /goal/update/<id>. A direct edit of a SENSITIVE field
    (status/links/playbook) requires the human-origin assertion; otherwise the
    change is filed as a confirmation-gated goal_update proposal, never applied
    (m18/m15). Non-sensitive human edits apply in place."""
    obj, err = _parse_json_body(body)
    if err:
        return False, err
    try:
        goals = _goals_mod()
    except Exception as e:
        return False, f"goals store unavailable: {e}"
    sensitive = set(obj) & _GOAL_SENSITIVE_FIELDS
    if sensitive and not is_human:
        # Automation-shaped request touching a gated field → file a proposal,
        # do NOT apply. This is the ONLY way automation reaches status/links/
        # playbook, and it never auto-applies.
        try:
            entry = goals.file_goal_update_proposal(
                goal_id, {k: obj[k] for k in obj},
                reason="automation /goal/update without human assertion",
                source="todo-server")
        except Exception as e:
            return False, f"goals store unavailable: {e}"
        if entry is None:
            return True, f"{goal_id}: goal_update proposal already pending (not applied)"
        return True, f"{goal_id}: filed goal_update proposal {entry.get('id')} (not applied)"
    try:
        goal, gerr = goals.update_goal(goal_id, obj)
    except Exception as e:
        return False, f"goals store unavailable: {e}"
    if gerr:
        return False, gerr
    return True, f"{goal_id} updated"


def goal_set_paused(paused):
    """POST /goal/pause | /goal/resume — the REAL kill switch (m7). The skill
    used to advertise pause but only ever sent a no-op /goal/update ping that
    returned success WITHOUT setting _paused (fail-open). This actually flips
    goals._paused so the planner then no-ops (ledgered)."""
    try:
        goals = _goals_mod()
        goals.set_paused(bool(paused))
    except Exception as e:
        return False, f"goals store unavailable: {e}"
    return True, json.dumps({"paused": bool(paused)}, ensure_ascii=False)


def goal_rerank(body):
    """POST /goal/rerank — HUMAN direct edit. Body {order:[goal-id,...]}."""
    obj, err = _parse_json_body(body)
    if err:
        return False, err
    order = obj.get("order")
    try:
        goals = _goals_mod()
        ok, gerr = goals.rerank(order if isinstance(order, list) else [])
    except Exception as e:
        return False, f"goals store unavailable: {e}"
    if not ok:
        return False, gerr or "rerank failed"
    return True, "reranked"


def brief_seen(params):
    """POST /brief/seen: stamp the seen sidecar for a brief (Keel M3). The
    sidecar — not the brief file — carries seen_ts, so the brief stays a
    pure, delete-safe derivation. Missing ?date targets the latest brief."""
    date = (params.get("date", [""])[0]) or None
    if date is not None and not BRIEF_DATE_RE.match(date):
        return False, f"invalid date {date!r} (want YYYY-MM-DD)"
    try:
        if str(SRC_DIR) not in sys.path:
            sys.path.insert(0, str(SRC_DIR))
        from assistant import brief  # noqa: PLC0415
        return brief.mark_seen(date)
    except Exception as e:
        return False, f"brief store unavailable: {e}"


def _slack_focus_store():
    if str(SRC_DIR) not in sys.path:
        sys.path.insert(0, str(SRC_DIR))
    from assistant.slack_focus import store  # noqa: PLC0415
    return store


def _reap_slack_focus():
    """Collect a finished refresh. If it exited before recording its own
    state, the state still names this server, so record the failure here."""
    global _slack_focus_proc
    if _slack_focus_proc is None or _slack_focus_proc.poll() is None:
        return
    _slack_focus_proc = None
    store = _slack_focus_store()
    state = store.read_json(store.base_dir(HOME) / "state.json", {})
    if isinstance(state, dict) and state.get("running") and state.get("pid") == os.getpid():
        store.write_state(HOME, running=False, ok=False, phase=None, finished=store.now_iso(),
                          error="The refresh didn't start. See ~/.assistant/slack-focus/refresh.log.")


def slack_focus_status():
    """POST /slack-focus/status: the refresh state the Slack tab's button polls."""
    _reap_slack_focus()
    return json.dumps(_slack_focus_store().read_state(HOME))


def slack_focus_refresh():
    """POST /slack-focus/refresh: start a refresh in the background, unless one
    is already running. It runs under zsh with ~/.zprofile sourced, because
    launchd doesn't give this server $SLACK_USER_TOKEN or the gh PATH."""
    global _slack_focus_proc
    _reap_slack_focus()
    store = _slack_focus_store()
    root = store.base_dir(HOME)
    with store.locked(root / "refresh.lock", blocking=False) as free:
        if not free:
            return json.dumps({"started": False})
    if store.read_state(HOME).get("running"):
        return json.dumps({"started": False})
    if _slack_focus_proc is not None:
        # The last launch never took the lock (a hung ~/.zprofile, say), and
        # its startup grace is over. Stop it so this one can run.
        try:
            os.killpg(_slack_focus_proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        _slack_focus_proc.wait()
        _slack_focus_proc = None
    store.write_state(HOME, running=True, pid=os.getpid(), started=store.now_iso(),
                      finished=None, ok=None, error=None, phase="Starting")
    try:
        with open(root / "refresh.log", "w") as log:
            _slack_focus_proc = subprocess.Popen(
                ["/bin/zsh", "-c", SLACK_FOCUS_LAUNCH, sys.executable, str(SLACK_FOCUS_SCRIPT)],
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    except OSError as e:
        store.write_state(HOME, running=False, ok=False, phase=None, finished=store.now_iso(),
                          error=f"The refresh couldn't start: {e}")
        return json.dumps({"started": False})
    return json.dumps({"started": True})


def slack_focus_mark(body, done):
    """POST /slack-focus/done | /slack-focus/undone  (body = {"fp": "<sha256>"})."""
    data, err = _parse_json_body(body)
    if err:
        return False, err
    fp = data.get("fp")
    if not isinstance(fp, str) or not SLACK_FOCUS_FP_RE.match(fp):
        return False, "fp must be a 64-character hex fingerprint"
    _slack_focus_store().set_done(HOME, fp, done)
    rerender_dashboard()
    return True, "marked done" if done else "marked not done"


class WorkspaceIdentityConflict(ValueError):
    """The requested workspace identity cannot be verified."""


def checked_workspace_id(ws_ref, observed_id):
    try:
        expected_id = str(UUID(observed_id))
    except ValueError:
        raise WorkspaceIdentityConflict("invalid workspace identity; refresh the dashboard") from None
    try:
        result = subprocess.run(
            [CMUX_BIN, "--id-format", "both", "tree", "--all", "--json"],
            check=False, timeout=4, capture_output=True, text=True,
        )
        if result.returncode != 0:
            raise WorkspaceIdentityConflict("workspace identity unavailable")
        tree = json.loads(result.stdout)
        matches = [
            workspace.get("id")
            for window in tree.get("windows", [])
            for workspace in window.get("workspaces", [])
            if workspace.get("ref") == ws_ref
        ]
    except (OSError, subprocess.TimeoutExpired, ValueError, AttributeError, TypeError):
        raise WorkspaceIdentityConflict("workspace identity unavailable; refresh the dashboard") from None
    if len(matches) != 1 or str(matches[0]).lower() != expected_id:
        raise WorkspaceIdentityConflict("workspace identity changed; refresh the dashboard")
    return expected_id


def focus_workspace(ws_ref, workspace_id=None):
    if not WORKSPACE_REF_RE.match(ws_ref):
        return False, f"invalid workspace ref {ws_ref!r}"
    target = checked_workspace_id(ws_ref, workspace_id) if workspace_id is not None else ws_ref
    try:
        result = subprocess.run(
            [CMUX_BIN, "select-workspace", "--workspace", target],
            check=False, timeout=4, capture_output=True, text=True,
        )
        if result.returncode != 0:
            return False, f"cmux error: {result.stderr.strip()}"
        return True, f"focused {ws_ref}"
    except Exception as e:
        return False, str(e)


def remove_item(td_id):
    with _todo_lock():
        data = load_json()
        items = data.get("items", []) or []
        idx = next((n for n, i in enumerate(items) if i.get("id") == td_id), None)
        if idx is None:
            return False, f"id {td_id!r} not found"
        item = items.pop(idx)
        item["removedAt"] = datetime.datetime.now(datetime.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        data.setdefault("removed", []).append(item)
        data["items"] = items
        save_json(data)
    _record("task", item, "removed", "dashboard", item.get("status"))
    rerender()
    return True, f"{td_id} removed"


# This server answers one connection at a time. A connection that opens and sends nothing (a browser keeps spare
# ones; so does anything that only probes the port) would otherwise hold it for as long as it stayed open, and the
# dashboard would stop answering. A connection is closed when the server has waited this many seconds for its next
# bytes. The limit is on each wait for the client, never on how long a route takes to do its work. It is not a
# defence against a program on this Mac that feeds a request in a byte at a time: each byte starts the wait again.
IDLE_TIMEOUT_SEC = 2
# Sending the answer gets a longer limit. For a send the limit is the total time, so two seconds would cut a
# large page off part-way when the reader is slow (a tunnel).
SEND_TIMEOUT_SEC = 30


class Handler(BaseHTTPRequestHandler):
    timeout = IDLE_TIMEOUT_SEC

    def log_message(self, fmt, *args):
        sys.stderr.write("[todo-server] " + redact(fmt % args) + "\n")

    def log_error(self, fmt, *args):
        if fmt.startswith("Request timed out") and not getattr(self, "raw_requestline", b""):
            return  # a spare connection that never sent a request: routine, and a line for each would bury the log
        super().log_error(fmt, *args)

    def end_headers(self):
        self.connection.settimeout(SEND_TIMEOUT_SEC)  # the request has been read; all that is left is sending
        super().end_headers()

    def _own_origins(self):
        """The origins that count as this server's own page: both loopback names on the port the
        request was sent to (a tab opened at localhost:P still posts to 127.0.0.1:P, and the other
        way round). Empty when the Host is not one of our names, or names no port we answer on."""
        host = self._header_once("Host")
        if host is None or not HOST_RE.fullmatch(host):
            return frozenset()
        port = host.rsplit(":", 1)[1]
        return frozenset({f"http://127.0.0.1:{port}", f"http://localhost:{port}"})

    def _cors(self):
        # Only the page this server serves (an Origin that is exactly one of our two loopback
        # names on the port it reached us by) or a local file (Origin: null) may read an answer. EXACT match: a prefix
        # match would wave through http://localhost.evil.com. A request with no Origin gets no
        # header at all.
        origin = self.headers.get("Origin", "")
        if origin == "null" or (origin and origin in self._own_origins()):
            self.send_header("Access-Control-Allow-Origin", origin)
            # The token header makes the local-file page's POSTs "non-simple",
            # so the browser asks first; without this it never sends them.
            self.send_header("Access-Control-Allow-Headers",
                             "Content-Type, X-Assistant-Human, X-Assistant-Token")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")

    def _header_once(self, name):
        """The header's value, or None when it is absent or sent twice (a second copy could be
        what a proxy or a script hides behind the first, so it is never guessed at)."""
        values = self.headers.get_all(name) or []
        return values[0] if len(values) == 1 else None

    def _wrong_host(self) -> bool:
        """DNS rebinding: a page on another name that resolves to 127.0.0.1 still sends its own
        name as Host. Only 127.0.0.1 and localhost are answered, on any port."""
        if self._own_origins():
            return False
        self._reply(403, "This dashboard only answers at an address that starts with http://127.0.0.1 "
                         "or http://localhost. If you reached it under another name, open it at "
                         "http://127.0.0.1:9876.")
        return True

    def _post_refused(self) -> bool:
        """The one guard every POST passes first, with no exceptions: the page this server serves
        (an Origin that is exactly 127.0.0.1 or localhost on the port it was reached by) is allowed; a missing
        Origin or Origin: null (the page opened as a local file, a command-line call, a sandboxed
        frame on any website) is allowed only with the per-install token; any other Origin,
        including a page on localhost at another port, is refused. Withholding the CORS header is
        not enough: a no-cors form or fetch ignores it and the write still lands."""
        if self._wrong_host():
            return True
        origin = self._header_once("Origin") if "Origin" in self.headers else ""
        if origin in self._own_origins():
            return False
        if "Origin" in self.headers and origin != "null":
            self._reply(403, "Refused. Reload the dashboard. Requests from another website are refused.")
            return True
        # A page can be reloaded; a command-line call has nothing to reload.
        lead = "Refused. Reload the dashboard. " if "Origin" in self.headers else "Refused. "
        try:
            token = _api_token_mod().ensure()
        except Exception:  # an unreadable folder, no HOME: refuse cleanly, never a dropped connection
            self._reply(403, lead + "The token in ~/.assistant/api-token can't be read.")
            return True
        supplied = self._header_once("X-Assistant-Token")
        if supplied is None:
            self._reply(403, lead + "This request needs the token in "
                             "~/.assistant/api-token, sent as the X-Assistant-Token header.")
            return True
        if not hmac.compare_digest(supplied.encode(), token.encode()):
            self._reply(403, lead + "That token isn't the one in "
                             "~/.assistant/api-token; send the current one as the X-Assistant-Token header.")
            return True
        return False

    def _is_human(self) -> bool:
        """The human-origin assertion (m18): the dashboard sets X-Assistant-Human
        on its goal-mutation fetches; automation's localhost curl does not. Used
        to gate direct edits of a goal's status/links/playbook."""
        return (self.headers.get("X-Assistant-Human", "") or "").strip() not in ("", "0", "false")

    def do_OPTIONS(self):
        if self._wrong_host():
            return
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        if self._wrong_host():
            return
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            # The page carries the token, so another website (or a sandboxed frame, whose
            # Origin is null) must not be able to read it with a cross-origin fetch.
            # A same-origin page load sends no Origin.
            if "Origin" in self.headers and self._header_once("Origin") not in self._own_origins():
                self._reply(403, "Refused. Reload the dashboard. Requests from another website are refused.")
                return
            try:
                body = DASHBOARD_HTML_PATH.read_bytes()
            except FileNotFoundError:
                self._reply(503, "dashboard not yet rendered — wait for next pulse")
                return
            self.send_response(200)
            self._cors()
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            # No other website may show this page in a frame (a click through it would be this
            # server's own page acting), read it, or embed it.
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", "frame-ancestors 'none'")
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/healthz":
            body = b"ok"
            self.send_response(200)
            self._cors()
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self._cors()
        self.end_headers()

    def _reply(self, code, text):
        body = text.encode()
        self.send_response(code)
        self._cors()
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self, max_bytes=64 * 1024):
        try:
            n = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            n = 0
        n = max(0, min(n, max_bytes))
        return self.rfile.read(n).decode("utf-8", errors="replace") if n else ""

    def do_POST(self):
        # Before any routing: every POST, reads included, passes the one guard.
        if self._post_refused():
            return
        parsed = urllib.parse.urlparse(self.path)
        parts = [p for p in parsed.path.split("/") if p]
        qs = urllib.parse.parse_qs(parsed.query)

        # POST /proposal/<id>/<action>
        if len(parts) == 3 and parts[0] == "proposal":
            _, prop_id, action = parts
            if not PROPOSAL_ID_RE.match(prop_id):
                self._reply(400, f"invalid proposal id {prop_id!r}")
                return
            ok, msg = proposal_action(prop_id, action, qs)
            self._reply(200 if ok else 400, msg)
            return

        # POST /decision/list — POST-only like every mutation-adjacent route.
        if parts == ["decision", "list"]:
            ok, msg = decision_list()
            if not ok:
                self._reply(503, msg)
                return
            body = msg.encode()
            self.send_response(200)
            self._cors()
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        # POST /decision/act/<dec-id>?action=...&minutes=N  (body = note)
        if len(parts) == 3 and parts[0] == "decision" and parts[1] == "act":
            dec_id = parts[2]
            if not DEC_ID_RE.match(dec_id):
                self._reply(400, f"invalid decision id {dec_id!r}")
                return
            action = (qs.get("action", [""])[0]) or ""
            note = self._read_body()
            ok, msg = decision_act(dec_id, action, qs, note)
            self._reply(200 if ok else (404 if "not found" in msg else 400), msg)
            return

        # POST /goal/list — POST-only like the other list routes (Keel M4).
        if parts == ["goal", "list"]:
            ok, msg = goal_list()
            if not ok:
                self._reply(503, msg)
                return
            body = msg.encode()
            self.send_response(200)
            self._cors()
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        # POST /goal/add  (body = goal JSON)  — HUMAN edit path.
        if parts == ["goal", "add"]:
            ok, msg = goal_add(self._read_body())
            self._reply(200 if ok else 400, msg)
            return

        # POST /goal/rerank  (body = {order:[...]}) — HUMAN edit path.
        if parts == ["goal", "rerank"]:
            ok, msg = goal_rerank(self._read_body())
            self._reply(200 if ok else 400, msg)
            return

        # POST /goal/pause | /goal/resume — the REAL kill switch (m7).
        if parts == ["goal", "pause"]:
            ok, msg = goal_set_paused(True)
            self._reply(200 if ok else 503, msg)
            return
        if parts == ["goal", "resume"]:
            ok, msg = goal_set_paused(False)
            self._reply(200 if ok else 503, msg)
            return

        # POST /goal/update/<goal-id>  (body = changes JSON). Sensitive-field
        # edits require the human-origin assertion (m18) or they file a proposal.
        if len(parts) == 3 and parts[0] == "goal" and parts[1] == "update":
            goal_id = parts[2]
            if not GOAL_ID_RE.match(goal_id):
                self._reply(400, f"invalid goal id {goal_id!r}")
                return
            ok, msg = goal_update(goal_id, self._read_body(),
                                  is_human=self._is_human())
            self._reply(200 if ok else (404 if "not found" in msg else 400), msg)
            return

        # POST /mode?value=pro|default — the dashboard's Pro mode switch.
        if parts == ["mode"]:
            if not self._is_human():
                self._reply(403, "mode changes need the dashboard's X-Assistant-Human header")
                return
            ok, msg = set_mode((qs.get("value") or [""])[0])
            self._reply(200 if ok else 400, msg)
            return

        if parts == ["brief", "refresh"]:
            rerender_dashboard()
            self._reply(200, "rebuilt")
            return

        # POST /brief/seen?date=YYYY-MM-DD — Brief-tab view signal (Keel M3).
        if parts == ["brief", "seen"]:
            ok, msg = brief_seen(qs)
            self._reply(200 if ok else (404 if "no brief" in msg else 400), msg)
            return

        # POST /slack-focus/{status,refresh,done,undone} — the Slack tab.
        if parts in (["slack-focus", "status"], ["slack-focus", "refresh"]):
            body = (slack_focus_status() if parts[1] == "status" else slack_focus_refresh()).encode()
            self.send_response(200)
            self._cors()
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parts in (["slack-focus", "done"], ["slack-focus", "undone"]):
            ok, msg = slack_focus_mark(self._read_body(), parts[1] == "done")
            self._reply(200 if ok else 400, msg)
            return

        # POST /focus/<workspace_ref>
        if len(parts) == 2 and parts[0] == "focus":
            identity = urllib.parse.parse_qs(
                parsed.query, keep_blank_values=True,
            ).get("workspace_id")
            if identity is not None and len(identity) != 1:
                self._reply(409, "ambiguous workspace identity; refresh the dashboard")
                return
            try:
                ok, msg = focus_workspace(
                    parts[1], workspace_id=identity[0] if identity is not None else None,
                )
            except WorkspaceIdentityConflict as error:
                self._reply(409, str(error))
                return
            self._reply(200 if ok else 400, msg)
            return

        # POST /append-detail/<id>  (body = additional context to append)
        if len(parts) == 2 and parts[0] == "append-detail":
            td_id = parts[1]
            if not TD_ID_RE.match(td_id):
                self._reply(400, f"invalid id {td_id!r}")
                return
            body = self._read_body()
            ok, msg = append_detail(td_id, body)
            self._reply(200 if ok else 400, msg)
            return

        # POST /dispatch-now/<id>  (force Bucket B at next pulse)
        if len(parts) == 2 and parts[0] == "dispatch-now":
            td_id = parts[1]
            if not TD_ID_RE.match(td_id):
                self._reply(400, f"invalid id {td_id!r}")
                return
            ok, msg = dispatch_now(td_id)
            self._reply(200 if ok else 404, msg)
            return

        if len(parts) != 2 or parts[0] not in ("toggle", "remove"):
            self._reply(404, "not found")
            return

        action, td_id = parts[0], parts[1]
        if not TD_ID_RE.match(td_id):
            self._reply(400, f"invalid id {td_id!r}")
            return
        if action == "remove":
            ok, msg = remove_item(td_id)
            self._reply(200 if ok else 404, msg)
            return

        # toggle: ?flag=autoDispatch&value=true|false|null
        flag = (qs.get("flag", ["autoDispatch"])[0]) or "autoDispatch"
        value_raw = (qs.get("value", ["true"])[0]) or "true"
        v = value_raw.lower()
        if v in ("null", "none", "unset", ""):
            value = None
        elif v in ("true", "1", "on", "yes"):
            value = True
        else:
            value = False
        if flag not in ALLOWED_FLAGS:
            self._reply(400, f"flag {flag!r} not allowed")
            return
        ok, msg = set_flag(td_id, flag, value)
        self._reply(200 if ok else 404, msg)


def main():
    install_crash_hooks()  # an uncaught error prints a redacted traceback
    httpd = HTTPServer((HOST, PORT), Handler)
    sys.stderr.write(f"[todo-server] listening on http://{HOST}:{PORT}\n")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
