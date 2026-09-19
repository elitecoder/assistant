"""Drive the rendered overview in a real browser without touching live sessions."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
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


def drive(output_dir, browser_executable):
    output_dir.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="assistant-browser-") as temporary:
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
                page = browser.new_page(viewport={"width": 1440, "height": 1000})
                page.clock.install()
                errors = []
                mutations = []
                page.on("pageerror", lambda error: errors.append(str(error)))

                def reject_action(route):
                    mutations.append(route.request.url)
                    route.fulfill(status=200, body="ok")

                page.route("**/focus/*", reject_action)
                url = f"http://127.0.0.1:{server.server_port}/assistant-dashboard.html"
                page.goto(url)
                page.wait_for_selector('[data-panel="overview"].active')
                assert not errors, errors
                assert page.locator('[data-tab="overview"]').inner_text().split() == ["Sessions", "10"]
                assert page.locator('[data-tab="brief"]').inner_text().strip() == "Notifications"
                assert page.locator('[data-tab="brief"] .tab-count').count() == 0
                assert page.locator('.session-scope').inner_text().startswith("10 open cmux workspaces")
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
                assert "Finish the older retry fix" in page.locator("#finish-current").inner_text()
                assert not mutations
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
                page.get_by_role("button", name="Refresh view", exact=True).click()
                page.wait_for_function("""() => document.querySelector('#task-workspace-id-1')
                    .innerText.includes('A new result arrived')""")
                assert page.locator('#task-workspace-id-1 details').get_attribute("open") is not None
                page.route("**/assistant-dashboard.html", lambda route: route.fulfill(
                    status=503, body="temporarily unavailable"))
                page.get_by_role("button", name="Refresh view", exact=True).click()
                page.wait_for_function("""() => document.getElementById('refresh-error')
                    .textContent.includes('503')""")
                assert "A new result arrived" in page.locator("#task-workspace-id-1").inner_text()
                page.unroute("**/assistant-dashboard.html")
                page.locator('#task-workspace-id-1 button').click()
                assert len(mutations) == 1 and mutations[0].endswith(
                    "/focus/workspace:1?workspace_id=workspace-id-1")
                page.locator('#task-workspace-id-1 summary').click()
                page.get_by_role("button", name="Review this task", exact=True).click()
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
                assert page.locator('#task-workspace-id-9').get_attribute("data-lane") == "needs-you"
                assert page.locator('#task-workspace-id-9 details').get_attribute("open") is not None
                assert "Check before closing" not in page.locator("#task-workspace-id-9").inner_text()
                assert page.locator("#finish-outdated").is_visible()
                page.evaluate("""() => {
                    const root = document.getElementById('dashboard-content');
                    root.dataset.snapshotAt = String(Date.now() / 1000 - 86400 * 15);
                    root.querySelector('[data-pulse-at]').dataset.pulseAt = root.dataset.snapshotAt;
                    updateFreshness();
                }""")
                assert "Outdated snapshot" in page.locator("#snapshot-status").inner_text()
                assert page.locator("#finish-outdated").is_visible()
                assert not page.locator("#finish-current").is_visible()
                assert page.locator('.attention-card button:not([disabled])').count() == 0
                assert page.locator('.attention-card[data-lane="ready"]').count() == 0
                assert page.locator('.attention-card[data-lane="working"]').count() == 0
                assert page.locator('[data-tab="overview"] .tab-count').inner_text() == "?"
                assert "Current workspace count is unverified" in page.locator('.session-scope').inner_text()
                assert page.locator(".pulse-health").get_attribute("class").endswith("pulse-bad")
                assert not errors, errors
                page.screenshot(path=str(output_dir / "overview-stale.png"), full_page=True)
                browser.close()
                print(json.dumps({"measurements": measurements, "browser_errors": errors,
                                  "explicit_focus_requests": len(mutations),
                                  "checks": "grouping, density, search, expansion, reading stability, focus, task navigation, automatic refresh, observation expiry, stale gating"}))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--browser-executable")
    args = parser.parse_args()
    drive(args.output_dir, args.browser_executable)


if __name__ == "__main__":
    main()
