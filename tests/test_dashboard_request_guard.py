"""The one guard on every POST to the dashboard server (bin/todo-server.py).

Every request must name the server by 127.0.0.1:<port> or localhost:<port>. A POST from the page the
server serves (its exact Origin) passes; one with no Origin or Origin: null (the page opened as a local
file, curl, a sandboxed frame on any website) needs the per-install token; any other Origin is refused.
No route is exempt, so the route list is read from the server's own source.

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
from http.server import HTTPServer
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
from assistant import apitoken  # noqa: E402


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, REPO / "bin" / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


mod = _load("todo_server_request_guard", "todo-server.py")

ALLOWED = "http://127.0.0.1:9876"
SEED_ITEMS = [{"id": "td-1", "title": "x", "autoDispatch": False}]
SEED_PROPOSAL = {"id": "prop-1", "status": "pending", "type": "note", "title": "p"}


def route_words() -> list[str]:
    """Every word the server's do_POST compares the request path with, read from its source."""
    tree = ast.parse((REPO / "bin" / "todo-server.py").read_text())
    post = next(node for cls in ast.walk(tree) if isinstance(cls, ast.ClassDef) and cls.name == "Handler"
                for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == "do_POST")
    words = set()
    for compare in (n for n in ast.walk(post) if isinstance(n, ast.Compare)):
        if "parts" not in ast.unparse(compare.left):
            continue
        for constant in (n for part in compare.comparators for n in ast.walk(part) if isinstance(n, ast.Constant)):
            if isinstance(constant.value, str) and re.fullmatch(r"[a-z][a-z-]*", constant.value):
                words.add(constant.value)
    return sorted(words)


def every_post_path() -> list[str]:
    words = route_words()
    paths = {"/", "/no-such-route", "/no/such/route/at/all"}
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
    for name in ("run", "Popen", "call", "check_call", "check_output"):
        monkeypatch.setattr(mod.subprocess, name, lambda *a, _n=name, **k: made.append(("subprocess", _n, a)) or (
            _ for _ in ()).throw(AssertionError("no process may start")))
    return made


def seed(home):
    (home / "todo.json").write_text(json.dumps({"items": SEED_ITEMS}))
    (home / "proposals" / "prop-1.json").write_text(json.dumps(SEED_PROPOSAL))


@pytest.fixture
def server(home, calls):
    seed(home)
    httpd = HTTPServer(("127.0.0.1", 0), mod.Handler)
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    yield type("Server", (), {"port": httpd.server_address[1], "home": home, "calls": calls})()
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=5)


def send(server, method, path, headers=(), host="default", body=b""):
    """One raw request. `headers` is a list of (name, value) pairs, so a header can be sent twice."""
    connection = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
    connection.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
    hosts = [f"127.0.0.1:{server.port}"] if host == "default" else host
    for value in hosts:
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
           "http://127.0.0.1", "https://127.0.0.1:9876", ALLOWED + "/", ""]

# (path, what it would change if it ran)
SIX_ROUTES = ["/toggle/td-1?flag=autoDispatch&value=true", "/remove/td-1", "/append-detail/td-1",
              "/focus/workspace:1", "/dispatch-now/td-1", "/proposal/prop-1/dismiss"]


def test_the_route_list_is_read_from_the_source_and_covers_the_known_routes():
    words = route_words()
    for expected in ("toggle", "remove", "focus", "dispatch-now", "proposal", "decision", "goal", "slack-focus",
                     "mode", "brief", "append-detail", "list", "status"):
        assert expected in words
    assert len(every_post_path()) > 100


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
    [("Origin", "null"), ("Origin", ALLOWED), ("X-Assistant-Token", "RIGHT")],
    [("Origin", ALLOWED), ("Origin", ALLOWED)],
]


@pytest.mark.parametrize("path", every_post_path())
def test_no_post_route_answers_a_foreign_origin_or_a_nameless_caller(server, path):
    before = state(server.home)
    holes = HOLES + (ODD_HOLES if path.count("/") < 2 else [])
    for headers in holes:
        headers = [(n, token() if v == "RIGHT" else v) for n, v in headers]
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


CASES = [
    ("/toggle/td-1?flag=autoDispatch&value=true", b"", []),
    ("/remove/td-1", b"", []),
    ("/append-detail/td-1", b"more detail", []),
    ("/focus/workspace:1", b"", []),
    ("/dispatch-now/td-1", b"", []),
    ("/proposal/prop-1/dismiss", b"", []),
    ("/proposal/prop-1/nonsense", b"", []),
    ("/decision/list", b"", []),
    ("/decision/act/dec-0123456789abcdef?action=accept", b"", []),
    ("/goal/list", b"", []),
    ("/goal/add", b'{"title":"a","outcome":"b"}', []),
    ("/goal/pause", b"", []),
    ("/goal/resume", b"", []),
    ("/mode?value=default", b"", [("X-Assistant-Human", "1")]),
    ("/brief/refresh", b"", []),
    ("/brief/seen?date=2026-01-01", b"", []),
    ("/slack-focus/status", b"", []),
    ("/slack-focus/done", b'{"fp":"' + b"a" * 64 + b'"}', []),
    ("/no-such-route", b"", []),
]


