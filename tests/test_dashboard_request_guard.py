"""The one guard on every request to the dashboard server (bin/todo-server.py).

The server answers only at 127.0.0.1 or localhost, on any port. A POST from the page the server serves (an
Origin that is exactly http:// plus the Host the request was sent to) passes; one with no Origin or Origin: null
(the page opened as a local file, curl, a sandboxed frame on any website) needs the per-install token; any other
Origin is refused. No route is exempt, so the route list is read from the server's own source.

The server runs for real over a loopback socket, with HOME and every store pointed under a tmp folder.
"""
from __future__ import annotations

import ast
import http.client
import importlib.util
import json
import os
import re
import stat
import subprocess
import sys
import threading
import types
from http.server import HTTPServer
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
from assistant import apitoken, decisions, goals  # noqa: E402


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, REPO / "bin" / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


mod = _load("todo_server_request_guard", "todo-server.py")

SEED_ITEMS = [{"id": "td-1", "title": "x", "autoDispatch": False}]
SEED_PROPOSALS = {
    "prop-1": {"id": "prop-1", "status": "pending", "type": "note", "title": "p"},
    "prop-2": {"id": "prop-2", "status": "pending", "type": "note", "title": "q", "action": "do it",
               "execute_via": "send-text-to-session", "touches": [{"type": "session", "ref": "workspace:2"}]},
}


def post_function():
    tree = ast.parse((REPO / "bin" / "todo-server.py").read_text())
    handler = next(n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == "Handler")
    return handler, next(n for n in handler.body if isinstance(n, ast.FunctionDef) and n.name == "do_POST")


def route_words() -> list[str]:
    """Every word the server's do_POST compares the request path with, read from its source."""
    words = set()
    for compare in (n for n in ast.walk(post_function()[1]) if isinstance(n, ast.Compare)):
        if "parts" not in ast.unparse(compare.left):
            continue
        for constant in (n for part in compare.comparators for n in ast.walk(part) if isinstance(n, ast.Constant)):
            if isinstance(constant.value, str) and re.fullmatch(r"[a-z][a-z-]*", constant.value):
                words.add(constant.value)
    return sorted(words)


def every_path() -> list[str]:
    words = route_words()
    paths = {"/", "/index.html", "/healthz", "/no-such-route", "/no/such/route/at/all"}
    for first in words:
        paths.add(f"/{first}")
        paths.add(f"/{first}/td-1")
        paths.add(f"/{first}/td-1/accept")
        for second in words:
            paths.add(f"/{first}/{second}")
            paths.add(f"/{first}/{second}/td-1")
    return sorted(paths)


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(mod, "HOME", tmp_path)
    return tmp_path


@pytest.fixture
def calls(home, monkeypatch):
    """Every side effect the server could cause outside its own files: processes and page rebuilds."""
    made = []
    (home / "proposals").mkdir()
    monkeypatch.setattr(mod, "JSON_PATH", home / "todo.json")
    monkeypatch.setattr(mod, "DASHBOARD_HTML_PATH", home / "dashboard.html")
    monkeypatch.setattr(mod, "PROPOSALS_DIR", home / "proposals")
    monkeypatch.setattr(mod, "CMUX_BIN", "/bin/true")
    monkeypatch.setattr(mod, "rerender", lambda: made.append("rerender"))
    monkeypatch.setattr(mod, "rerender_dashboard", lambda: made.append("rerender_dashboard"))
    monkeypatch.setattr(mod, "set_mode", lambda value: made.append(("mode", value)) or (True, value))
    monkeypatch.setattr(mod, "slack_focus_refresh", lambda: made.append("slack refresh") or '{"started": false}')

    def run(argv, **kwargs):
        made.append(("run", argv))
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    def never(*args, **kwargs):
        made.append(("process", args))
        raise AssertionError("no other process may start")

    monkeypatch.setattr(mod.subprocess, "run", run)
    for name in ("Popen", "call", "check_call", "check_output"):
        monkeypatch.setattr(mod.subprocess, name, never)
    return made


def seed(home):
    (home / "todo.json").write_text(json.dumps({"items": SEED_ITEMS}))
    for name, proposal in SEED_PROPOSALS.items():
        (home / "proposals" / f"{name}.json").write_text(json.dumps(proposal))
    record, _ = decisions.open_decision(
        event={"schema": "world-event/1", "id": "eid-1", "source": "cmux", "kind": "needs_input",
               "external_id": "cmux:workspace:1:needs_input:1:aa", "title": "t", "snippet": "s",
               "refs": {}, "raw_path": None},
        lane="escalate", policy_id="r1", urgency="now", now=1783000000.0)
    goals.add_goal(title="a", outcome="o")
    return record["id"]


