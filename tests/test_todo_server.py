"""Tests for bin/todo-server.py — the tiny localhost HTTP daemon backing the
Assistant dashboards.

The script is a hyphenated CLI, so it is loaded by file path (importlib). After
loading we monkeypatch the module-level path constants (JSON_PATH,
DASHBOARD_HTML_PATH, PROPOSALS_DIR, CMUX_BIN) to point under tmp_path, and stub
rerender / rerender_dashboard to no-ops so the heavy render script never runs.
The real ~/.claude state and real cmux are never touched. The HTTP Handler is
exercised for real over a loopback HTTPServer."""
from __future__ import annotations

import importlib.util
import json
import sys
import threading
import urllib.error
import urllib.request
from http.server import HTTPServer
from pathlib import Path

import pytest

_BIN = Path(__file__).resolve().parent.parent / "bin"


def _load(name: str, filename: str):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, str(_BIN / filename))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


mod = _load("todo_server", "todo-server.py")


# ─── shared fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Point the module's path constants at tmp files and neutralise side
    effects (render scripts). Returns the tmp paths for assertions."""
    json_path = tmp_path / "assistant-todo.json"
    dash_path = tmp_path / "assistant-dashboard.html"
    props_dir = tmp_path / "proposals"
    props_dir.mkdir()

    monkeypatch.setattr(mod, "JSON_PATH", json_path)
    monkeypatch.setattr(mod, "DASHBOARD_HTML_PATH", dash_path)
    monkeypatch.setattr(mod, "PROPOSALS_DIR", props_dir)
    monkeypatch.setattr(mod, "CMUX_BIN", "/bin/true")
    monkeypatch.setattr(mod, "rerender", lambda: None)
    monkeypatch.setattr(mod, "rerender_dashboard", lambda: None)

    return type("Env", (), {
        "json_path": json_path,
        "dash_path": dash_path,
        "props_dir": props_dir,
        "tmp": tmp_path,
    })()


def _seed_items(env, items, **extra):
    data = {"items": items}
    data.update(extra)
    env.json_path.write_text(json.dumps(data))
    return data


def _read(env):
    return json.loads(env.json_path.read_text())


def _seed_proposal(env, prop):
    p = env.props_dir / f"{prop['id']}.json"
    p.write_text(json.dumps(prop))
    return p


# ─── A) pure functions ────────────────────────────────────────────────────────


def test_load_save_json_roundtrip_atomic_newline(env):
    payload = {"items": [{"id": "td-1"}], "note": "héllo"}
    mod.save_json(payload)
    # save_json writes atomically (tmp then replace) and appends a trailing \n
    raw = env.json_path.read_text()
    assert raw.endswith("\n")
    assert not (env.json_path.with_suffix(".json.tmp")).exists()
    assert mod.load_json() == payload


def test_rerender_invokes_subprocess(env, monkeypatch):
    # rerender() is no-op'd by the env fixture; restore the real one and verify
    # it shells out to the render script (subprocess stubbed).
    monkeypatch.undo()  # drop env fixture's monkeypatches, then re-stub subprocess
    calls = []
    monkeypatch.setattr(mod.subprocess, "run", lambda argv, **kw: calls.append(argv))
    mod.rerender()
    assert calls and calls[0][0] == "python3"
    assert str(mod.RENDER_SCRIPT) in calls[0][1]


def test_rerender_swallows_exception(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("render blew up")

    monkeypatch.setattr(mod.subprocess, "run", boom)
    # must not raise
    mod.rerender()


def test_rerender_dashboard_invokes_subprocess(env, monkeypatch):
    monkeypatch.undo()
    calls = []
    monkeypatch.setattr(mod.subprocess, "run", lambda argv, **kw: calls.append(argv))
    mod.rerender_dashboard()
    assert calls and calls[0][0] == "python3"


def test_rerender_dashboard_swallows_exception(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("nope")

    monkeypatch.setattr(mod.subprocess, "run", boom)
    mod.rerender_dashboard()


def test_set_flag_existing(env):
    _seed_items(env, [{"id": "td-1", "autoDispatch": False}])
    ok, msg = mod.set_flag("td-1", "autoDispatch", True)
    assert ok is True
    assert "autoDispatch" in msg
    assert _read(env)["items"][0]["autoDispatch"] is True


def test_set_flag_none_value(env):
    _seed_items(env, [{"id": "td-1"}])
    ok, _ = mod.set_flag("td-1", "autoDispatch", None)
    assert ok is True
    assert _read(env)["items"][0]["autoDispatch"] is None


def test_set_flag_missing(env):
    _seed_items(env, [{"id": "td-1"}])
    ok, msg = mod.set_flag("nope", "autoDispatch", True)
    assert ok is False
    assert "not found" in msg
    # file unchanged
    assert "autoDispatch" not in _read(env)["items"][0]


def test_append_detail_empty_body(env):
    _seed_items(env, [{"id": "td-1"}])
    ok, msg = mod.append_detail("td-1", "   ")
    assert ok is False
    assert msg == "empty body"


def test_append_detail_first(env):
    _seed_items(env, [{"id": "td-1"}])
    ok, _ = mod.append_detail("td-1", "first note")
    assert ok is True
    detail = _read(env)["items"][0]["detail"]
    assert detail.startswith("[mukul ")
    assert detail.endswith("first note")
    assert "\n\n" not in detail


def test_append_detail_separator_on_prior(env):
    _seed_items(env, [{"id": "td-1", "detail": "old context"}])
    ok, _ = mod.append_detail("td-1", "new note")
    assert ok is True
    detail = _read(env)["items"][0]["detail"]
    assert detail.startswith("old context")
    assert "\n\n[mukul " in detail
    assert detail.endswith("new note")


def test_append_detail_missing(env):
    _seed_items(env, [{"id": "td-1"}])
    ok, _ = mod.append_detail("ghost", "text")
    assert ok is False


def test_dispatch_now_missing(env):
    _seed_items(env, [{"id": "td-1"}])
    ok, msg = mod.dispatch_now("ghost")
    assert ok is False
    assert "not found" in msg


def test_dispatch_now_open_status_untouched(env):
    _seed_items(env, [{
        "id": "td-1", "status": "open",
        "dispatchedAt": "2026-01-01T00:00:00Z", "dispatchedWs": "workspace:5",
    }])
    ok, _ = mod.dispatch_now("td-1")
    assert ok is True
    item = _read(env)["items"][0]
    assert item["autoDispatch"] is True
    assert item["dispatchedAt"] is None
    assert item["dispatchedWs"] is None
    # status was "open" so it is not flipped and no statusReason added
    assert item["status"] == "open"
    assert "statusReason" not in item


@pytest.mark.parametrize("status", ["deferred", "blocked", "done"])
def test_dispatch_now_reopens(env, status):
    _seed_items(env, [{"id": "td-1", "status": status}])
    ok, _ = mod.dispatch_now("td-1")
    assert ok is True
    item = _read(env)["items"][0]
    assert item["status"] == "open"
    assert item["statusReason"].startswith("Re-opened via dashboard")
    assert "statusUpdatedAt" in item


def test_load_proposal_match(env):
    _seed_proposal(env, {"id": "prop-1", "action": "do thing"})
    path, data = mod.load_proposal("prop-1")
    assert path is not None
    assert data["action"] == "do thing"


def test_load_proposal_no_match(env):
    _seed_proposal(env, {"id": "prop-1"})
    path, data = mod.load_proposal("other")
    assert path is None and data is None


def test_load_proposal_missing_dir(env, monkeypatch):
    monkeypatch.setattr(mod, "PROPOSALS_DIR", env.tmp / "does-not-exist")
    path, data = mod.load_proposal("prop-1")
    assert path is None and data is None


def test_load_proposal_skips_bad_json(env):
    (env.props_dir / "broken.json").write_text("{not valid json")
    _seed_proposal(env, {"id": "prop-good"})
    path, data = mod.load_proposal("prop-good")
    assert data["id"] == "prop-good"


def test_save_proposal_roundtrip(env):
    p = env.props_dir / "prop-x.json"
    mod.save_proposal(p, {"id": "prop-x", "v": 1})
    assert p.read_text().endswith("\n")
    assert json.loads(p.read_text())["v"] == 1


def test_proposal_action_missing(env):
    ok, msg = mod.proposal_action("ghost", "hold", {})
    assert ok is False
    assert "not found" in msg


def test_proposal_action_unknown(env):
    _seed_proposal(env, {"id": "prop-1"})
    ok, msg = mod.proposal_action("prop-1", "frobnicate", {})
    assert ok is False
    assert "unknown action" in msg


def test_proposal_action_hold_unhold(env):
    p = _seed_proposal(env, {"id": "prop-1"})
    ok, _ = mod.proposal_action("prop-1", "hold", {})
    assert ok is True
    d = json.loads(p.read_text())
    assert d["held"] is True
    assert d["thread"][-1]["note"] == "Held via dashboard."

    ok, _ = mod.proposal_action("prop-1", "unhold", {})
    assert ok is True
    d = json.loads(p.read_text())
    assert d["held"] is False
    assert d["thread"][-1]["note"] == "Unheld via dashboard."


def test_proposal_action_snooze_parses_minutes(env):
    p = _seed_proposal(env, {"id": "prop-1"})
    ok, _ = mod.proposal_action("prop-1", "snooze", {"minutes": ["45"]})
    assert ok is True
    d = json.loads(p.read_text())
    assert "snoozed_to" in d
    assert "Snoozed 45m" in d["thread"][-1]["note"]


def test_proposal_action_snooze_default_minutes(env):
    p = _seed_proposal(env, {"id": "prop-1"})
    ok, _ = mod.proposal_action("prop-1", "snooze", {})
    assert ok is True
    d = json.loads(p.read_text())
    assert "Snoozed 30m" in d["thread"][-1]["note"]


def test_proposal_action_veto(env):
    p = _seed_proposal(env, {"id": "prop-1"})
    ok, _ = mod.proposal_action("prop-1", "veto", {})
    assert ok is True
    d = json.loads(p.read_text())
    assert d["status"] == "vetoed"
    assert d["thread"][-1]["note"] == "Vetoed via dashboard."


def test_proposal_action_run_now_dispatches(env, monkeypatch):
    p = _seed_proposal(env, {
        "id": "prop-1",
        "execute_via": "send-text-to-session",
        "action": "do the thing",
        "touches": [{"type": "session", "ref": "workspace:7"}],
    })
    calls = []
    monkeypatch.setattr(mod.subprocess, "run",
                        lambda argv, **kw: calls.append(argv))
    ok, _ = mod.proposal_action("prop-1", "run-now", {})
    assert ok is True
    # cmux send + send-key both invoked with the workspace ref
    assert calls[0] == ["/bin/true", "send", "--workspace", "workspace:7", "do the thing"]
    assert calls[1] == ["/bin/true", "send-key", "--workspace", "workspace:7", "Enter"]
    d = json.loads(p.read_text())
    assert d["status"] == "working"
    assert d["thread"][-1]["actor"] == "assistant"


def test_proposal_action_run_now_no_session_ref(env, monkeypatch):
    # execute_via is send-text-to-session but no session-type touch → no cmux,
    # still flips to working.
    p = _seed_proposal(env, {
        "id": "prop-1",
        "execute_via": "send-text-to-session",
        "touches": [{"type": "file", "ref": "/x"}],
    })
    calls = []
    monkeypatch.setattr(mod.subprocess, "run",
                        lambda argv, **kw: calls.append(argv))
    ok, _ = mod.proposal_action("prop-1", "run-now", {})
    assert ok is True
    assert calls == []
    assert json.loads(p.read_text())["status"] == "working"


def test_proposal_action_run_now_cmux_failure(env, monkeypatch):
    p = _seed_proposal(env, {
        "id": "prop-1",
        "execute_via": "send-text-to-session",
        "action": "x",
        "touches": [{"type": "session", "ref": "workspace:7"}],
    })

    def boom(*a, **k):
        raise RuntimeError("cmux down")

    monkeypatch.setattr(mod.subprocess, "run", boom)
    ok, msg = mod.proposal_action("prop-1", "run-now", {})
    assert ok is False
    assert "cmux send failed" in msg


def test_proposal_action_run_now_other_execute_via(env, monkeypatch):
    # execute_via not "send-text-to-session" → no subprocess, just flips status.
    p = _seed_proposal(env, {"id": "prop-1", "execute_via": "manual"})
    monkeypatch.setattr(mod.subprocess, "run",
                        lambda *a, **k: pytest.fail("should not shell out"))
    ok, _ = mod.proposal_action("prop-1", "run-now", {})
    assert ok is True
    assert json.loads(p.read_text())["status"] == "working"


def test_focus_workspace_invalid(env):
    ok, msg = mod.focus_workspace("notaref")
    assert ok is False
    assert "invalid workspace ref" in msg


def test_focus_workspace_ok(env, monkeypatch):
    def fake_run(argv, **kw):
        assert argv == ["/bin/true", "select-workspace", "--workspace", "workspace:3"]
        return type("R", (), {"returncode": 0, "stderr": ""})()

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    ok, msg = mod.focus_workspace("workspace:3")
    assert ok is True
    assert "focused workspace:3" in msg


def test_focus_workspace_cmux_error(env, monkeypatch):
    monkeypatch.setattr(mod.subprocess, "run",
                        lambda *a, **k: type("R", (), {"returncode": 1, "stderr": "boom\n"})())
    ok, msg = mod.focus_workspace("workspace:3")
    assert ok is False
    assert msg == "cmux error: boom"


def test_focus_workspace_exception(env, monkeypatch):
    def boom(*a, **k):
        raise OSError("no such binary")

    monkeypatch.setattr(mod.subprocess, "run", boom)
    ok, msg = mod.focus_workspace("workspace:3")
    assert ok is False
    assert msg == "no such binary"


def test_remove_item_missing(env):
    _seed_items(env, [{"id": "td-1"}])
    ok, msg = mod.remove_item("ghost")
    assert ok is False
    assert "not found" in msg


def test_remove_item_existing(env):
    _seed_items(env, [{"id": "td-1"}, {"id": "td-2"}])
    ok, _ = mod.remove_item("td-1")
    assert ok is True
    data = _read(env)
    assert [i["id"] for i in data["items"]] == ["td-2"]
    assert data["removed"][0]["id"] == "td-1"
    assert "removedAt" in data["removed"][0]


# ─── B) the HTTP Handler over a real loopback socket ──────────────────────────


@pytest.fixture
def server(env):
    """Start the real HTTPServer on an ephemeral port. env fixture has already
    pointed the module constants at tmp files."""
    srv = HTTPServer(("127.0.0.1", 0), mod.Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    port = srv.server_address[1]
    base = f"http://127.0.0.1:{port}"
    try:
        yield type("Srv", (), {"base": base, "env": env})()
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=5)


def _request(base, method, path, body=None, headers=None):
    """Returns (status, body_text). Non-2xx is captured via HTTPError."""
    data = body.encode() if isinstance(body, str) else body
    req = urllib.request.Request(base + path, data=data, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def test_get_root_serves_dashboard(server):
    server.env.dash_path.write_text("<html>dash</html>")
    status, body = _request(server.base, "GET", "/")
    assert status == 200
    assert body == "<html>dash</html>"


def test_get_root_503_when_missing(server):
    # dash_path does not exist
    status, body = _request(server.base, "GET", "/")
    assert status == 503
    assert "not yet rendered" in body


def test_get_index_html_alias(server):
    server.env.dash_path.write_text("<html>idx</html>")
    status, body = _request(server.base, "GET", "/index.html")
    assert status == 200
    assert body == "<html>idx</html>"


def test_get_healthz(server):
    status, body = _request(server.base, "GET", "/healthz")
    assert status == 200
    assert body == "ok"


def test_get_404(server):
    status, _ = _request(server.base, "GET", "/nonexistent")
    assert status == 404


def test_options_cors(server):
    req = urllib.request.Request(server.base + "/", method="OPTIONS",
                                 headers={"Origin": "null"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        assert resp.status == 204
        assert resp.headers.get("Access-Control-Allow-Methods") is not None
        assert resp.headers.get("Access-Control-Allow-Origin") == "null"


def test_post_toggle_true(server):
    _seed_items(server.env, [{"id": "td-1", "autoDispatch": False}])
    status, _ = _request(server.base, "POST", "/toggle/td-1?flag=autoDispatch&value=true")
    assert status == 200
    assert _read(server.env)["items"][0]["autoDispatch"] is True


def test_post_toggle_false(server):
    _seed_items(server.env, [{"id": "td-1", "autoDispatch": True}])
    status, _ = _request(server.base, "POST", "/toggle/td-1?flag=autoDispatch&value=false")
    assert status == 200
    assert _read(server.env)["items"][0]["autoDispatch"] is False


def test_post_toggle_null(server):
    _seed_items(server.env, [{"id": "td-1", "autoDispatch": True}])
    status, _ = _request(server.base, "POST", "/toggle/td-1?flag=autoDispatch&value=null")
    assert status == 200
    assert _read(server.env)["items"][0]["autoDispatch"] is None


def test_post_toggle_flag_not_allowed(server):
    _seed_items(server.env, [{"id": "td-1"}])
    status, body = _request(server.base, "POST", "/toggle/td-1?flag=evil&value=true")
    assert status == 400
    assert "not allowed" in body


def test_post_toggle_bad_id(server):
    _seed_items(server.env, [{"id": "td-1"}])
    # id with a disallowed char (space encoded) fails TD_ID_RE
    status, _ = _request(server.base, "POST", "/toggle/bad%20id?flag=autoDispatch&value=true")
    assert status == 400


def test_post_toggle_missing_id(server):
    _seed_items(server.env, [{"id": "td-1"}])
    status, _ = _request(server.base, "POST", "/toggle/td-999?flag=autoDispatch&value=true")
    assert status == 404


def test_post_toggle_closeonmerge_allowed(server):
    _seed_items(server.env, [{"id": "td-1"}])
    status, _ = _request(server.base, "POST", "/toggle/td-1?flag=closeOnMerge&value=true")
    assert status == 200
    assert _read(server.env)["items"][0]["closeOnMerge"] is True


def test_post_remove_existing(server):
    _seed_items(server.env, [{"id": "td-1"}])
    status, _ = _request(server.base, "POST", "/remove/td-1")
    assert status == 200
    assert _read(server.env)["items"] == []


def test_post_remove_missing(server):
    _seed_items(server.env, [{"id": "td-1"}])
    status, _ = _request(server.base, "POST", "/remove/td-999")
    assert status == 404


def test_post_focus_valid(server, monkeypatch):
    monkeypatch.setattr(mod.subprocess, "run",
                        lambda *a, **k: type("R", (), {"returncode": 0, "stderr": ""})())
    status, body = _request(server.base, "POST", "/focus/workspace:4")
    assert status == 200
    assert "focused" in body


def test_post_focus_invalid(server):
    status, _ = _request(server.base, "POST", "/focus/garbage")
    assert status == 400


def test_post_append_detail(server):
    _seed_items(server.env, [{"id": "td-1"}])
    status, _ = _request(server.base, "POST", "/append-detail/td-1",
                         body="extra context", headers={"Content-Type": "text/plain"})
    assert status == 200
    assert "extra context" in _read(server.env)["items"][0]["detail"]


def test_post_append_detail_empty_body_400(server):
    _seed_items(server.env, [{"id": "td-1"}])
    # empty body → append_detail returns False → 400
    status, _ = _request(server.base, "POST", "/append-detail/td-1", body="")
    assert status == 400


def test_post_append_detail_bad_id(server):
    status, _ = _request(server.base, "POST", "/append-detail/bad%20id", body="x")
    assert status == 400


def test_post_dispatch_now_existing(server):
    _seed_items(server.env, [{"id": "td-1", "status": "deferred"}])
    status, _ = _request(server.base, "POST", "/dispatch-now/td-1")
    assert status == 200
    item = _read(server.env)["items"][0]
    assert item["status"] == "open"
    assert item["autoDispatch"] is True


def test_post_dispatch_now_missing(server):
    _seed_items(server.env, [{"id": "td-1"}])
    status, _ = _request(server.base, "POST", "/dispatch-now/td-999")
    assert status == 404


def test_post_dispatch_now_bad_id(server):
    status, _ = _request(server.base, "POST", "/dispatch-now/bad%20id")
    assert status == 400


def test_post_proposal_action(server):
    _seed_proposal(server.env, {"id": "prop-1"})
    status, _ = _request(server.base, "POST", "/proposal/prop-1/hold")
    assert status == 200
    d = json.loads((server.env.props_dir / "prop-1.json").read_text())
    assert d["held"] is True


def test_post_proposal_unknown_action_400(server):
    _seed_proposal(server.env, {"id": "prop-1"})
    status, body = _request(server.base, "POST", "/proposal/prop-1/frobnicate")
    assert status == 400
    assert "unknown action" in body


def test_post_proposal_bad_id(server):
    # id with disallowed char fails PROPOSAL_ID_RE
    status, _ = _request(server.base, "POST", "/proposal/bad%20id/hold")
    assert status == 400


def test_post_proposal_snooze_with_minutes_query(server):
    _seed_proposal(server.env, {"id": "prop-1"})
    status, _ = _request(server.base, "POST", "/proposal/prop-1/snooze?minutes=45")
    assert status == 200
    d = json.loads((server.env.props_dir / "prop-1.json").read_text())
    assert "Snoozed 45m" in d["thread"][-1]["note"]


def test_post_unknown_path_404(server):
    status, _ = _request(server.base, "POST", "/wat/td-1")
    assert status == 404


def test_post_too_many_parts_404(server):
    status, _ = _request(server.base, "POST", "/toggle/td-1/extra")
    assert status == 404


# ─── _read_body bad Content-Length branch (driven against a fake Handler) ─────


def _make_handler(content_length: str, body: bytes = b""):
    """Construct a Handler without running BaseHTTPRequestHandler.__init__ (which
    would parse a real request); attach fake rfile/headers so _read_body can be
    exercised in isolation."""
    import io

    h = mod.Handler.__new__(mod.Handler)
    h.rfile = io.BytesIO(body)
    h.headers = {"Content-Length": content_length}
    return h


def test_read_body_bad_content_length_returns_empty():
    # int("not-a-number") raises ValueError → n=0 → returns "".
    h = _make_handler("not-a-number", b"ignored")
    assert h._read_body() == ""


def test_read_body_reads_n_bytes():
    h = _make_handler("5", b"hello world")
    assert h._read_body() == "hello"


def test_read_body_zero_length():
    h = _make_handler("0", b"hello")
    assert h._read_body() == ""