def reset(server):
    for stale in server.home.rglob("*"):
        if stale.is_file() and not stale.name.startswith("api-token"):
            stale.unlink()
    (server.home / "proposals").mkdir(exist_ok=True)
    seed(server.home)
    server.calls.clear()


@pytest.mark.parametrize("path,body,extra", CASES)
def test_a_token_carrier_or_the_served_page_gets_what_each_route_always_answered(server, path, body, extra):
    """With the token (no Origin, or Origin: null) and from the page's own Origin with no token, a route
    answers exactly as it does from the page, which is what the buttons rely on."""
    answers = []
    for headers in ([("Origin", ALLOWED)], [("Origin", "http://localhost:9876")],
                    [("Origin", "null"), ("X-Assistant-Token", token())], [("X-Assistant-Token", token())]):
        reset(server)
        status, text, _ = send(server, "POST", path, [*headers, *extra], body=body)
        answers.append((status, text))
    assert len(set(answers)) == 1, answers
    assert answers[0][0] != 403, answers[0]


def test_a_dashboard_tab_opened_before_the_update_keeps_working(server):
    """Old page code sends no token, but it is served from this server, so its POSTs carry the exact Origin."""
    status, text, _ = send(server, "POST", "/toggle/td-1?flag=autoDispatch&value=true",
                           [("Origin", ALLOWED)])
    assert (status, text) == (200, "td-1.autoDispatch = True")
    assert json.loads((server.home / "todo.json").read_text())["items"][0]["autoDispatch"] is True


def test_a_wrong_token_is_refused_and_the_comparison_is_constant_time(server, monkeypatch):
    seen = []
    real = mod.hmac.compare_digest
    monkeypatch.setattr(mod.hmac, "compare_digest", lambda a, b: seen.append((a, b)) or real(a, b))
    status, text, _ = send(server, "POST", "/remove/td-1", [("X-Assistant-Token", "not-the-token")])
    assert status == 403 and "X-Assistant-Token" in text
    assert seen == [(b"not-the-token", token().encode())]
    status, _, _ = send(server, "POST", "/remove/td-1", [("X-Assistant-Token", "é-not-ascii")])
    assert status == 403
    assert json.loads((server.home / "todo.json").read_text())["items"] == SEED_ITEMS


def test_a_missing_token_gets_the_plain_sentence_that_says_what_to_do(server):
    status, text, _ = send(server, "POST", "/remove/td-1", [("Origin", "null")])
    assert status == 403
    assert text == ("This request needs the token in ~/.assistant/api-token. "
                    "Reload the dashboard, or send it as the X-Assistant-Token header.")


def test_a_token_that_cannot_be_read_refuses_instead_of_guessing(server, monkeypatch):
    def broken():
        raise OSError("disk")

    monkeypatch.setattr(mod, "_api_token_mod", lambda: type("T", (), {"ensure": staticmethod(broken)}))
    status, text, _ = send(server, "POST", "/remove/td-1", [("X-Assistant-Token", "anything")])
    assert status == 403 and "can't be read" in text
    assert json.loads((server.home / "todo.json").read_text())["items"] == SEED_ITEMS


@pytest.mark.parametrize("method,path", [("GET", "/"), ("GET", "/healthz"), ("GET", "/nope"),
                                         ("POST", "/decision/list"), ("POST", "/remove/td-1"),
                                         ("OPTIONS", "/remove/td-1")])
def test_a_request_for_another_host_name_is_refused(server, method, path):
    (server.home / "dashboard.html").write_text("<html>page</html>")
    good = [("X-Assistant-Token", token())]
    for host in (["evil.example"], ["evil.example:9876"], ["127.0.0.1"], ["127.0.0.1:1"], ["localhost"],
                 ["localhost.evil.com:%d" % server.port], [], [f"127.0.0.1:{server.port}", "evil.example"],
                 [f"127.0.0.1:{server.port}", f"127.0.0.1:{server.port}"]):
        status, text, _ = send(server, method, path, good, host=host)
        assert status == 403 and "127.0.0.1 or localhost" in text, (host, status)
    assert json.loads((server.home / "todo.json").read_text())["items"] == SEED_ITEMS
    for name in ("127.0.0.1", "localhost"):
        assert send(server, method, path, good, host=[f"{name}:{server.port}"])[0] != 403


def test_the_page_and_the_health_check_need_no_token_but_not_a_foreign_origin(server):
    (server.home / "dashboard.html").write_text("<html>page</html>")
    for path in ("/", "/index.html"):
        assert send(server, "GET", path)[:2] == (200, "<html>page</html>")
        assert send(server, "GET", path, [("Origin", ALLOWED)])[0] == 200
        for origin in ("null", "https://evil.example", ""):
            status, text, _ = send(server, "GET", path, [("Origin", origin)])
            assert status == 403 and "<html>" not in text, (path, origin)
        assert send(server, "GET", path, [("Origin", ALLOWED), ("Origin", ALLOWED)])[0] == 403
    assert send(server, "GET", "/healthz") [:2] == (200, "ok")
    assert send(server, "GET", "/healthz", [("Origin", "null")])[:2] == (200, "ok")
    assert send(server, "GET", "/nothing")[0] == 404