@pytest.fixture
def server(home, calls):
    decision = seed(home)
    httpd = HTTPServer(("127.0.0.1", 0), mod.Handler)
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    port = httpd.server_address[1]
    yield types.SimpleNamespace(port=port, home=home, calls=calls, decision=decision, origin=f"http://127.0.0.1:{port}")
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=5)


def send(server, method, path, headers=(), host="default", body=b""):
    """One raw request. `headers` is a list of (name, value) pairs, so a header can be sent twice."""
    connection = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
    connection.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
    for value in ([f"127.0.0.1:{server.port}"] if host == "default" else host):
        connection.putheader("Host", value)
    for name, value in headers:
        connection.putheader(name, value)
    connection.putheader("Content-Length", str(len(body)))
    connection.endheaders(body)
    response = connection.getresponse()
    answer = (response.status, response.read().decode(), response)
    connection.close()
    return answer


def token():
    return apitoken.ensure()


def state(home):
    """Every file under HOME except the token and its lock, as bytes: a refused request must leave it identical."""
    return {str(p.relative_to(home)): p.read_bytes() for p in sorted(home.rglob("*"))
            if p.is_file() and not p.name.startswith("api-token")}


FOREIGN = ["https://evil.example", "http://localhost.evil.com:9876", "http://127.0.0.1:9999",
           "http://127.0.0.1", "https://127.0.0.1:9876", "http://localhost:9876", "http://localhost:3000", ""]

SIX_ROUTES = ["/toggle/td-1?flag=autoDispatch&value=true", "/remove/td-1", "/append-detail/td-1",
              "/focus/workspace:1", "/dispatch-now/td-1", "/proposal/prop-1/dismiss"]


def test_the_route_list_is_read_from_the_source_and_covers_the_known_routes():
    words = route_words()
    for expected in ("toggle", "remove", "focus", "dispatch-now", "proposal", "decision", "goal", "slack-focus",
                     "mode", "brief", "append-detail", "list", "status"):
        assert expected in words
    assert len(every_path()) > 100


# ─── nothing is exempt, and nothing else is a way in ────────────────────────────────────────────


def test_the_guard_is_the_first_thing_do_post_does():
    handler, post = post_function()
    first = next(n for n in post.body if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)))
    assert ast.unparse(first.test) == "self._post_refused()" and isinstance(first.body[0], ast.Return)


def test_no_other_method_has_a_handler_and_each_is_answered_501(server):
    for name in ("do_PUT", "do_DELETE", "do_PATCH", "do_HEAD", "do_TRACE", "do_CONNECT"):
        assert not hasattr(mod.Handler, name), name
    before = state(server.home)
    for method in ("PUT", "DELETE", "PATCH", "HEAD", "TRACE"):
        assert send(server, method, "/remove/td-1", [("X-Assistant-Token", token())])[0] == 501, method
    assert state(server.home) == before and server.calls == []


def test_no_get_changes_anything(server):
    (server.home / "dashboard.html").write_text("<html>page</html>")
    before = state(server.home)
    for path in every_path():
        for headers in ([], [("X-Assistant-Token", token())], [("Origin", server.origin)]):
            send(server, "GET", path, headers)
    assert state(server.home) == before and server.calls == []


HOLES = [
    [("Origin", "https://evil.example")],
    [("Origin", "null")],
    [],
    [("Origin", "null"), ("X-Assistant-Token", "wrong")],
]
# Every odd way to present an origin or a token, tried on the single-word paths and the unknown ones.
ODD_HOLES = [
    *[[("Origin", origin)] for origin in FOREIGN],
    *[[("Origin", origin), ("X-Assistant-Token", "RIGHT")] for origin in FOREIGN],  # a token never rescues one
    [("X-Assistant-Token", "wrong")],
    [("Origin", "null"), ("X-Assistant-Token", "")],
    [("Origin", "null"), ("X-Assistant-Token", "RIGHT"), ("X-Assistant-Token", "RIGHT")],
    [("Origin", "null"), ("Origin", "OWN"), ("X-Assistant-Token", "RIGHT")],
    [("Origin", "OWN"), ("Origin", "OWN")],
]


@pytest.mark.parametrize("path", [p for p in every_path() if p not in ("/", "/index.html", "/healthz")])
def test_no_post_route_answers_a_foreign_origin_or_a_nameless_caller(server, path):
    before = state(server.home)
    holes = HOLES + (ODD_HOLES if path.count("/") < 2 else [])
    for headers in holes:
        headers = [(n, token() if v == "RIGHT" else server.origin if v == "OWN" else v) for n, v in headers]
        status, text, _ = send(server, "POST", path, headers, body=b'{"title":"x","outcome":"y"}')
        assert status == 403, (path, headers, status, text)
    assert state(server.home) == before
    assert server.calls == []


