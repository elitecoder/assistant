#!/usr/bin/env python3
"""Tiny HTTP daemon for the Assistant dashboards (TODO + main).

Bound to 127.0.0.1:9876 (localhost only). Endpoints:
  POST /toggle/<id>?flag=autoDispatch&value=true|false   flip a boolean flag
  POST /remove/<id>                                       soft-delete an item
                                                          (moved to removed[]
                                                          with removedAt stamp)
  POST /focus/<workspace_ref>                             switch the active
                                                          cmux workspace
                                                          (workspace:N only)
  GET  /                                                  health check ("ok")

On every successful TODO mutation, the JSON file is atomically rewritten and the
static HTML is re-rendered so the next file:// load reflects the new state.
/focus is a side-effect-only command (shells out to `cmux select-workspace`).

Stdlib only. Run by LaunchAgent com.assistant.assistant-todo-server.
"""

import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

WORKSPACE_REF_RE = re.compile(r"^workspace:\d+$")
TD_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
PROPOSAL_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,128}$")
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
ALLOWED_FLAGS = {"autoDispatch", "closeOnMerge"}
PORT = 9876
HOST = "127.0.0.1"


def load_json():
    return json.loads(JSON_PATH.read_text())


def save_json(data):
    tmp = JSON_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    os.replace(tmp, JSON_PATH)


def rerender():
    try:
        subprocess.run(
            ["python3", str(RENDER_SCRIPT)],
            check=False,
            timeout=4,
            capture_output=True,
        )
    except Exception:
        pass


def set_flag(td_id, flag, value):
    """Set a TODO's boolean-ish flag. value is True, False, or None."""
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
    data = load_json()
    items = data.get("items", []) or []
    target = next((i for i in items if i.get("id") == td_id), None)
    if target is None:
        return False, f"id {td_id!r} not found"
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    target["autoDispatch"] = True
    target["dispatchedAt"] = None
    target["dispatchedWs"] = None
    if target.get("status") in ("deferred", "blocked", "done"):
        target["status"] = "open"
        target["statusUpdatedAt"] = now
        target["statusReason"] = f"Re-opened via dashboard 'Dispatch now' at {now}"
    save_json(data)
    rerender()
    return True, f"{td_id}: queued for next-pulse Bucket B dispatch"


def rerender_dashboard():
    try:
        subprocess.run(
            ["python3", str(RENDER_DASHBOARD_SCRIPT)],
            check=False, timeout=6, capture_output=True,
        )
    except Exception:
        pass


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
                try:
                    subprocess.run(
                        [CMUX_BIN, "send", "--workspace", ws_ref, data.get("action", "")],
                        check=False, timeout=4, capture_output=True,
                    )
                    subprocess.run(
                        [CMUX_BIN, "send-key", "--workspace", ws_ref, "Enter"],
                        check=False, timeout=4, capture_output=True,
                    )
                except Exception as e:
                    return False, f"cmux send failed: {e}"
        data["status"] = "working"
        data.setdefault("thread", []).append({"ts": now, "actor": "assistant", "note": "Dispatched via dashboard Run button."})
    else:
        return False, f"unknown action {action!r}"
    save_proposal(path, data)
    rerender_dashboard()
    return True, f"{prop_id}.{action} ok"


def focus_workspace(ws_ref):
    if not WORKSPACE_REF_RE.match(ws_ref):
        return False, f"invalid workspace ref {ws_ref!r}"
    try:
        result = subprocess.run(
            [CMUX_BIN, "select-workspace", "--workspace", ws_ref],
            check=False, timeout=4, capture_output=True, text=True,
        )
        if result.returncode != 0:
            return False, f"cmux error: {result.stderr.strip()}"
        return True, f"focused {ws_ref}"
    except Exception as e:
        return False, str(e)


def remove_item(td_id):
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
    rerender()
    return True, f"{td_id} removed"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write("[todo-server] " + fmt % args + "\n")

    def _cors(self):
        origin = self.headers.get("Origin", "")
        # Only allow requests from the local dashboard (file:// or localhost).
        allowed = (
            origin.startswith("file://")
            or origin.startswith("http://127.0.0.1")
            or origin.startswith("http://localhost")
            or origin == "null"  # file:// sends Origin: null in many browsers
            or not origin  # same-origin requests omit the header
        )
        if allowed:
            self.send_header("Access-Control-Allow-Origin", origin or "null")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
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

        # POST /focus/<workspace_ref>
        if len(parts) == 2 and parts[0] == "focus":
            ok, msg = focus_workspace(parts[1])
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
    httpd = HTTPServer((HOST, PORT), Handler)
    sys.stderr.write(f"[todo-server] listening on http://{HOST}:{PORT}\n")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
