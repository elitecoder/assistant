"""The request guard in a real browser, against the real dashboard server.

Chromium opens the page the renderer wrote, served over HTTP by the server and opened as a local file, and
clicks its buttons. Then a page on another website, and a sandboxed frame (Origin: null), try the same writes
and change nothing. The server runs on an ephemeral port, never 9876, so its allowed origins are patched to
that port and the local-file copy of the page has its port 9876 swapped for the same one.

Skipped when Playwright or its Chromium isn't installed.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import threading
from http.server import HTTPServer
from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import Error, sync_playwright  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
from assistant import apitoken, decisions  # noqa: E402


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, REPO / "bin" / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


server_mod = load("todo_server_guard_browser", "todo-server.py")


@pytest.fixture
def dashboard(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    claude = tmp_path / ".claude"
    (claude / "cache").mkdir(parents=True)
    (claude / "cache/world.json").write_text(json.dumps(
        {"counts": {}, "live_sessions": [], "workspaces": [], "todo": {}}))
    todo = claude / "assistant-todo.json"
    todo.write_text(json.dumps({"items": [
        {"id": "td-1", "title": "First", "priority": "P1", "status": "open", "autoDispatch": False,
         "createdAt": "2026-11-01"},
        {"id": "td-2", "title": "Second", "priority": "P1", "status": "open", "autoDispatch": False,
         "createdAt": "2026-11-01"}]}))
    record, _ = decisions.open_decision(
        event={"schema": "world-event/1", "id": "eid-1", "source": "cmux", "kind": "needs_input",
               "external_id": "cmux:workspace:1:needs_input:1:aa", "title": "t", "snippet": "s",
               "refs": {}, "raw_path": None},
        lane="escalate", policy_id="r1", urgency="now", now=1783000000.0)
    log = {"focus": [], "rebuilt": [], "mode": [], "slack": [], "answers": []}
    monkeypatch.setattr(server_mod.Handler, "log_message", lambda self, fmt, *args: log["answers"].append((fmt % args)))

    def fake_run(argv, **kwargs):
        log["focus"].append(argv)
        return type("Done", (), {"returncode": 0, "stderr": "", "stdout": ""})()

    monkeypatch.setattr(server_mod, "JSON_PATH", todo)
    monkeypatch.setattr(server_mod, "PROPOSALS_DIR", tmp_path / "proposals")
    monkeypatch.setattr(server_mod, "rerender", lambda: log["rebuilt"].append("todo"))
    monkeypatch.setattr(server_mod, "rerender_dashboard", lambda: log["rebuilt"].append("page"))
    monkeypatch.setattr(server_mod, "set_mode", lambda value: log["mode"].append(value) or (True, value))
    monkeypatch.setattr(server_mod, "slack_focus_refresh", lambda: log["slack"].append("refresh") or '{"started": false}')
    monkeypatch.setattr(server_mod, "slack_focus_status", lambda: "{}")
    monkeypatch.setattr(server_mod.subprocess, "run", fake_run)
    httpd = HTTPServer(("127.0.0.1", 0), server_mod.Handler)
    port = httpd.server_address[1]
    renderer = load("renderer_guard_browser", "render-assistant-page.py")
    renderer.render()
    page = claude / "assistant-dashboard.html"
    # The renderer's absolute URLs name the real port; the file copy is pointed at the test server's.
    local = tmp_path / "local-copy.html"
    local.write_text(page.read_text().replace("127.0.0.1:9876", f"127.0.0.1:{port}"))
    monkeypatch.setattr(server_mod, "DASHBOARD_HTML_PATH", local)  # the server serves the same copy
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    yield type("Dashboard", (), {"decision": record["id"], "port": port, "todo": todo, "local": local, "log": log, "home": tmp_path})()
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=5)


# Recent Chrome refuses a public page's request to 127.0.0.1 before it is sent. Safari and Firefox don't,
# and older Chrome didn't, so the attack below is tried with that protection off: the server must hold alone.
NO_NETWORK_PREFLIGHT = ("--disable-features=PrivateNetworkAccessSendPreflights,PrivateNetworkAccessRespectPreflightResults,"
                        "BlockInsecurePrivateNetworkRequests,LocalNetworkAccessChecks,LocalNetworkAccessChecksWebSockets")


def browser_executable():
    """$ASSISTANT_TEST_BROWSER, else Playwright's own Chromium, else Chrome or an older Chromium that is installed."""
    candidates = [os.environ.get("ASSISTANT_TEST_BROWSER", "")]
    cache = Path.home() / "Library/Caches/ms-playwright"
    candidates += ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                   *sorted(map(str, cache.glob("chromium-*/chrome-mac*/*.app/Contents/MacOS/*")), reverse=True)]
    return next((c for c in candidates if c and Path(c).is_file()), None)