@pytest.mark.parametrize("path", SIX_ROUTES)
def test_the_six_routes_that_skipped_the_cross_site_check_are_refused_cross_site(server, path):
    before = state(server.home)
    for origin in ("https://evil.example", "null"):
        status, _, _ = send(server, "POST", path, [("Origin", origin)])
        assert status == 403, (path, origin)
    assert state(server.home) == before
    assert server.calls == []


def test_the_read_only_post_routes_pass_the_same_guard(server):
    for path in ("/decision/list", "/goal/list", "/slack-focus/status"):
        assert send(server, "POST", path, [("Origin", "https://evil.example")])[0] == 403
        assert send(server, "POST", path)[0] == 403
        assert send(server, "POST", path, [("X-Assistant-Token", token())])[0] != 403


# Each route the page or the /goal skill calls, with what it answered at origin/main to the page's own request
# (recorded by running these same requests against that server).
CASES = [
    ("toggle", "/toggle/td-1?flag=autoDispatch&value=true", b"", []),
    ("remove", "/remove/td-1", b"", []),
    ("append-detail", "/append-detail/td-1", b"more detail", []),
    ("focus", "/focus/workspace:1", b"", []),
    ("dispatch-now", "/dispatch-now/td-1", b"", []),
    ("proposal-dismiss", "/proposal/prop-1/dismiss", b"", []),
    ("proposal-hold", "/proposal/prop-1/hold", b"", []),
    ("proposal-run-now", "/proposal/prop-2/run-now", b"", []),
    ("decision-list", "/decision/list", b"", []),
    ("decision-act", "/decision/act/{decision}?action=snooze&minutes=5", b"", []),
    ("goal-list", "/goal/list", b"", []),
    ("goal-add", "/goal/add", b'{"title":"a","outcome":"b"}', []),
    ("goal-update", "/goal/update/goal-1", b'{"title":"renamed"}', []),
    ("goal-rerank", "/goal/rerank", b'{"order":["goal-1"]}', []),
    ("goal-pause", "/goal/pause", b"", []),
    ("goal-resume", "/goal/resume", b"", []),
    ("mode", "/mode?value=default", b"", [("X-Assistant-Human", "1")]),
    ("brief-refresh", "/brief/refresh", b"", []),
    ("brief-seen", "/brief/seen?date=2026-01-01", b"", []),
    ("slack-status", "/slack-focus/status", b"", []),
    ("slack-refresh", "/slack-focus/refresh", b"", []),
    ("slack-done", "/slack-focus/done", b'{"fp":"' + b"a" * 64 + b'"}', []),
    ("slack-undone", "/slack-focus/undone", b'{"fp":"' + b"a" * 64 + b'"}', []),
    ("unknown", "/no-such-route", b"", []),
]

RECORDED_AT_MAIN = {
    'toggle': (200, 'td-1.autoDispatch = True'),
    'remove': (200, 'td-1 removed'),
    'append-detail': (200, 'td-1: appended 11 chars'),
    'focus': (200, 'focused workspace:1'),
    'dispatch-now': (200, 'td-1: queued for next-pulse Bucket B dispatch'),
    'proposal-dismiss': (400, "unknown action 'dismiss'"),
    'proposal-hold': (200, 'prop-1.hold ok'),
    'proposal-run-now': (200, 'prop-2.run-now ok'),
    'decision-list': (200, '{"open": [{"schema": "decision/1", "id": "<decision>", "ts": "<time>", "epoch": 1783000000, "created_epoch": 1783000000, "event_ref": "eid-1", "source": "cmux", "kind": "needs_input", "title": "t", "snippet": "s", "refs": {}, "lane": "escalate", "policy_id": "r1", "triage": null, "recommended": null, "goal_refs": [], "score": 150, "urgency": "now", "ttl_h": null, "status": "open", "resolution": null}], "n_open": 1, "n_total": 1, "ts": "<time>"}'),
    'decision-act': (200, '<decision> -> snoozed'),
    'goal-list': (200, '{"goals": [{"id": "goal-1", "rank": 1, "title": "a", "outcome": "o", "status": "active", "horizon": null, "links": {"repos": [], "channels": [], "jql": null, "senders": [], "todos": [], "prs": []}, "stallAfterHours": 48, "lastProgressAt": null, "createdAt": "<time>", "playbook": {"unattended": ["research", "doc-draft", "pr-scaffold", "test-backfill"], "gated": ["code-change", "config-change", "external-comms"]}, "budget": {"maxActiveWs": 2, "maxStagedTodosPerNight": 2, "maxStrategistCallsPerDay": 1}}], "n": 1, "paused": false}'),
    'goal-add': (200, '{"added": "goal-2", "rank": 2}'),
    'goal-update': (200, 'goal-1 updated'),
    'goal-rerank': (200, 'reranked'),
    'goal-pause': (200, '{"paused": true}'),
    'goal-resume': (200, '{"paused": false}'),
    'mode': (200, 'default'),
    'brief-refresh': (200, 'rebuilt'),
    'brief-seen': (404, 'no brief for 2026-01-01'),
    'slack-status': (200, '{}'),
    'slack-refresh': (200, '{"started": false}'),
    'slack-done': (200, 'marked done'),
    'slack-undone': (200, 'marked not done'),
    'unknown': (404, 'not found'),
}


