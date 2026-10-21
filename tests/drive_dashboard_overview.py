"""Drive the rendered overview in a real browser without touching live sessions."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import threading
from datetime import datetime, timedelta, timezone
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from playwright.sync_api import sync_playwright


REPO = Path(__file__).resolve().parents[1]


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *_args):
        pass


def drive(output_dir, browser_executable, coverage_dir=None):
    output_dir.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".assistant-browser-", dir=REPO) as temporary:
        home = Path(temporary)
        output = home / ".claude"
        (output / "cache").mkdir(parents=True)
        (home / ".assistant/observer-summaries").mkdir(parents=True)
        now = datetime.now(timezone.utc).replace(microsecond=0)
        world = {
            "_meta": {"built_at": now.isoformat()},
            "workspaces": [], "live_sessions": [], "counts": {}, "todo": {},
        }
        verdicts = ["needs_user"] * 7 + ["active", "ready_for_cleanup", "active"]
        for index, verdict in enumerate(verdicts, 1):
            ref = f"workspace:{index}"
            title = f"Task {index}: finish the pending change"
            identity = {"surface_id": f"surface-id-{index}", "provider": "claude",
                        "session_id": f"session-{index}"}
            world["workspaces"].append({
                "ws_ref": ref, "title": title, "workspace_id": f"workspace-id-{index}",
                "surfaces": [{"surface_id": identity["surface_id"]}],
                "session_ids": [identity["session_id"]],
            })
            world["live_sessions"].append({
                **identity, "ws_ref": ref, "workspace_id": f"workspace-id-{index}",
                "identity_status": "verified", "context_status": "verified",
                "pending_tool_use": False,
                "context_built_at": now.isoformat()})
            (home / f".assistant/observer-summaries/workspace_{index}.json").write_text(
                json.dumps({
                    "ws_ref": ref, "title": title, "verdict": verdict,
                    "workspace_id": f"workspace-id-{index}", "observed_sessions": [identity],
                    "observation_complete": True,
                    "observed_at": now.timestamp(),
                    "ts": now.timestamp(), "cwd": "/work/example",
                    "summary": "A detailed return note. " * 25,
                    "next": "Review the recorded result before choosing the next step.",
                }))
        (home / ".assistant/back-off.json").write_text(json.dumps({
            "workspaces": [{"ws_ref": "workspace:10", "workspace_id": "workspace-id-10",
                            "reason": "Waiting for a dependency."}]}))
        world["live_sessions"][0].update({
            "pending_tool_use": True,
            "guidance_context": {
                "source_version": "question-v1",
                "initial_request": {"text": "Repair the playback behavior."},
                "last_request": {"text": "Verify the failing interaction."},
                "last_response": {"text": "One behavior choice remains."},
                "pending_questions": [{
                    "tool_use_id": "question-1",
                    "question": "Investigate the undo failure before closing this task?",
                    "header": "Undo failure",
                    "options": [{"label": "Investigate", "description": "Keep the failure as a blocker."},
                                {"label": "Park", "description": "Save the evidence and revisit later."}],
                    "multiSelect": False,
                }],
            },
        })
        world["live_sessions"][7]["guidance_context"] = {
            "source_version": "continuation-v1",
            "last_response": {"text": "The coder's patch is available."},
            "pending_questions": [],
        }
        (home / ".assistant/session-return-notes.json").write_text(json.dumps({"sessions": [{
            "workspace_id": "workspace-id-8", "surface_id": "surface-id-8",
            "provider": "claude", "session_id": "session-8", "source_version": "continuation-v1",
            "goal": "Complete the retry fix", "progress": "The coder's patch is available.",
            "next_action": "Run the retry regression against the patched code.",
            "who": "agent", "recommendation": "continue",
        }]}))
        (home / ".assistant/heartbeat.json").write_text(json.dumps({
            "last_pulse_ts": now.timestamp(), "pulse_idx": 1, "model": "fixture"}))
        (output / "assistant-todo.json").write_text(json.dumps({"items": [
            {"id": "td-901", "title": "Finish the older retry fix", "priority": "P1",
             "createdAt": (now - timedelta(days=5)).date().isoformat(), "status": "blocked"},
            {"id": "td-902", "title": "A newer idea", "priority": "P2",
             "createdAt": now.date().isoformat(), "status": "open"},
        ]}))
        (output / "cache/world.json").write_text(json.dumps(world))
        decisions = home / ".assistant/decisions"
        decisions.mkdir(parents=True)
        (decisions / "decisions.jsonl").write_text("".join(json.dumps({
            "schema": "decision/1", "id": f"dec-test-{index}",
            "status": "open", "epoch": int(now.timestamp()),
            "source": "github", "kind": "review_requested", "lane": "staged",
            "title": f"Unrelated pull request {index}", "refs": {
                "repo": "example/project", "pr": index,
            },
        }) + "\n" for index in range(1, 50)))
        slack_root = home / ".assistant/slack-focus"
        slack_root.mkdir(parents=True)
        (home / ".assistant/comms").mkdir(parents=True)
        (home / ".assistant/comms/config.json").write_text(json.dumps({"slack_focus": {
            "watched_channels": {"C1": "team"}}}))
        slack_threads = [{"id": n, "first": {"text": f"thread {n}", "at": "1"}, "latest": []} for n in (1, 2, 3)]
        slack_items = [
            {"lane": "Do first", "text": "Answer Aalia about the take-trim design, which she asked about twice "
             "in the thread and again in a DM", "link": "https://acme.slack.com/archives/D1/p1",
             "thread_id": 1, "channel": "DM Aalia Habib", "who": "Aalia Habib", "last": "2026-10-18T09:30",
             "mention": True, "why": "she is waiting", "effort": "Quick"},
            {"lane": "This week", "text": "Share the roadmap", "link": "https://acme.slack.com/archives/C1/p2",
             "thread_id": 2, "channel": "#team", "who": "Tim Beauchamp", "last": "2026-10-17T10:00",
             "effort": "Medium"},
            {"lane": "Promise", "text": "Send the freeze plan", "link": "https://acme.slack.com/archives/C1/p3",
             "thread_id": 3, "channel": "#team", "who": "you", "last": ""},
        ]
        sys.path.insert(0, str(REPO / "src"))
        from assistant.slack_focus import store as slack_store
        for slack_item in slack_items:
            slack_item["sig"] = slack_store.signature(slack_threads[slack_item["thread_id"] - 1])
        (slack_root / "threads.json").write_text(json.dumps({"threads": slack_threads}))
        (slack_root / "focus.json").write_text(json.dumps({
            "generated": now.isoformat(), "window_days": 7, "judge": {"ran": True}, "active": slack_items,
            "read": [], "judge_dropped": [], "handled": []}))
        (slack_root / "done.json").write_text(json.dumps({slack_store.fingerprint(slack_items[2]): now.isoformat()}))
        with mock.patch.dict(os.environ, {"HOME": str(home)}):
            spec = importlib.util.spec_from_file_location(
                "browser_renderer", REPO / "bin/render-assistant-page.py")
            renderer = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(renderer)
            renderer.render()
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0), partial(QuietHandler, directory=str(output)))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(
                    headless=True, executable_path=browser_executable)
                context = browser.new_context(viewport={"width": 1440, "height": 1000})
                page = context.new_page()
                profiler = None
                if coverage_dir is not None:
                    coverage_dir.mkdir(parents=True, exist_ok=True)
                    profiler = page.context.new_cdp_session(page)
                    profiler.send("Profiler.enable")
                    profiler.send("Debugger.enable")
                    profiler.send("Profiler.startPreciseCoverage", {"callCount": True, "detailed": True})
                page.clock.install()
                errors = []
                mutations = []
                intercepted_writes = []
                page.on("pageerror", lambda error: errors.append(str(error)))

                def isolate_requests(route):
                    request = route.request
                    if request.method not in ("GET", "HEAD"):
                        intercepted_writes.append(request.url)
                        route.fulfill(status=200, content_type="application/json",
                                      body='{"ok":true}', headers={"Access-Control-Allow-Origin": "*"})
                    elif (request.url.startswith(f"http://127.0.0.1:{server.server_port}/")
                          or request.url.split("#", 1)[0] == (output / "assistant-dashboard.html").as_uri()):
                        route.continue_()
                    else:
                        route.abort()

                def reject_action(route):
                    mutations.append(route.request.url)
                    route.fulfill(status=200, body="ok")

                page.context.route("**/*", isolate_requests)
                page.route("**/focus/*", reject_action)
                url = f"http://127.0.0.1:{server.server_port}/assistant-dashboard.html"
                page.goto(url)
                page.wait_for_selector('[data-panel="overview"].active')
                assert not errors, errors
                assert page.locator('[data-tab="overview"]').inner_text().split() == ["Sessions", "10"]
                assert page.locator('[data-tab="brief"]').inner_text().strip() == "Notifications"
                assert page.locator('[data-tab="brief"] .tab-count').count() == 0
                assert page.locator('.session-scope').inner_text().startswith("10 cmux workspaces in the saved list")
                assert not page.locator('.review-topic').first.is_visible()
                page.locator('[data-tab="brief"]').click()
                assert page.locator('.review-topic').count() == 49
                assert "49 GitHub pull requests" in page.locator('.brief-summary').inner_text()
                assert "not open workspaces" in page.locator('.brief-summary').inner_text()
                page.get_by_role("button", name="Back to sessions", exact=True).click()
                assert page.locator('[data-panel="overview"]').is_visible()
                assert page.locator(".attention-context[open]").count() == 0
                visible_cards = page.locator(".attention-card:visible").count()
                assert visible_cards == 7, visible_cards
                assert page.locator(".attention-card .attention-actions button:visible").count() == visible_cards
                assert page.locator(".attention-context button[data-ws]").count() == 0
                assert page.locator('#task-workspace-id-1 .attention-actions button').is_enabled()
                assert "Task 9" in page.locator("#finish-current").inner_text()
                assert not mutations
                assert "Your answer is needed" in page.locator('#task-workspace-id-1').inner_text()
                assert "2 choices from your session" in page.locator('#task-workspace-id-1').inner_text()
                page.locator('#task-workspace-id-8 summary').click()
                page.evaluate("""() => Object.defineProperty(navigator, 'clipboard', {
                    configurable: true, value: {writeText: async text => {window.copiedPrompt = text;}}
                })""")
                page.locator('#task-workspace-id-8 .resume-copy').click()
                assert "Run the retry regression" in page.evaluate("window.copiedPrompt")
                assert "Don't merge, delete, close sessions" in page.evaluate("window.copiedPrompt")
                assert not mutations
                page.locator('#task-workspace-id-8 summary').click()
                measurements = []
                for width in (390, 820, 1440):
                    page.set_viewport_size({"width": width, "height": 1000})
                    measurement = page.evaluate("""() => ({
                        width: innerWidth,
                        offsetWidth: document.documentElement.offsetWidth,
                        scrollWidth: document.documentElement.scrollWidth,
                        visibleWords: document.querySelector('[data-panel="overview"]').innerText.split(/\\s+/).length,
                        overflowingCards: [...document.querySelectorAll('.attention-card')]
                            .filter(card => card.offsetWidth && card.scrollWidth > card.offsetWidth).length
                    })""")
                    assert measurement["scrollWidth"] == measurement["offsetWidth"], measurement
                    assert measurement["overflowingCards"] == 0, measurement
                    assert measurement["visibleWords"] <= 650, measurement
                    measurements.append(measurement)
                    page.screenshot(path=str(output_dir / f"overview-{width}.png"), full_page=True)
                page.locator("#attention-search").fill("Task 7:")
                assert page.locator(".attention-card:visible").count() == 1, {
                    "errors": errors,
                    "visible": page.locator(".attention-card:visible").all_inner_texts(),
                }
                assert page.locator(".attention-more[open]").count() == 1
                page.locator("#attention-search").fill("no such task")
                assert page.locator("#attention-search-empty").is_visible()
                page.locator("#attention-search").fill("")
                page.locator('#task-workspace-id-1 .attention-context > summary').click()
                assert "A detailed return note" in page.locator("#task-workspace-id-1").inner_text()
                assert "Keep the failure as a blocker." in page.locator("#task-workspace-id-1").inner_text()
                assert "Investigate" in page.locator("#task-workspace-id-1 .session-question").inner_text()
                awaitable = page.evaluate("refreshDashboard()")
                assert awaitable is None
                assert page.locator('#task-workspace-id-1 details').get_attribute("open") is not None
                summary_path = home / ".assistant/observer-summaries/workspace_1.json"
                updated_summary = json.loads(summary_path.read_text())
                updated_summary["summary"] = "A new result arrived while you were reading."
                summary_path.write_text(json.dumps(updated_summary))
                with mock.patch.dict(os.environ, {"HOME": str(home)}), mock.patch.object(
                        renderer, "utc_now", return_value=now + timedelta(seconds=3)):
                    renderer.render()
                page.get_by_role("button", name="Reload page", exact=True).click()
                page.wait_for_function("""() => document.querySelector('#task-workspace-id-1')
                    .innerText.includes('A new result arrived')""")
                assert page.locator('#task-workspace-id-1 details').get_attribute("open") is not None
                page.route("**/assistant-dashboard.html", lambda route: route.fulfill(
                    status=503, body="temporarily unavailable"))
                page.get_by_role("button", name="Reload page", exact=True).click()
                page.wait_for_function("""() => document.getElementById('refresh-error')
                    .textContent.includes('503')""")
                assert "A new result arrived" in page.locator("#task-workspace-id-1").inner_text()
                page.unroute("**/assistant-dashboard.html")
                page.locator('#task-workspace-id-1 summary').click()
                assert not page.locator('#task-workspace-id-1 details').get_attribute("open")
                page.locator('#task-workspace-id-1 button').click()
                assert len(mutations) == 1 and mutations[0].endswith(
                    "/focus/workspace:1?workspace_id=workspace-id-1")
                page.get_by_role("button", name="Review this task", exact=True).click()
                assert page.locator('#task-workspace-id-9 details').get_attribute("open") is not None
                page.locator('#task-workspace-id-9 summary').click()
                page.evaluate('openPending("td-901")')
                assert page.locator('[data-panel="todos"]').is_visible()
                assert page.locator('.todo-row[data-task-id="td-901"]').evaluate(
                    "element => document.activeElement === element")
                page.locator('[data-tab="overview"]').click()
                for version, focused in ((6, "tab"), (9, "summary"), (12, "refresh")):
                    updated_summary["summary"] = f"Polling still works with {focused} focus."
                    summary_path.write_text(json.dumps(updated_summary))
                    with mock.patch.dict(os.environ, {"HOME": str(home)}), mock.patch.object(
                            renderer, "utc_now", return_value=now + timedelta(seconds=version)):
                        renderer.render()
                    if focused == "summary":
                        page.locator('#task-workspace-id-1 summary').focus()
                    elif focused == "refresh":
                        page.locator('#refresh-dashboard').focus()
                    page.clock.fast_forward(15000)
                    page.wait_for_function(
                        "text => document.querySelector('#task-workspace-id-1').textContent.includes(text)",
                        arg=updated_summary["summary"])
                page.locator('#task-workspace-id-9 summary').click()
                page.evaluate("""() => {
                    const expired = String(Date.now() / 1000 - 599);
                    document.getElementById('task-workspace-id-9').dataset.evidenceAt = expired;
                    document.getElementById('finish-prompt').dataset.evidenceAt = expired;
                }""")
                page.clock.fast_forward(15000)
                assert page.locator('#task-workspace-id-9').get_attribute("data-lane") == "unknown"
                assert page.locator('#task-workspace-id-9 details').get_attribute("open") is not None
                assert "Check before closing" not in page.locator("#task-workspace-id-9").inner_text()
                assert page.locator("#finish-outdated").is_visible()
                page.evaluate("""() => {
                    const root = document.getElementById('dashboard-content');
                    root.dataset.snapshotAt = String(Date.now() / 1000 - 86400 * 15);
                    root.querySelector('[data-pulse-at]').dataset.pulseAt = root.dataset.snapshotAt;
                    updateFreshness();
                }""")
                assert "Session information is out of date" in page.locator("#snapshot-status").inner_text()
                assert page.locator("#finish-outdated").is_visible()
                assert not page.locator("#finish-current").is_visible()
                assert page.locator('.attention-card button:not([disabled])').count() == 0
                assert page.locator('.attention-card[data-lane="ready"]').count() == 0
                assert page.locator('.attention-card[data-lane="working"]').count() == 0
                assert page.locator('.attention-lane[data-lane="needs-you"] .attention-count').inner_text() == "0"
                assert page.locator('.attention-lane[data-lane="unknown"] .attention-count').inner_text() == "10"
                assert page.locator('[data-tab="overview"] .tab-count').inner_text() == "?"
                assert "The current workspace count hasn't been checked" in page.locator('.session-scope').inner_text()
                assert page.locator(".pulse-health").get_attribute("class").endswith("pulse-bad")
                assert not errors, errors
                page.screenshot(path=str(output_dir / "overview-stale.png"), full_page=True)

                render_version = 100

                def render_only():
                    nonlocal render_version
                    render_version += 1
                    (output / "cache/world.json").write_text(json.dumps(world))
                    with mock.patch.dict(os.environ, {"HOME": str(home)}), mock.patch.object(
                            renderer, "utc_now", return_value=now + timedelta(seconds=render_version)):
                        renderer.render()

                def refresh_fixture():
                    render_only()
                    page.evaluate("refreshDashboard(true)")

                refresh_fixture()
                page.locator('#task-workspace-id-8 summary').click()
                page.evaluate("""() => Object.defineProperty(navigator, 'clipboard', {
                    configurable: true, value: {writeText: async () => {
                        window.deniedCopies = (window.deniedCopies || 0) + 1;
                        throw new DOMException('Permission denied', 'NotAllowedError');
                    }}
                })""")
                page.locator('#task-workspace-id-8 .resume-copy').click()
                assert "Couldn't copy" in page.locator('#task-workspace-id-8 .copy-result').inner_text()
                assert page.locator('#task-workspace-id-8 textarea').evaluate(
                    "el => document.activeElement === el && el.selectionEnd === el.value.length")
                page.evaluate("""() => {
                    const card = document.getElementById('task-workspace-id-8');
                    card.dataset.evidenceAt = String(Date.now() / 1000 - 601);
                    updateFreshness();
                    copySessionPrompt(card.querySelector('.resume-copy'));
                }""")
                assert page.locator('#task-workspace-id-8 .resume-copy').is_disabled()
                assert "saved message is old" in page.locator('#task-workspace-id-8 .copy-result').inner_text()
                assert page.evaluate("window.deniedCopies") == 1
                refresh_fixture()
                page.evaluate("""() => {
                    document.getElementById('task-workspace-id-10').dataset.evidenceAt =
                        String(Date.now() / 1000 - 601);
                    updateFreshness();
                }""")
                assert page.locator('#task-workspace-id-10').get_attribute("data-lane") == "parked"
                assert page.locator('#task-workspace-id-10').get_attribute("data-expired") is None

                world["live_sessions"][1]["guidance_context"] = {"source_version": "updates-v1"}
                world["live_sessions"][2]["context_status"] = "unknown"
                world["workspaces"].append({
                    "ws_ref": "workspace:11", "workspace_id": "workspace-id-11",
                    "title": "Not linked yet",
                })
                notes_path = home / ".assistant/session-return-notes.json"
                notes = json.loads(notes_path.read_text())
                notes["sessions"].append({
                    "workspace_id": "workspace-id-2", "surface_id": "surface-id-2",
                    "provider": "claude", "session_id": "session-2", "source_version": "updates-v1",
                    "goal": "Archived retry findings", "progress": "You have the saved result.",
                    "recommendation": "review", "who": "nobody",
                })
                notes_path.write_text(json.dumps(notes))
                refresh_fixture()
                assert page.locator('#task-workspace-id-11').get_attribute("data-evidence-at") == ""
                assert "Not checked yet" in page.locator('#task-workspace-id-11 .attention-state').text_content()
                for lane, query in (("updates", "Archived retry"), ("unknown", "Task 3:")):
                    secondary = page.locator(f'[data-context-key="secondary-{lane}"]')
                    secondary.evaluate("el => el.open = false")
                    page.locator("#attention-search").fill(query)
                    assert secondary.get_attribute("open") is not None
                    assert page.locator(".attention-card:visible").count() == 1
                    page.locator("#attention-search").fill(query + " ")
                    page.locator("#attention-search").fill("")
                    assert secondary.get_attribute("open") is None
                    secondary.locator(":scope > summary").click()
                    page.locator("#attention-search").fill(query)
                    page.locator("#attention-search").fill("")
                    assert secondary.get_attribute("open") is not None
                page.evaluate('revealAttention("task-workspace-id-7")')
                assert page.locator('#task-workspace-id-7 details').get_attribute("open") is not None
                before_missing = page.locator("#dashboard-content").inner_html()
                page.evaluate('revealAttention("missing-card")')
                assert page.locator("#dashboard-content").inner_html() == before_missing
                page.evaluate('openPending("missing-task")')
                assert page.locator('[data-panel="todos"]').is_visible()
                assert not page.locator('.todo-row:focus').count()
                page.locator('[data-tab="overview"]').click()

                request_state = {
                    "generated_at": now.isoformat(),
                    "awaiting_input": [{"title": "Legacy workspace question", "confidence": 1,
                                        "touches": ["workspace:77"], "tier": "T1"}],
                }
                state_path = output / "cache/assistant-state.json"
                state_path.write_text(json.dumps(request_state))
                refresh_fixture()
                page.locator('[data-tab="decisions"]').click()
                focus_before = len(mutations)
                page.locator('#assistant-requests button[data-ws="workspace:77"]').click()
                page.wait_for_function("""() => document.querySelector(
                    '#assistant-requests button[data-ws="workspace:77"]').textContent.includes('✓')""")
                assert len(mutations) == focus_before + 1
                assert mutations[-1].endswith("/focus/workspace:77")
                for stamp in (None, "", now.timestamp() + 3600, now.timestamp() - 601):
                    refresh_fixture()
                    focus_before = len(mutations)
                    page.evaluate("""async stamp => {
                        const requests = document.getElementById('assistant-requests');
                        requests.dataset.generatedAt = String(stamp);
                        await openWs(requests.querySelector('button[data-ws]'));
                    }""", stamp)
                    assert len(mutations) == focus_before
                    assert page.locator('#assistant-requests button[data-ws]').count() == 0
                    assert "Saved reference: workspace:77" in page.locator("#assistant-requests").inner_text()
                    assert page.locator("#saved-requests-warning").is_visible()
                refresh_fixture()
                page.locator('[data-tab="overview"]').click()

                brief_dir = home / ".assistant/brief"
                brief_dir.mkdir(exist_ok=True)
                first_date = now.date().isoformat()
                next_date = (now + timedelta(days=1)).date().isoformat()
                (brief_dir / f"brief-{first_date}.json").write_text(json.dumps({"epoch": now.timestamp()}))
                refresh_fixture()
                with page.expect_response(lambda response: "/brief/seen?" in response.url) as first_seen:
                    page.locator('[data-tab="brief"]').click()
                assert first_seen.value.status == 200
                seen = [url for url in intercepted_writes if "/brief/seen?" in url]
                assert len(seen) == 1 and seen[0].endswith(first_date), seen
                refresh_fixture()
                assert [url for url in intercepted_writes if "/brief/seen?" in url] == seen
                (brief_dir / f"brief-{next_date}.json").write_text(json.dumps({"epoch": now.timestamp()}))
                with page.expect_response(lambda response: "/brief/seen?" in response.url) as next_seen:
                    refresh_fixture()
                assert next_seen.value.status == 200
                seen = [url for url in intercepted_writes if "/brief/seen?" in url]
                assert len(seen) == 2 and seen[-1].endswith(next_date), seen
                page.locator('[data-tab="overview"]').click()

                page.locator('[data-tab="todos"]').click()
                for selector, endpoint in (
                        ('.td-remove[data-id="td-901"]', "/remove/td-901"),
                        ('.td-dispatch[data-id="td-901"]', "/dispatch-now/td-901")):
                    before_cancel = len(intercepted_writes)
                    page.once("dialog", lambda dialog: dialog.dismiss())
                    page.locator(selector).click()
                    assert len(intercepted_writes) == before_cancel
                    page.once("dialog", lambda dialog: dialog.accept())
                    with page.expect_response(lambda response, endpoint=endpoint:
                                              response.url.endswith(endpoint)) as requested:
                        page.locator(selector).click()
                    assert requested.value.status == 200
                    assert intercepted_writes[-1].endswith(endpoint)
                    assert len(intercepted_writes) == before_cancel + 1
                page.locator('[data-tab="overview"]').click()

                page.route("**/assistant-dashboard.html", lambda route: route.fulfill(
                    status=200, body="<main>No dashboard content</main>"))
                preserved = page.locator(".attention-card").all_text_contents()
                page.evaluate("refreshDashboard(true)")
                assert "Dashboard content is unavailable" in page.locator("#refresh-error").inner_text()
                assert page.locator(".attention-card").all_text_contents() == preserved
                page.unroute("**/assistant-dashboard.html")
                page.route("**/assistant-dashboard.html", lambda route: route.abort("failed"))
                page.evaluate("refreshDashboard(true)")
                assert "Refresh failed" in page.locator("#refresh-error").inner_text()
                page.unroute("**/assistant-dashboard.html")
                pending_refresh = []
                page.route("**/assistant-dashboard.html", lambda route: pending_refresh.append(route))
                page.evaluate("() => { window.refreshResult = refreshDashboard(true); }")
                page.wait_for_timeout(50)
                assert len(pending_refresh) == 1
                page.evaluate("refreshDashboard(true)")
                assert len(pending_refresh) == 1
                pending_refresh[0].fulfill(status=200, content_type="text/html",
                                          body=(output / "assistant-dashboard.html").read_text())
                page.evaluate("window.refreshResult")
                page.unroute("**/assistant-dashboard.html")
                assert page.locator("#refresh-error").inner_text() == ""

                page.evaluate("""() => {
                    const root = document.getElementById('dashboard-content');
                    root.remove();
                    updateFreshness();
                    document.body.append(root);
                    root.dataset.snapshotAt = '';
                    updateFreshness();
                }""")
                assert "Sessions haven't been checked yet" in page.locator("#snapshot-status").inner_text()
                refresh_fixture()
                for delta, message, color in (
                        (60, "Heartbeat time is in the future", "pulse-bad"),
                        (-900, "Pulse slow", "pulse-warn")):
                    page.evaluate("""delta => {
                        document.querySelector('[data-pulse-at]').dataset.pulseAt =
                            String(Date.now() / 1000 + delta);
                        updateFreshness();
                    }""", delta)
                    assert message in page.locator(".pulse-health").text_content()
                    assert color in page.locator(".pulse-health").get_attribute("class")
                page.evaluate("""() => {
                    history.replaceState(null, '', location.pathname);
                    window.hashlessRefresh = refreshDashboard(true);
                }""")
                page.evaluate("window.hashlessRefresh")
                refresh_fixture()
                assert page.locator('[data-panel="overview"]').is_visible()

                load_profiler = None
                slack_calls = []
                slack_script = {"status": [], "refresh": 200, "mark": 200}

                def slack_route(route):
                    action = route.request.url.rsplit("/slack-focus/", 1)[1]
                    slack_calls.append((action, route.request.post_data))
                    if action == "status":
                        answer = slack_script["status"].pop(0) if slack_script["status"] else {"running": False}
                        if answer == 500:
                            route.fulfill(status=500, body="down")
                        else:
                            route.fulfill(status=200, content_type="application/json", body=json.dumps(answer))
                    elif action == "refresh":
                        route.fulfill(status=slack_script["refresh"], content_type="application/json",
                                      body='{"started": true}')
                    elif slack_script["mark"] == 200:
                        slack_store.set_done(home, json.loads(route.request.post_data)["fp"], action == "done")
                        render_only()
                        route.fulfill(status=200, body="saved")
                    else:
                        route.fulfill(status=slack_script["mark"], body="fp must be a 64-character hex fingerprint")

                page.route("**/slack-focus/*", slack_route)
                page.locator('[data-tab="slack"]').click()
                slack_panel = page.locator('[data-panel="slack"]')
                assert page.locator('[data-tab="slack"] .tab-count').inner_text() == "1"
                assert slack_panel.locator(".sf-do .sf-row").count() == 1
                assert "@you" in slack_panel.locator(".sf-do .sf-row").inner_text()
                assert slack_panel.locator('[data-context-key="sf-done"] .sf-row').count() == 1
                for width in (390, 820, 1440):
                    page.set_viewport_size({"width": width, "height": 1000})
                    measurement = page.evaluate("""() => ({
                        offsetWidth: document.documentElement.offsetWidth,
                        scrollWidth: document.documentElement.scrollWidth,
                        overflowingRows: [...document.querySelectorAll('[data-panel="slack"] .sf-row')]
                            .filter(row => row.offsetWidth && row.scrollWidth > row.offsetWidth).length
                    })""")
                    assert measurement["scrollWidth"] == measurement["offsetWidth"], measurement
                    assert measurement["overflowingRows"] == 0, measurement
                    page.screenshot(path=str(output_dir / f"slack-{width}.png"), full_page=True)
                slack_script["status"] = [{"running": True, "phase": "Pulling Slack (1 of 2 threads)"},
                                          {"running": False, "ok": True}]
                page.locator("#sf-refresh").click()
                page.wait_for_function("""() => document.getElementById('sf-status')
                    .textContent === 'Refreshing: Pulling Slack (1 of 2 threads).'""")
                assert page.locator("#sf-refresh").is_disabled()
                assert page.locator("#sf-refresh").inner_text() == "Refreshing…"
                page.evaluate("() => { slackFocusPoll(); }")
                page.clock.fast_forward(2000)
                page.wait_for_function("text => document.getElementById('sf-status').textContent === text",
                                       arg="Refresh finished. The list updates within a minute.")
                assert [a for a, _ in slack_calls] == ["refresh", "status", "status"], slack_calls
                assert page.locator("#sf-refresh").inner_text() == "Refresh from Slack"
                for answer, message in (({"running": False, "ok": False, "error": "Slack search.messages failed"},
                                         "The refresh didn't finish: Slack search.messages failed"),
                                        ({"running": False, "ok": False},
                                         "The refresh didn't finish: unknown error")):
                    slack_script["status"] = [answer]
                    page.evaluate("slackFocusPoll()")
                    assert page.locator("#sf-status").inner_text() == message
                slack_script["status"] = [{"running": True}, 500]
                page.evaluate("() => { slackFocusPoll(); }")
                page.wait_for_function(
                    "() => document.getElementById('sf-status').textContent === 'Refreshing: Starting.'")
                page.clock.fast_forward(2000)
                page.wait_for_function("text => document.getElementById('sf-status').textContent === text",
                                       arg="Couldn't check the refresh: the server returned 500")
                slack_script["refresh"] = 500
                page.locator("#sf-refresh").click()
                page.wait_for_function("text => document.getElementById('sf-status').textContent === text",
                                       arg="Couldn't start the refresh: the server returned 500")
                assert not page.locator("#sf-refresh").is_disabled()
                slack_script["refresh"] = 200
                fingerprint = page.locator('.sf-do .sf-mark').get_attribute("data-fp")
                page.locator('.sf-do .sf-mark').click()
                page.wait_for_function("() => document.querySelectorAll('[data-context-key=\"sf-done\"] .sf-row').length === 2")
                assert slack_calls[-1] == ("done", json.dumps({"fp": fingerprint}, separators=(",", ":")))
                assert page.locator(".sf-do .sf-row").count() == 0
                assert page.locator('[data-tab="slack"] .tab-count').inner_text() == "0"
                page.locator('[data-context-key="sf-done"] > summary').click()
                page.locator(f'[data-context-key="sf-done"] .sf-mark[data-fp="{fingerprint}"]').click()
                page.wait_for_function("() => document.querySelectorAll('.sf-do .sf-row').length === 1")
                assert slack_calls[-1][0] == "undone"
                slack_script["mark"] = 400
                page.locator('.sf-do .sf-mark').click()
                page.wait_for_function("text => document.querySelector('.sf-do .sf-mark').textContent === text",
                                       arg="Couldn't save. Try again.")
                assert not page.locator('.sf-do .sf-mark').is_disabled()
                slack_script["mark"] = 200
                page.evaluate("""() => {
                    document.getElementById('sf-status').remove();
                    slackFocusShow({running: true});
                }""")
                assert not page.locator("#sf-refresh").is_disabled()
                def advance_until(predicate, on, steps=20):
                    for _ in range(steps):
                        if on.evaluate(predicate):
                            return
                        on.clock.fast_forward(1000)
                        on.wait_for_timeout(50)
                    raise AssertionError(predicate)

                def wait_for_calls(count, on=page):
                    for _ in range(100):
                        if len(slack_calls) >= count:
                            return
                        on.wait_for_timeout(20)
                    raise AssertionError((count, slack_calls))

                running_state = json.dumps({"running": True, "started": slack_store.now_iso(), "phase": "Sorting threads"})
                shows_running = "() => document.getElementById('slack-focus').dataset.running === 'true'"

                def finished_at(seconds):
                    return {"running": False, "ok": True,
                            "finished": (now + timedelta(seconds=seconds)).isoformat()}

                load_page = page.context.new_page()
                load_page.on("pageerror", lambda error: errors.append(str(error)))
                load_page.clock.install()
                load_page.route("**/slack-focus/*", slack_route)
                if profiler is not None:
                    load_profiler = page.context.new_cdp_session(load_page)
                    load_profiler.send("Profiler.enable")
                    load_profiler.send("Debugger.enable")
                    load_profiler.send("Profiler.startPreciseCoverage", {"callCount": True, "detailed": True})
                for resume, on in (("refresh", page), ("load", load_page)):
                    (slack_root / "state.json").write_text(running_state)
                    before_resume = len(slack_calls)
                    slack_script["status"] = [finished_at(render_version + 2)]
                    if resume == "refresh":
                        refresh_fixture()
                    else:
                        render_only()
                        load_page.goto(url + "#slack")
                    wait_for_calls(before_resume + 1, on)
                    assert on.evaluate(shows_running), resume
                    assert on.locator("#sf-status").inner_text() == "Loading the new list…"
                    (slack_root / "state.json").unlink()
                    render_only()
                    advance_until("() => document.getElementById('slack-focus').dataset.running === 'false'", on)
                    assert [a for a, _ in slack_calls[before_resume:]] == ["status"], slack_calls[before_resume:]
                    assert not on.locator("#sf-refresh").is_disabled()
                    assert on.locator("#sf-status").inner_text().startswith("Last pulled")
                (slack_root / "state.json").write_text(running_state)
                slack_script["status"] = [finished_at(10 ** 6)]
                before_stuck = len(slack_calls)
                refresh_fixture()
                wait_for_calls(before_stuck + 1)
                for _ in range(17):
                    page.clock.fast_forward(1000)
                    page.wait_for_timeout(30)
                assert page.evaluate(shows_running)
                assert page.locator("#sf-status").inner_text() == "Refresh finished. The list updates within a minute."
                (slack_root / "state.json").unlink()
                refresh_fixture()
                page.unroute("**/slack-focus/*")
                page.locator('[data-tab="overview"]').click()
                assert not errors, errors

                file_page = page.context.new_page()
                file_errors = []
                file_page.on("pageerror", lambda error: file_errors.append(str(error)))
                file_profiler = None
                if profiler is not None:
                    file_profiler = page.context.new_cdp_session(file_page)
                    file_profiler.send("Profiler.enable")
                    file_profiler.send("Debugger.enable")
                    file_profiler.send("Profiler.startPreciseCoverage", {"callCount": True, "detailed": True})
                file_page.goto((output / "assistant-dashboard.html").as_uri() + "#invalid-tab")
                assert file_page.locator('[data-panel="overview"]').is_visible()
                requests_after_load = []
                file_page.on("request", lambda request: requests_after_load.append(request.url))
                file_page.evaluate("refreshDashboard(true)")
                assert not requests_after_load, requests_after_load
                assert file_page.locator("#refresh-error").inner_text() == ""
                assert not file_errors, file_errors
                assert not errors, errors
                if profiler is not None:
                    scripts = []
                    for collector in (profiler, file_profiler, load_profiler):
                        entries = collector.send("Profiler.takePreciseCoverage")["result"]
                        for entry in entries:
                            source = collector.send("Debugger.getScriptSource", {
                                "scriptId": entry["scriptId"]})["scriptSource"]
                            if "function showTab(name)" in source and "function updateFreshness()" in source:
                                scripts.append({**entry, "source": source})
                        collector.send("Profiler.stopPreciseCoverage")
                    assert scripts, "Dashboard script was not captured by the browser coverage profiler"
                    (coverage_dir / "browser-v8.json").write_text(json.dumps(scripts, indent=2))
                browser.close()
                print(json.dumps({"measurements": measurements, "browser_errors": errors,
                                  "explicit_focus_requests": len(mutations),
                                  "intercepted_brief_seen_requests": len(seen),
                                  "coverage_script_instances": len(scripts) if profiler is not None else 0,
                                  "checks": "grouping, density, search restoration, expansion, clipboard denial and expiry, reading stability, legacy focus, missing task guards, refresh errors and concurrency, brief date changes, file protocol, observation expiry, stale request gating, Slack refresh, check-off, and errors"}))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--browser-executable")
    parser.add_argument("--coverage-dir", type=Path)
    args = parser.parse_args()
    drive(args.output_dir, args.browser_executable, args.coverage_dir)


if __name__ == "__main__":
    main()
