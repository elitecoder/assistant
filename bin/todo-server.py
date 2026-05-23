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

Stdlib only. Run by LaunchAgent com.mukuls.assistant-todo-server.
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
CMUX_BIN = shutil.which("cmux") or "/Applications/cmux.app/Contents/Resources/bin/cmux"

HOME = Path(os.path.expanduser("~"))
JSON_PATH = HOME / ".claude" / "assistant-todo.json"
RENDER_SCRIPT = HOME / ".claude" / "bin" / "render-todo.py"
RENDER_DASHBOARD_SCRIPT = HOME / ".claude" / "bin" / "render-dashboard.py"
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


def toggle_flag(td_id, flag, value):
    data = load_json()
    items = data.get("items", []) or []
    target = next((i for i in items if i.get("id") == td_id), None)
    if target is None:
        return False, f"id {td_id!r} not found"
    target[flag] = bool(value)
    save_json(data)
    rerender()
    return True, f"{td_id}.{flag} = {bool(value)}"


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
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/":
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

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        parts = [p for p in parsed.path.split("/") if p]
        qs = urllib.parse.parse_qs(parsed.query)

        # POST /proposal/<id>/<action>
        if len(parts) == 3 and parts[0] == "proposal":
            _, prop_id, action = parts
            ok, msg = proposal_action(prop_id, action, qs)
            self._reply(200 if ok else 400, msg)
            return

        # POST /focus/<workspace_ref>
        if len(parts) == 2 and parts[0] == "focus":
            ok, msg = focus_workspace(parts[1])
            self._reply(200 if ok else 400, msg)
            return

        if len(parts) != 2 or parts[0] not in ("toggle", "remove"):
            self._reply(404, "not found")
            return

        action, td_id = parts[0], parts[1]
        if action == "remove":
            ok, msg = remove_item(td_id)
            self._reply(200 if ok else 404, msg)
            return

        # toggle
        flag = (qs.get("flag", ["autoDispatch"])[0]) or "autoDispatch"
        value_raw = (qs.get("value", ["true"])[0]) or "true"
        value = value_raw.lower() in ("true", "1", "on", "yes")
        if flag not in ALLOWED_FLAGS:
            self._reply(400, f"flag {flag!r} not allowed")
            return
        ok, msg = toggle_flag(td_id, flag, value)
        self._reply(200 if ok else 404, msg)


def main():
    httpd = HTTPServer((HOST, PORT), Handler)
    sys.stderr.write(f"[todo-server] listening on http://{HOST}:{PORT}\n")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