def normal(text, decision):
    """An answer without the parts that differ every run: the decision's id and the times."""
    return re.sub(r"\d{4}-\d\d-\d\dT[0-9:]+Z", "<time>", text.replace(decision, "<decision>"))


def reset(server):
    for stale in server.home.rglob("*"):
        if stale.is_file() and not stale.name.startswith("api-token"):
            stale.unlink()
    (server.home / "proposals").mkdir(exist_ok=True)
    server.decision = seed(server.home)
    server.calls.clear()


@pytest.mark.parametrize("name,path,body,extra", CASES, ids=[c[0] for c in CASES])
def test_a_token_carrier_or_the_served_page_gets_what_each_route_always_answered(server, name, path, body, extra):
    """With the token (no Origin, or Origin: null) and from the page's own Origin with no token, a route
    answers exactly as it did at origin/main, which is what the buttons and the /goal skill rely on."""
    answers = []
    for headers in ([("Origin", server.origin)], [("Origin", "null"), ("X-Assistant-Token", token())],
                    [("X-Assistant-Token", token())]):
        reset(server)
        status, text, _ = send(server, "POST", path.format(decision=server.decision), [*headers, *extra], body=body)
        answers.append((status, normal(text, server.decision)))
    assert len(set(answers)) == 1, answers
    assert answers[0] == RECORDED_AT_MAIN[name], answers[0]


def test_the_requests_that_send_text_or_start_sessions_really_ran_for_the_token_carrier(server):
    assert send(server, "POST", "/proposal/prop-2/run-now", [("X-Assistant-Token", token())])[0] == 200
    assert send(server, "POST", "/focus/workspace:1", [("X-Assistant-Token", token())])[0] == 200
    # "Run now" types through bin/cmux-send.py (which records it), once; it used to call `cmux send-key` itself.
    assert [call[1][1].rsplit("/", 1)[-1] for call in server.calls
            if isinstance(call, tuple) and call[0] == "run" and len(call[1]) > 2].count("cmux-send.py") == 1
    assert any(call[0] == "run" and call[1][-1] == "workspace:1" for call in server.calls if isinstance(call, tuple))


def test_a_dashboard_tab_opened_before_the_update_keeps_working(server):
    """Old page code sends no token, but it is served from this server, so its POSTs carry the exact Origin."""
    status, text, _ = send(server, "POST", "/toggle/td-1?flag=autoDispatch&value=true", [("Origin", server.origin)])
    assert (status, text) == (200, "td-1.autoDispatch = True")
    assert json.loads((server.home / "todo.json").read_text())["items"][0]["autoDispatch"] is True


def test_a_wrong_token_is_refused_and_the_comparison_is_constant_time(server, monkeypatch):
    seen = []
    real = mod.hmac.compare_digest
    monkeypatch.setattr(mod.hmac, "compare_digest", lambda a, b: seen.append((a, b)) or real(a, b))
    status, text, _ = send(server, "POST", "/remove/td-1", [("X-Assistant-Token", "not-the-token")])
    assert status == 403 and text.startswith("Refused. That token isn't the one in ~/.assistant/api-token") and "X-Assistant-Token" in text
    status, text, _ = send(server, "POST", "/remove/td-1", [("Origin", "null"), ("X-Assistant-Token", "not-the-token")])
    assert status == 403 and text.startswith("Refused. Reload the dashboard. That token isn't the one")
    assert seen == [(b"not-the-token", token().encode())] * 2
    status, _, _ = send(server, "POST", "/remove/td-1", [("X-Assistant-Token", "é-not-ascii")])
    assert status == 403
    assert json.loads((server.home / "todo.json").read_text())["items"] == SEED_ITEMS


def test_a_missing_token_gets_a_short_sentence_that_fits_a_button_then_what_to_do(server):
    status, text, _ = send(server, "POST", "/remove/td-1", [("Origin", "null")])
    assert status == 403
    assert text == ("Refused. Reload the dashboard. This request needs the token in ~/.assistant/api-token, "
                    "sent as the X-Assistant-Token header.")
    # A command-line call has nothing to reload.
    assert send(server, "POST", "/remove/td-1")[:2] == (403, (
        "Refused. This request needs the token in ~/.assistant/api-token, sent as the X-Assistant-Token header."))
    assert send(server, "POST", "/remove/td-1", [("Origin", "https://evil.example")])[1] == (
        "Refused. Reload the dashboard. Requests from another website are refused.")


