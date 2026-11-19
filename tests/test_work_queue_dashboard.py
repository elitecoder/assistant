"""The dashboard's Work queue tab appears only in Pro mode."""
from __future__ import annotations

import json

import pytest

from test_pro_mode import home, render_page, set_mode  # noqa: F401

pytestmark = pytest.mark.real_mode
TAB = '<button class="tab" data-tab="work-queue"'


def test_default_mode_hides_the_work_queue_tab(home, monkeypatch):
    monkeypatch.delenv("WQ_DATA", raising=False)
    page = render_page(home)
    assert TAB not in page and 'data-panel="work-queue"' not in page and "127.0.0.1:8851" not in page


def test_pro_mode_links_the_work_queue_and_says_when_it_last_refreshed(home, monkeypatch):
    monkeypatch.delenv("WQ_DATA", raising=False)
    monkeypatch.delenv("REVIEW_PORT", raising=False)
    set_mode(home, "pro")
    page = render_page(home)
    panel = page[page.index('data-panel="work-queue"'):]
    assert TAB in page and 'href="http://127.0.0.1:8851/"' in panel and "Not refreshed yet." in panel
    (home / ".assistant/work-queue").mkdir(parents=True)
    (home / ".assistant/work-queue/sources.json").write_text(json.dumps({"port": 9123}))
    (home / ".claude/work-queue").mkdir(parents=True)
    (home / ".claude/work-queue/refresh-status.json").write_text(json.dumps(
        {"finished": "2026-11-18T09:00:00+00:00", "error": "Outlook: <b>expired</b>"}))
    panel = render_page(home).split('data-panel="work-queue"')[1]
    assert 'href="http://127.0.0.1:9123/"' in panel
    assert "Last refreshed 2026-11-18T09:00:00+00:00. Last refresh problems: Outlook: &lt;b&gt;expired&lt;/b&gt;" in panel
    (home / ".assistant/work-queue/sources.json").write_text("{torn")
    panel = render_page(home).split('data-panel="work-queue"')[1]
    assert "The Work queue can't read its settings:" in panel and "isn&#x27;t valid JSON" in panel