@pytest.fixture
def browser():
    with sync_playwright() as playwright:
        try:
            chromium = playwright.chromium.launch(
                headless=True, executable_path=browser_executable(), args=[NO_NETWORK_PREFLIGHT])
        except Error as error:  # pragma: no cover - only without an installed browser
            pytest.skip(f"no Chromium: {error}")
        yield chromium
        chromium.close()


def items(dashboard):
    return [item["id"] for item in json.loads(dashboard.todo.read_text())["items"]]


def never_the_real_port(context):
    """Abort anything aimed at port 9876, where a real dashboard server may be running."""
    context.route("**://127.0.0.1:9876/**", lambda route: route.abort())
    context.route("**://localhost:9876/**", lambda route: route.abort())


def watch(page):
    """Every request the page sends to the server, with its token header and the answer."""
    seen = []
    page.on("response", lambda r: seen.append((r.request.method, r.url.split("/", 3)[3], r.status,
                                               r.request.headers.get("x-assistant-token"))) if r.request.method == "POST" else None)
    return seen


def test_every_button_works_when_the_page_is_served_over_http(browser, dashboard):
    page = browser.new_page()
    never_the_real_port(page.context)
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("dialog", lambda d: d.accept())
    seen = watch(page)
    page.goto(f"http://127.0.0.1:{dashboard.port}/")
    page.locator('[data-tab="todos"]').click()
    # The Brief tab's refresh button and the view signal use the absolute address; the rest are relative.
    page.locator('[data-tab="brief"]').click()
    page.locator("#brief-refresh").click()
    page.wait_for_function("document.getElementById('brief-refresh').textContent === 'Refresh now'")
    page.locator('[data-tab="todos"]').click()
    page.locator('.td-set-true[data-id="td-1"]').click()
    page.wait_for_function("document.querySelector('.td-set-true[data-id=\"td-1\"]').classList.contains('active')")
    page.locator('.td-dispatch[data-id="td-1"]').click()
    page.locator('.td-context-toggle[data-id="td-1"]').click()
    page.locator('.td-context-text[data-id="td-1"]').fill("a note")
    page.locator('.td-context-save[data-id="td-1"]').click()
    page.wait_for_function("document.querySelector('.td-context-save[data-id=\"td-1\"]').textContent.includes('appended')")
    page.locator('.td-remove[data-id="td-2"]').click()
    page.wait_for_function("document.querySelector('.td-remove[data-id=\"td-2\"]').textContent === '✓ removed'")
    # Buttons the fixture has no data for are added to the page, and the page's own handlers run them.
    page.evaluate(f"""document.body.insertAdjacentHTML('beforeend',
      '<button id="t-focus" data-ws="workspace:1" onclick="openWs(this)">f</button>'
      + '<button id="t-dec" class="dec-act" data-dec="{dashboard.decision}" data-action="snooze" data-minutes="5">d</button>')""")
    page.locator("#t-focus").click()
    page.wait_for_function("document.getElementById('t-focus').textContent.startsWith('✓')")
    page.locator("#t-dec").click()
    page.wait_for_function("document.getElementById('t-dec').textContent.startsWith('✓')")
    page.evaluate("setMode('pro')")
    page.wait_for_function("document.getElementById('mode-status').textContent !== 'Switching…'")
    page.locator('[data-tab="slack"]').click()
    page.locator("#sf-refresh").click()
    page.wait_for_function("document.getElementById('sf-refresh').textContent === 'Refresh from Slack'")
    page.close()
    assert not errors, errors
    assert items(dashboard) == ["td-1"]
    detail = json.loads(dashboard.todo.read_text())["items"][0]
    assert detail["autoDispatch"] is True and "a note" in json.dumps(detail)
    assert dashboard.log["mode"] == ["pro"] and dashboard.log["slack"] == ["refresh"]
    assert dashboard.log["focus"] and dashboard.log["focus"][0][-1] == "workspace:1"
    token = apitoken.ensure()
    assert seen and all(status < 400 for _, _, status, _ in seen), seen
    assert all(sent == token for _, _, _, sent in seen), seen
    paths = {path.split("?")[0].split("/")[0] for _, path, _, _ in seen}
    assert {"toggle", "dispatch-now", "append-detail", "remove", "focus", "decision", "mode",
            "brief", "slack-focus"} <= paths, paths