@pytest.mark.parametrize("failure", [OSError("disk"), KeyError("HOME"), RuntimeError("anything")])
def test_a_token_that_cannot_be_read_refuses_cleanly_instead_of_dropping_the_connection(server, monkeypatch, failure):
    def broken():
        raise failure

    monkeypatch.setattr(mod, "_api_token_mod", lambda: types.SimpleNamespace(ensure=broken))
    status, text, _ = send(server, "POST", "/remove/td-1", [("X-Assistant-Token", "anything")])
    assert (status, text) == (403, "Refused. The token in ~/.assistant/api-token can't be read.")
    status, text, _ = send(server, "POST", "/remove/td-1", [("Origin", "null"), ("X-Assistant-Token", "anything")])
    assert (status, text) == (403, "Refused. Reload the dashboard. The token in ~/.assistant/api-token can't be read.")
    assert json.loads((server.home / "todo.json").read_text())["items"] == SEED_ITEMS


# ─── which names and which origins ─────────────────────────────────────────────────────────────────

BAD_HOSTS = ["evil.com:9876", "127.0.0.1.evil.com:9876", "localhost.evil.com:9876", "127.0.0.1:9876@evil.com",
             "LOCALHOST:9876", "Localhost:9876", "localhost.:9876", "127.0.0.1.:9876", "[::1]:9876", "::1",
             "127.0.0.1", "localhost", "localhost:", "localhost:abc", "localhost:123456", "127.0.0.1:98 76",
             "127.0.0.1:98²6", "evil.example", "0.0.0.0:9876", "127.0.0.2:9876",
             "localhost:9876, evil.com"]
WRONG_HOST_TEXT = ("This dashboard only answers at an address that starts with http://127.0.0.1 or "
                   "http://localhost. If you reached it under another name, open it at http://127.0.0.1:9876.")


@pytest.mark.parametrize("method,path", [("GET", "/"), ("GET", "/healthz"), ("GET", "/nope"),
                                         ("POST", "/decision/list"), ("POST", "/remove/td-1"),
                                         ("OPTIONS", "/remove/td-1")])
def test_a_request_for_another_host_name_is_refused_with_a_sentence_that_says_what_to_do(server, method, path):
    (server.home / "dashboard.html").write_text("<html>page</html>")
    good = [("X-Assistant-Token", token())]
    for host in [[h] for h in BAD_HOSTS] + [[], [f"127.0.0.1:{server.port}", "evil.example"],
                                            [f"127.0.0.1:{server.port}", f"127.0.0.1:{server.port}"]]:
        status, text, _ = send(server, method, path, good, host=host)
        assert (status, text) == (403, WRONG_HOST_TEXT), (host, status, text)
    assert json.loads((server.home / "todo.json").read_text())["items"] == SEED_ITEMS
    for host in ("127.0.0.1:9877", "localhost:3000", f"127.0.0.1:{server.port}", "localhost:1", "localhost:65535",
                 "127.0.0.1:00001"):
        assert send(server, method, path, good, host=[host])[0] != 403, host


def test_a_forwarded_port_works_for_the_page_and_for_a_button(server):
    """ssh -L 9877:127.0.0.1:9876: the browser says localhost:9877, so that is the Host and the Origin."""
    (server.home / "dashboard.html").write_text("<html>page</html>")
    for name in ("localhost", "127.0.0.1"):
        host = [f"{name}:9877"]
        assert send(server, "GET", "/", [("Origin", f"http://{name}:9877")], host=host)[:2] == (200, "<html>page</html>")
        assert send(server, "GET", "/", host=host)[0] == 200
        assert send(server, "POST", "/remove/td-1", [("Origin", f"http://{name}:9877")], host=host)[:2] == (
            200, "td-1 removed")
        server.calls.clear()
        (server.home / "todo.json").write_text(json.dumps({"items": SEED_ITEMS}))


def test_a_page_on_another_port_or_scheme_or_name_is_not_the_same_origin(server):
    before = state(server.home)
    p = server.port
    for host, origin in ((f"localhost:{p}", "http://localhost:3000"),
                         (f"localhost:{p}", "http://localhost:8851"),
                         (f"localhost:{p}", f"http://127.0.0.1:{p + 1}"),
                         (f"127.0.0.1:{p}", f"http://localhost:{p + 1}"),
                         (f"127.0.0.1:{p}", f"http://localhost:{p - 1}"),
                         (f"localhost:{p}", f"https://localhost:{p}"),
                         (f"localhost:{p}", f"https://127.0.0.1:{p}"),
                         (f"localhost:{p}", f"http://localhost:{p}/"),
                         (f"localhost:{p}", f"http://LOCALHOST:{p}"),
                         (f"localhost:{p}", f"http://localhost.evil.com:{p}"),
                         (f"localhost:{p}", f"http://127.0.0.1.evil.com:{p}"),
                         (f"localhost:{p}", f"http://[::1]:{p}"),
                         (f"localhost:{p}", f"http://localhost:{p}@evil.com"),
                         (f"localhost:{p}", f"http://127.0.0.1:0{p}"),
                         (f"localhost:{p}", f"http://localhost")):
        assert send(server, "POST", "/remove/td-1", [("Origin", origin)], host=[host])[0] == 403, (host, origin)
        assert send(server, "GET", "/", [("Origin", origin)], host=[host])[0] == 403, (host, origin)
    assert state(server.home) == before and server.calls == []


