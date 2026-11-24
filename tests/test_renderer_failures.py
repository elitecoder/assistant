"""The dashboard shows a failure that keeps repeating once, with a count."""
import re
from datetime import datetime

import pytest

from assistant import failwatch
from test_renderer_in_process import load_module

NOW = datetime(2026, 11, 20, 12, 0).timestamp()  # a Friday, noon local time


@pytest.fixture
def mod(tmp_path):
    (tmp_path / ".assistant").mkdir()
    return load_module(tmp_path)


def fail(n, text, component="self-update", start=NOW - 3600, step=60):
    for i in range(n):
        failwatch.record_failure(component, text, now=start + i * step)


def test_nothing_shows_below_three_repeats(mod):
    fail(2, "boom")
    assert mod.render_failure_alerts(now=NOW) == ""
    fail(1, "boom", start=NOW - 3000)
    assert "has failed 3 times" in mod.render_failure_alerts(now=NOW)


def test_the_alert_says_what_how_often_since_when_and_the_error(mod):
    first = datetime(2026, 11, 16, 9, 10).timestamp()  # the Monday it began
    fail(13, "fetch failed: fatal: couldn't find remote ref feat/x", start=first, step=1)
    fail(1, "fetch failed: fatal: couldn't find remote ref feat/x", start=NOW - 60)
    (alert,) = re.findall(r"<span class=\"pulse-text\">(.*?)</span>", mod.render_failure_alerts(now=NOW))
    assert alert.startswith("Self-update has failed 14 times since Monday 09:10 with the same error: "
                            "fetch failed: fatal: couldn&#x27;t find remote ref feat/x.")
    assert "paused" in alert
    html = mod.render_failure_alerts(now=NOW)
    assert 'role="alert"' in html and "pulse-bad" in html


def test_paused_until_shows_only_while_backed_off(mod):
    start = NOW - 600
    fail(3, "boom", start=start, step=1)
    until = failwatch.backed_off("self-update", now=start + 3)
    assert until
    paused = mod.render_failure_alerts(now=NOW)
    assert f"Assistant has paused retries until {datetime.fromtimestamp(until).strftime('%H:%M')}." in paused
    assert "paused" not in mod.render_failure_alerts(now=until + 1)
    failwatch.record_ok("self-update")
    assert "paused" not in mod.render_failure_alerts(now=NOW)
    for i in range(4):
        failwatch.record_failure("dashboard-render", "boom", now=NOW - 10 + i, pause=False)
    unpaused = mod.render_failure_alerts(now=NOW)
    assert "Refreshing this page has failed 4 times" in unpaused and "paused" not in unpaused


def test_a_pause_that_ends_on_another_day_names_the_weekday(mod):
    fail(8, "boom", start=NOW - 60, step=1)  # the 24-hour cap, from just before noon on a Friday
    assert re.search(r"paused retries until Saturday \d\d:\d\d\.", mod.render_failure_alerts(now=NOW))


def test_at_most_five_most_frequent_first(mod):
    for i in range(7):
        fail(3 + i, f"error {chr(97 + i)}", component=f"c{i}")
    html = mod.render_failure_alerts(now=NOW)
    assert html.count('data-failure="repeated"') == 5
    counts = [int(c) for c in re.findall(r"has failed (\d+) times", html)]
    assert counts == [9, 8, 7, 6, 5]


def test_the_error_is_html_escaped(mod):
    fail(3, "<script>alert(1)</script> & <b>x</b>")
    html = mod.render_failure_alerts(now=NOW)
    assert "<script>" not in html and "<b>" not in html
    assert "&lt;script&gt;" in html and "&amp;" in html


def test_friendly_names_and_unknown_components(mod):
    fail(3, "a", component="model:lessons")
    fail(3, "b", component="ready-for-merge:workspace:7")
    fail(3, "c", component="something-new")
    html = mod.render_failure_alerts(now=NOW)
    assert "Lesson extraction has failed" in html
    assert "The merge check for workspace:7 has failed" in html
    assert "something-new has failed" in html


def test_default_time_is_now(mod):
    fail(3, "boom", start=datetime.now().timestamp() - 100, step=1)
    assert "Self-update has failed 3 times" in mod.render_failure_alerts()


def test_the_alert_is_on_the_page_at_the_top(tmp_path, monkeypatch):
    from test_pro_mode import render_page
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".assistant").mkdir()
    assert "has failed" not in render_page(tmp_path)
    fail(3, "boom", start=datetime.now().timestamp() - 100, step=1)
    page = render_page(tmp_path)
    assert page.count("Self-update has failed 3 times") == 1
    assert page.index("Self-update has failed") < page.index('<div class="tabs">')