def test_the_two_buttons_that_work_from_a_local_file_still_do(browser, dashboard):
    """Only the Brief tab's refresh and view signal use the absolute address; they now send the token with
    Origin: null, after the browser's preflight allows that header."""
    page = browser.new_page()
    never_the_real_port(page.context)
    seen = watch(page)
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(dashboard.local.as_uri() + "#brief")
    page.locator('[data-tab="brief"]').click()
    page.locator("#brief-refresh").click()
    page.wait_for_function("document.getElementById('brief-refresh').textContent === 'Refresh now'")
    page.close()
    assert not errors, errors
    assert ("POST", "brief/refresh", 200, apitoken.ensure()) in seen, seen
    assert dashboard.log["rebuilt"] == ["page"]


def test_a_stale_token_in_a_local_file_page_says_so_instead_of_doing_nothing(browser, dashboard):
    stale = dashboard.local.with_name("stale-copy.html")
    stale.write_text(dashboard.local.read_text().replace(apitoken.ensure(), "a-token-from-before"))
    page = browser.new_page()
    never_the_real_port(page.context)
    seen = watch(page)
    page.goto(stale.as_uri() + "#brief")
    page.locator('[data-tab="brief"]').click()
    page.locator("#brief-refresh").click()
    page.wait_for_function("document.getElementById('brief-refresh').textContent === 'Refresh now'")
    assert page.locator("#refresh-error").inner_text() == f"Couldn't refresh. Reload this page, or open http://127.0.0.1:{dashboard.port}."
    page.close()
    assert [(method, path, status) for method, path, status, _ in seen] == [("POST", "brief/refresh", 403)]
    assert dashboard.log["rebuilt"] == []


FRAME_ATTACK = """<!doctype html><body>
<iframe id="plain" src="{url}"></iframe>
<iframe id="boxed" sandbox="allow-scripts allow-same-origin allow-forms" src="{url}"></iframe>
<iframe id="boxed-null" sandbox="allow-scripts allow-forms" src="{url}"></iframe></body>"""


def test_another_website_cannot_show_the_dashboard_in_a_frame_or_act_through_one(browser, dashboard):
    """A page on another site frames the dashboard (a navigation sends no Origin, so it is served) and the
    visitor's click lands on its buttons. Plain, sandboxed, and sandboxed with a null origin."""
    context = browser.new_context()
    never_the_real_port(context)
    page = context.new_page()
    url = f"http://127.0.0.1:{dashboard.port}/#todos"
    page.route("http://evil.test/**", lambda route: route.fulfill(
        status=200, content_type="text/html", body=FRAME_ATTACK.format(url=url)))
    page.goto("http://evil.test/")
    for _ in range(50):
        if len([f for f in page.frames if f != page.main_frame]) == 3 and all(
                f.url for f in page.frames if f != page.main_frame):
            break
        page.wait_for_timeout(100)
    page.wait_for_timeout(800)  # a framed dashboard would have drawn by now
    frames = [f for f in page.frames if f != page.main_frame]
    assert len(frames) == 3
    for frame in frames:
        assert frame.locator('.td-set-true[data-id="td-1"]').count() == 0, frame.url
        assert frame.locator("#dashboard-content").count() == 0, frame.url
    posts = [line for line in dashboard.log["answers"] if line.startswith('"POST')]
    assert posts == [] and items(dashboard) == ["td-1", "td-2"]
    assert len([line for line in dashboard.log["answers"] if line.startswith('"GET /')]) >= 3
    context.close()


def test_a_tab_opened_before_the_update_sends_no_token_and_still_works(browser, dashboard):
    page = browser.new_page()
    never_the_real_port(page.context)
    page.on("dialog", lambda d: d.accept())
    page.route("**/*", lambda route: route.continue_(headers={
        k: v for k, v in route.request.headers.items() if k != "x-assistant-token"}))
    seen = watch(page)
    page.goto(f"http://127.0.0.1:{dashboard.port}/")
    page.locator('[data-tab="todos"]').click()
    page.locator('.td-remove[data-id="td-2"]').click()
    page.wait_for_function("document.querySelector('.td-remove[data-id=\"td-2\"]').textContent === '✓ removed'")
    page.close()
    assert ("POST", "remove/td-2", 200, None) in seen, seen
    assert items(dashboard) == ["td-1"]