def test_a_tab_opened_at_one_loopback_name_may_post_to_the_other_on_the_same_port(server):
    """A tab left open at localhost:P has old code that posts to http://127.0.0.1:P/brief/…, and the reverse."""
    (server.home / "dashboard.html").write_text("<html>page</html>")
    p = server.port
    for host, origin in ((f"127.0.0.1:{p}", f"http://localhost:{p}"), (f"localhost:{p}", f"http://127.0.0.1:{p}"),
                         (f"localhost:{p}", f"http://localhost:{p}"), (f"127.0.0.1:{p}", f"http://127.0.0.1:{p}")):
        status, _, response = send(server, "POST", "/brief/refresh", [("Origin", origin)], host=[host])
        assert (status, response.getheader("Access-Control-Allow-Origin")) == (200, origin), (host, origin)
        status, _, response = send(server, "OPTIONS", "/brief/refresh", [("Origin", origin)], host=[host])
        assert (status, response.getheader("Access-Control-Allow-Origin")) == (204, origin), (host, origin)
        assert send(server, "GET", "/", [("Origin", origin)], host=[host])[:2] == (200, "<html>page</html>")
    assert server.calls.count("rerender_dashboard") == 4


PAGE_HEADERS = {"X-Frame-Options": "DENY", "Content-Security-Policy": "frame-ancestors 'none'",
                "Cross-Origin-Resource-Policy": "same-origin", "X-Content-Type-Options": "nosniff"}


def test_the_page_cannot_be_framed_or_read_by_another_website(server):
    (server.home / "dashboard.html").write_text("<html>page</html>")
    for path in ("/", "/index.html"):
        for headers in ([], [("Origin", server.origin)]):
            status, text, response = send(server, "GET", path, headers)
            assert (status, text) == (200, "<html>page</html>")
            for name, value in PAGE_HEADERS.items():
                assert response.getheader(name) == value, (path, name)
        for origin in ("null", "https://evil.example", "", "http://localhost:3000"):
            status, text, _ = send(server, "GET", path, [("Origin", origin)])
            assert status == 403 and "<html>" not in text, (path, origin)
        assert send(server, "GET", path, [("Origin", server.origin), ("Origin", server.origin)])[0] == 403
    assert send(server, "GET", "/healthz")[:2] == (200, "ok")
    assert send(server, "GET", "/healthz", [("Origin", "null")])[:2] == (200, "ok")
    assert send(server, "GET", "/nothing")[0] == 404


def allowed_headers(response):
    return {h.strip().lower() for h in (response.getheader("Access-Control-Allow-Headers") or "").split(",") if h.strip()}


def test_an_answer_names_an_origin_only_for_the_page_itself_or_a_local_file(server):
    (server.home / "dashboard.html").write_text("<html>page</html>")
    for path in ("/", "/healthz"):
        assert send(server, "GET", path)[2].getheader("Access-Control-Allow-Origin") is None
    assert send(server, "POST", "/decision/list", [("X-Assistant-Token", token())])[2].getheader(
        "Access-Control-Allow-Origin") is None
    assert send(server, "POST", "/decision/list", [("Origin", server.origin)])[2].getheader(
        "Access-Control-Allow-Origin") == server.origin
    assert send(server, "POST", "/decision/list", [("Origin", "null"), ("X-Assistant-Token", token())])[2].getheader(
        "Access-Control-Allow-Origin") == "null"
    assert send(server, "POST", "/decision/list", [("Origin", f"http://localhost:{server.port}")])[
        2].getheader("Access-Control-Allow-Origin") == f"http://localhost:{server.port}"
    for origin in ("https://evil.example", "http://localhost:3000", f"http://localhost:{server.port + 1}"):
        assert send(server, "POST", "/decision/list", [("Origin", origin), ("X-Assistant-Token", token())])[
            2].getheader("Access-Control-Allow-Origin") is None, origin


def test_the_preflight_allows_the_token_header_for_the_page_and_for_a_local_file_but_not_for_a_stranger(server):
    for origin in (server.origin, "null"):
        status, _, response = send(server, "OPTIONS", "/remove/td-1", [
            ("Origin", origin), ("Access-Control-Request-Method", "POST"),
            ("Access-Control-Request-Headers", "x-assistant-token,x-assistant-human,content-type")])
        assert status == 204
        assert response.getheader("Access-Control-Allow-Origin") == origin
        assert {"x-assistant-token", "x-assistant-human", "content-type"} <= allowed_headers(response)
    for origin in ("https://evil.example", "http://localhost.evil.com:9876", "http://localhost:3000"):
        status, _, response = send(server, "OPTIONS", "/remove/td-1", [("Origin", origin)])
        assert response.getheader("Access-Control-Allow-Origin") is None
        assert allowed_headers(response) == set()