def allowed_headers(response):
    return {h.strip().lower() for h in (response.getheader("Access-Control-Allow-Headers") or "").split(",") if h.strip()}


def test_the_preflight_allows_the_token_header_for_the_page_and_for_a_local_file_but_not_for_a_stranger(server):
    for origin in (ALLOWED, "http://localhost:9876", "null"):
        status, _, response = send(server, "OPTIONS", "/remove/td-1", [
            ("Origin", origin), ("Access-Control-Request-Method", "POST"),
            ("Access-Control-Request-Headers", "x-assistant-token,x-assistant-human,content-type")])
        assert status == 204
        assert response.getheader("Access-Control-Allow-Origin") == origin
        assert {"x-assistant-token", "x-assistant-human", "content-type"} <= allowed_headers(response)
    for origin in ("https://evil.example", "http://localhost.evil.com:9876"):
        status, _, response = send(server, "OPTIONS", "/remove/td-1", [("Origin", origin)])
        assert response.getheader("Access-Control-Allow-Origin") is None
        assert allowed_headers(response) == set()


def test_a_refusal_changes_nothing_even_for_a_request_that_would_start_a_session(server):
    before = state(server.home)
    for path in ("/dispatch-now/td-1", "/proposal/prop-1/run-now", "/focus/workspace:1"):
        assert send(server, "POST", path, [("Origin", "null")])[0] == 403
        assert send(server, "POST", path, [("Origin", "https://evil.example")])[0] == 403
    assert state(server.home) == before and server.calls == []


# ─── the token file ──────────────────────────────────────────────────────────


def mode_of(path):
    return stat.S_IMODE(path.stat().st_mode)


def test_the_token_is_created_private_as_32_random_bytes_of_url_safe_text(home):
    assert not apitoken.path().exists()
    first = apitoken.ensure()
    assert apitoken.path() == home / ".assistant" / "api-token"
    assert mode_of(apitoken.path()) == 0o600
    assert len(first) == 43 and set(first) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
    assert not list((home / ".assistant").glob("*.tmp"))


def test_an_existing_token_is_reused(home):
    first = apitoken.ensure()
    assert apitoken.ensure() == first
    apitoken.path().write_text("a-token-someone-chose\n")
    assert apitoken.ensure() == "a-token-someone-chose"


def test_an_empty_or_unreadable_token_is_replaced_with_a_private_one(home):
    path = apitoken.path()
    path.parent.mkdir(parents=True)
    path.write_text(" \n")
    path.chmod(0o644)
    fresh = apitoken.ensure()
    assert fresh and path.read_text().strip() == fresh and mode_of(path) == 0o600
    path.write_bytes(b"\xff\xfe\x00 not text")
    again = apitoken.ensure()
    assert again != fresh and path.read_text().strip() == again
    if os.geteuid() != 0:
        path.chmod(0)
        third = apitoken.ensure()
        assert third != again and path.read_text().strip() == third and mode_of(path) == 0o600


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


# ─── the page ────────────────────────────────────────────────────────────────


def render_page(home):
    (home / ".claude/cache").mkdir(parents=True, exist_ok=True)
    (home / ".claude/cache/world.json").write_text(json.dumps(
        {"counts": {}, "live_sessions": [], "workspaces": [], "todo": {}}))
    renderer = _load(f"renderer_request_guard_{len(list(home.iterdir()))}_{id(home)}", "render-assistant-page.py")
    renderer.render()
    return home / ".claude/assistant-dashboard.html"


def test_the_page_holds_the_token_and_is_readable_by_its_owner_only(home):
    page = render_page(home)
    assert f'<meta name="assistant-api-token" content="{apitoken.ensure()}">' in page.read_text()
    assert mode_of(page) == 0o600
    first = page.read_text()
    page.chmod(0o644)
    (page.with_name(page.name + ".tmp")).write_text("left by a crash")
    (page.with_name(page.name + ".tmp")).chmod(0o644)
    second = render_page(home)
    assert mode_of(second) == 0o600 and not page.with_name(page.name + ".tmp").exists()
    token_of = lambda text: text.split('name="assistant-api-token" content="')[1].split('"')[0]  # noqa: E731
    assert token_of(second.read_text()) == token_of(first)


def test_every_post_the_page_makes_goes_through_the_one_helper(home):
    text = render_page(home).read_text()
    script = text.split("<script>", 1)[1].split("</script>", 1)[0]
    assert script.count("fetch(") == 2  # the helper itself, and the page refresh (a GET)
    assert "'X-Assistant-Token'" in script
    assert script.count("assistantPost(") >= 11


def test_the_server_finds_the_token_module_even_when_src_is_not_on_the_path(monkeypatch):
    monkeypatch.setattr(sys, "path", [p for p in sys.path if p != str(mod.SRC_DIR)])
    assert mod._api_token_mod() is apitoken
    assert sys.path[0] == str(mod.SRC_DIR)