def test_a_tab_left_open_at_localhost_still_reaches_the_address_its_old_code_names(browser, dashboard):
    """Before this update the page sent its two Brief calls to http://127.0.0.1:P/…, whatever name it was opened
    at. From a tab at localhost:P that is another name for the same port, with no token."""
    page = browser.new_page()
    never_the_real_port(page.context)
    page.goto(f"http://localhost:{dashboard.port}/")
    answers = page.evaluate(f"""async () => {{
      const out = [];
      for (const path of ['/brief/refresh', '/brief/seen?date=2026-01-01']) {{
        const r = await fetch('http://127.0.0.1:{dashboard.port}' + path, {{method: 'POST', mode: 'cors'}});
        out.push([r.status, (await r.text())]);
      }}
      return out;
    }}""")
    page.close()
    assert answers == [[200, "rebuilt"], [404, "no brief for 2026-01-01"]], answers
    assert dashboard.log["rebuilt"] == ["page"]
    # and a localhost page on another port is still a stranger
    other = browser.new_page()
    never_the_real_port(other.context)
    other.route("http://localhost:3000/**", lambda route: route.fulfill(status=200, content_type="text/html", body="<p>x</p>"))
    other.goto("http://localhost:3000/")
    refused = other.evaluate(f"""fetch('http://127.0.0.1:{dashboard.port}/brief/refresh', {{method: 'POST', mode: 'no-cors'}}).then(() => 'sent')""")
    other.close()
    assert refused == "sent" and dashboard.log["rebuilt"] == ["page"]


def test_another_website_and_a_sandboxed_frame_change_nothing(browser, dashboard):
    context = browser.new_context()
    never_the_real_port(context)
    page = context.new_page()
    page.route("http://evil.test/**", lambda route: route.fulfill(status=200, content_type="text/html", body="<p>x</p>"))
    page.goto("http://evil.test/")
    target = f"http://127.0.0.1:{dashboard.port}"
    attack = f"""
      for (const path of ['/remove/td-1', '/dispatch-now/td-1', '/focus/workspace:1', '/mode?value=pro',
                          '/slack-focus/refresh', '/brief/refresh']) {{
        fetch('{target}' + path, {{method: 'POST', mode: 'no-cors', headers: {{'X-Assistant-Human': '1'}}}}).catch(() => {{}});
        fetch('{target}' + path, {{method: 'POST', mode: 'no-cors'}}).catch(() => {{}});
      }}
      const frame = document.createElement('iframe');
      frame.sandbox = 'allow-scripts';
      frame.srcdoc = '<script>'
        + 'for (const path of ["/remove/td-1", "/dispatch-now/td-1", "/focus/workspace:1"])'
        + '  fetch("{target}" + path, {{method: "POST", mode: "no-cors"}}).catch(() => {{}});'
        // A sandboxed frame reading the page (which holds the token) must get nothing.
        + 'fetch("{target}/").then(r => r.text().then(t => parent.postMessage("GET:" + r.status + ":" + t.includes("assistant-api-token"), "*")))'
        + '  .catch(() => parent.postMessage("GET:blocked", "*"));'
        + '<\\/script>';
      document.body.appendChild(frame);
    """
    messages = []
    page.expose_function("record", lambda text: messages.append(text))
    page.evaluate("window.addEventListener('message', e => record(String(e.data)))")
    page.evaluate(attack)
    # At least the six plain POSTs from the page are sent (the frame adds up to three more) (the ones with a custom header stop at
    # the preflight, which the server answers without allowing them). Wait for them under load.
    for _ in range(100):
        posts = [line for line in dashboard.log["answers"] if line.startswith('"POST')]
        if len(posts) >= 6 and messages:
            break
        page.wait_for_timeout(100)
    assert len(posts) >= 6 and all(line.endswith(" 403 -") for line in posts), dashboard.log["answers"]
    assert items(dashboard) == ["td-1", "td-2"]
    assert dashboard.log["focus"] == [] and dashboard.log["mode"] == [] and dashboard.log["slack"] == []
    assert dashboard.log["rebuilt"] == []
    assert messages and all(m in ("GET:403:false", "GET:blocked") for m in messages), messages