def test_a_refusal_changes_nothing_even_for_a_request_that_would_start_a_session(server):
    before = state(server.home)
    for path in ("/dispatch-now/td-1", "/proposal/prop-2/run-now", "/focus/workspace:1"):
        assert send(server, "POST", path, [("Origin", "null")])[0] == 403
        assert send(server, "POST", path, [("Origin", "https://evil.example")])[0] == 403
    assert state(server.home) == before and server.calls == []


# ─── the token file ──────────────────────────────────────────────────────────────────────────────


def mode_of(path):
    return stat.S_IMODE(path.stat().st_mode)


def test_the_token_is_created_private_as_32_random_bytes_of_url_safe_text(home):
    assert not apitoken.path().exists()
    first = apitoken.ensure()
    assert apitoken.path() == home / ".assistant" / "api-token"
    assert mode_of(apitoken.path()) == 0o600
    assert len(first) == 43 and set(first) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
    assert not list((home / ".assistant").glob("*.tmp"))


def test_an_existing_token_is_reused_and_a_file_that_is_too_open_is_tightened(home):
    first = apitoken.ensure()
    assert apitoken.ensure() == first
    apitoken.path().write_text("a-token-someone-chose-by-hand-that-is-long-enough\n")
    apitoken.path().chmod(0o644)
    assert apitoken.ensure() == "a-token-someone-chose-by-hand-that-is-long-enough"
    assert mode_of(apitoken.path()) == 0o600


def test_an_empty_short_or_unreadable_token_is_replaced_with_a_private_one(home):
    path = apitoken.path()
    path.parent.mkdir(parents=True)
    path.write_text(" \n")
    path.chmod(0o644)
    fresh = apitoken.ensure()
    assert len(fresh) == 43 and path.read_text().strip() == fresh and mode_of(path) == 0o600
    path.write_text("tok\n")
    short = apitoken.ensure()
    assert short != "tok" and len(short) == 43 and path.read_text().strip() == short
    path.write_text("x" * 31)
    assert apitoken.ensure() != "x" * 31
    path.write_text("x" * 32)
    assert apitoken.ensure() == "x" * 32
    path.write_bytes(b"\xff\xfe\x00 not text")
    again = apitoken.ensure()
    assert again != short and path.read_text().strip() == again
    if os.geteuid() != 0:
        path.chmod(0)
        third = apitoken.ensure()
        assert third != again and path.read_text().strip() == third and mode_of(path) == 0o600


def test_a_token_that_cannot_be_written_raises_and_leaves_no_stray_file(home):
    path = apitoken.path()
    path.mkdir(parents=True)  # a folder where the file belongs: it can be neither read nor replaced
    with pytest.raises(OSError):
        apitoken.ensure()
    assert path.is_dir() and not list(path.parent.glob("*.tmp"))
    path.rmdir()
    if os.geteuid() != 0:
        path.parent.chmod(0o500)
        try:
            with pytest.raises(OSError):
                apitoken.ensure()
        finally:
            path.parent.chmod(0o700)
        assert not list(path.parent.glob("*.tmp"))


def test_the_home_folder_is_found_the_way_the_other_modules_do(monkeypatch, tmp_path):
    monkeypatch.delenv("HOME")
    monkeypatch.setattr(apitoken.Path, "home", classmethod(lambda cls: tmp_path))
    assert apitoken.path() == tmp_path / ".assistant" / "api-token"


def test_two_processes_that_start_together_end_up_with_one_token(home):
    script = ("import sys; sys.path.insert(0, sys.argv[1]); from assistant import apitoken; "
              "import time; time.sleep(max(0, float(sys.argv[2]) - time.time())); print(apitoken.ensure())")
    import time
    start = str(time.time() + 1.5)
    env = {**os.environ, "HOME": str(home)}
    procs = [subprocess.Popen([sys.executable, "-c", script, str(REPO / "src"), start], env=env,
                              stdout=subprocess.PIPE, text=True) for _ in range(8)]
    tokens = {p.communicate(timeout=30)[0].strip() for p in procs}
    assert len(tokens) == 1 and tokens == {apitoken.path().read_text().strip()}


def test_callers_that_race_inside_one_read_still_end_up_with_one_token(home, monkeypatch):
    """Widen the window between "is there a token?" and "write one" so that, without the lock, every caller
    would find none."""
    import time
    real = apitoken._read
    monkeypatch.setattr(apitoken, "_read", lambda file: (lambda found: (time.sleep(0.2), found)[1])(real(file)))
    found = []
    threads = [threading.Thread(target=lambda: found.append(apitoken.ensure())) for _ in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert len(found) == 5 and len(set(found)) == 1 and set(found) == {apitoken.path().read_text().strip()}


# ─── the page ─────────────────────────────────────────────────────────────────────────────────────

_renders = []


def renderer_for(home):
    (home / ".claude/cache").mkdir(parents=True, exist_ok=True)
    (home / ".claude/cache/world.json").write_text(json.dumps(
        {"counts": {}, "live_sessions": [], "workspaces": [], "todo": {}}))
    _renders.append(1)
    return _load(f"renderer_request_guard_{len(_renders)}", "render-assistant-page.py")


def render_page(home):
    renderer_for(home).render()
    return home / ".claude/assistant-dashboard.html"


def token_in(text):
    return text.split('name="assistant-api-token" content="')[1].split('"')[0]


def test_the_page_holds_the_token_and_is_readable_by_its_owner_only(home):
    page = render_page(home)
    first = page.read_text()
    assert f'<meta name="assistant-api-token" content="{apitoken.ensure()}">' in first
    assert mode_of(page) == 0o600
    page.chmod(0o644)
    second = render_page(home)
    assert mode_of(second) == 0o600
    assert token_in(second.read_text()) == token_in(first)
    assert not list(page.parent.glob("*.tmp"))


def test_two_renders_at_once_both_finish_and_leave_one_complete_page(home, monkeypatch):
    renderer = renderer_for(home)
    errors = []
    page = home / ".claude/assistant-dashboard.html"
    # Every render waits with its page written to a temporary file, so the four always overlap, however fast they run.
    all_written, replace = threading.Barrier(4, timeout=30), renderer.os.replace
    monkeypatch.setattr(renderer.os, "replace",
                        lambda source, target: (Path(target) == page and all_written.wait(), replace(source, target)))

    def render():
        try:
            renderer.render()
        except BaseException as error:  # noqa: BLE001
            errors.append(error)

    threads = [threading.Thread(target=render) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)
    assert errors == []
    assert page.read_text().rstrip().endswith("</html>") and page.read_text().count("<!doctype html>") == 1
    assert not list(page.parent.glob("*.tmp")) and mode_of(page) == 0o600


def test_a_render_that_fails_to_write_leaves_no_temporary_file_and_keeps_the_old_page(home, monkeypatch):
    page = render_page(home)
    before = page.read_text()
    renderer = renderer_for(home)
    monkeypatch.setattr(renderer.os, "replace", lambda *a: (_ for _ in ()).throw(OSError("full")))
    with pytest.raises(OSError):
        renderer.render()
    monkeypatch.undo()
    assert page.read_text() == before and not list(page.parent.glob("*.tmp"))


@pytest.mark.parametrize("trouble", ["folder", "readonly", "no-home"])
def test_a_token_file_problem_never_stops_the_page_from_rendering(home, monkeypatch, trouble):
    if trouble == "folder":
        apitoken.path().mkdir(parents=True)
    elif trouble == "readonly":
        if os.geteuid() == 0:
            pytest.skip("root can write anywhere")
        (home / ".assistant").mkdir()
        (home / ".assistant").chmod(0o500)
    else:
        monkeypatch.setattr(apitoken, "ensure", lambda: (_ for _ in ()).throw(KeyError("HOME")))
    try:
        page = render_page(home)
    finally:
        if (home / ".assistant").exists():
            (home / ".assistant").chmod(0o700)
    text = page.read_text()
    assert '<meta name="assistant-api-token" content="">' in text and "assistantPost" in text


def test_every_post_the_page_makes_goes_through_the_one_helper(home):
    text = render_page(home).read_text()
    script = text.split("<script>", 1)[1].split("</script>", 1)[0]
    assert script.count("fetch(") == 2  # the helper itself, and the page refresh (a GET)
    assert "'X-Assistant-Token'" in script
    assert script.count("assistantPost(") >= 11


def test_the_two_calls_a_local_file_page_makes_name_the_server_only_when_it_is_a_local_file(home):
    script = render_page(home).read_text()
    assert "http://127.0.0.1:9876" in script and script.count("http://127.0.0.1:9876") == 2  # and the message
    assert "location.protocol === 'file:' ? 'http://127.0.0.1:9876' : ''" in script
    assert "Couldn't refresh. Reload this page, or open http://127.0.0.1:9876." in script
    assert "refused = !(await assistantPost(serverUrl('/brief/refresh'), {mode: 'cors'})).ok;" in script


def test_the_server_finds_the_token_module_even_when_src_is_not_on_the_path(monkeypatch):
    monkeypatch.setattr(sys, "path", [p for p in sys.path if p != str(mod.SRC_DIR)])
    assert mod._api_token_mod() is apitoken
    assert sys.path[0] == str(mod.SRC_DIR)
