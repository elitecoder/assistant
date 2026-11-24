"""Decisions, the morning brief, and the goals planner are off until someone
turns them on, and so is the lesson extractor's pass over repeated actions.

conftest's `_watcher_on` fixture forces Pro mode and turns these jobs on for
other tests; the `home` fixture here puts the real settings back, so each test
sees the defaults.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import time
from pathlib import Path
from unittest import mock

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
from assistant import mode  # noqa: E402

REAL_JOB_SETTINGS = mode.job_settings


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".assistant").mkdir()
    (tmp_path / ".claude").mkdir()
    monkeypatch.setattr(mode, "job_settings", REAL_JOB_SETTINGS)
    monkeypatch.delenv("ASSISTANT_LESSON_LEDGER_PASS", raising=False)
    return tmp_path


def write_config(home: Path, text) -> None:
    (home / ".assistant/config.json").write_text(text if isinstance(text, str) else json.dumps(text))


@pytest.fixture
def pulse(home):
    spec = importlib.util.spec_from_file_location("pulse_jobs_off", REPO / "bin/pulse.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def renderer(home):
    spec = importlib.util.spec_from_file_location("renderer_jobs_off", REPO / "bin/render-assistant-page.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ─── the setting ─────────────────────────────────────────────────────────────

def test_every_job_is_off_by_default(home):
    assert mode.job_settings() == {"decisions": False, "brief": False, "planner": False}


def test_config_turns_each_job_on_by_itself(home):
    for name in ("decisions", "brief", "planner"):
        write_config(home, {"jobs": {name: True}})
        assert mode.job_settings() == {n: n == name for n in ("decisions", "brief", "planner")}


@pytest.mark.parametrize("var", ["ASSISTANT_DECISIONS", "ASSISTANT_BRIEF", "ASSISTANT_PLANNER"])
def test_the_variables_no_longer_turn_anything_on(home, pulse, monkeypatch, var):
    """The dashboard is drawn by another process, so only the config file counts."""
    monkeypatch.setenv(var, "1")
    assert not any(mode.job_settings().values())
    assert not any(pulse.job_enabled(job) for job in ("triage", "brief", "planner"))


@pytest.mark.parametrize("value", ["true", "yes", 1, "1", [True], {"enabled": True}, None, False, 0])
def test_only_the_json_true_counts(home, value):
    write_config(home, {"jobs": {"decisions": value, "brief": value, "planner": value}})
    assert not any(mode.job_settings().values())


@pytest.mark.parametrize("config", ['{"jobs": true}', '{"jobs": ["decisions"]}', '{"jobs": null}',
                                    '{"mode": "pro"}', "{torn", "[]"])
def test_a_wrong_shape_or_unreadable_config_leaves_every_job_off(home, config):
    write_config(home, config)
    assert not any(mode.job_settings().values())


@pytest.mark.parametrize("value, raw", [("true", '"true"'), (1, "1"), ("yes", '"yes"'),
                                        ([True], "[true]"), (None, "null")])
def test_a_value_that_is_ignored_is_reported(home, value, raw):
    write_config(home, {"jobs": {"decisions": value}})
    assert mode.ignored_job_value("decisions") == raw
    assert mode.job_settings()["decisions"] is False


@pytest.mark.parametrize("config", [{"jobs": {"decisions": True}}, {"jobs": {"decisions": False}},
                                    {"jobs": {}}, {"jobs": "x"}, {}, "{torn"])
def test_a_true_false_or_absent_value_is_not_reported_as_ignored(home, config):
    write_config(home, config)
    assert mode.ignored_job_value("decisions") is None


# ─── the pulse ───────────────────────────────────────────────────────────────

def test_pro_mode_alone_runs_none_of_the_three_jobs(pulse, monkeypatch):
    monkeypatch.setenv("ASSISTANT_STRATEGIST", "1")
    assert not any(pulse.job_enabled(job) for job in ("triage", "brief", "planner"))
    assert pulse.job_enabled("strategist")


def test_config_turns_the_jobs_on_in_pro_mode(home, pulse):
    write_config(home, {"jobs": {"decisions": True, "planner": True}})
    assert pulse.job_enabled("triage") and pulse.job_enabled("planner")
    assert not pulse.job_enabled("brief")
    write_config(home, {"jobs": {"brief": True}})
    assert pulse.job_enabled("brief")


def test_default_mode_runs_none_of_them_even_when_turned_on(home, pulse, monkeypatch):
    monkeypatch.setattr(mode, "current", lambda: "default")
    write_config(home, {"jobs": {"decisions": True, "brief": True, "planner": True}})
    monkeypatch.setenv("ASSISTANT_BRIEF_NARRATOR", "1")
    assert not any(pulse.job_enabled(job) for job in ("triage", "brief", "planner", "narrator"))


def test_the_narrator_needs_the_brief(home, pulse, monkeypatch):
    monkeypatch.setenv("ASSISTANT_BRIEF_NARRATOR", "1")
    assert not pulse.job_enabled("narrator")
    write_config(home, {"jobs": {"brief": True}})
    assert pulse.job_enabled("narrator")


def test_the_pulse_reads_the_job_settings_once(home, pulse):
    write_config(home, {"jobs": {"decisions": True}})
    pulse.PULSE_JOBS = {"decisions": False, "brief": False, "planner": False}
    assert not pulse.job_enabled("triage")
    pulse.PULSE_JOBS = None
    assert pulse.job_enabled("triage")


def test_main_reads_the_job_settings_into_the_pulse(home, pulse, monkeypatch):
    write_config(home, {"jobs": {"brief": True}})
    monkeypatch.setattr(sys, "argv", ["pulse.py", "--dry-run"])
    seen = {}
    monkeypatch.setattr(pulse, "load_state", lambda: seen.update(jobs=pulse.PULSE_JOBS) or (_ for _ in ()).throw(SystemExit(0)))
    with pytest.raises(SystemExit):
        pulse.main()
    assert seen["jobs"] == {"decisions": False, "brief": True, "planner": False}


def seed_events(n: int) -> Path:
    from assistant import triage
    path = triage.events_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        for i in range(n):
            f.write(json.dumps({"schema": triage.EVENT_SCHEMA,
                                "id": f"e{path.stat().st_size}-{i}"}) + "\n")
    return path


def test_default_mode_never_shows_open_urgent_decisions_on_the_awaiting_list(home, pulse, monkeypatch):
    from assistant import decisions
    decisions.open_decision(event=event("u"), lane="escalate", policy_id="r", now=time.time() - 86400)
    monkeypatch.setattr(mode, "current", lambda: "default")
    assert pulse.run_triage_step(1) == {"cards": []}
    monkeypatch.setattr(mode, "current", lambda: "pro")
    assert len(pulse.run_triage_step(1)["cards"]) == 1


def test_with_decisions_off_no_event_becomes_a_decision_and_the_cursor_skips(home, pulse):
    from assistant import decisions, triage
    events = seed_events(5)
    with mock.patch.object(triage, "triage_new_events") as lane:
        assert pulse.run_triage_step(1) == {"cards": []}
    lane.assert_not_called()
    assert json.loads(triage.cursor_path().read_text())["offset"] == events.stat().st_size
    assert decisions.open_decisions() == []


def event(external_id: str) -> dict:
    return {"source": "github", "external_id": external_id, "id": external_id}


def test_turning_decisions_on_later_replays_nothing(home, pulse, monkeypatch):
    """Events that arrive while decisions are off never become decisions; only
    events after the switch do."""
    from assistant import decisions, triage
    seed_events(5)
    pulse.run_triage_step(1)
    write_config(home, {"jobs": {"decisions": True}})
    seed_events(2)
    monkeypatch.setattr(pulse, "call_triage_batch", lambda events, idx: {})
    with mock.patch.object(triage, "triage_new_events", wraps=triage.triage_new_events) as lane:
        pulse.run_triage_step(2)
    assert lane.call_args.kwargs["pulse_idx"] == 2
    ids = [d["event_ref"] for d in decisions.open_decisions()]
    assert len(ids) == 2
    assert not set(ids) & {e["id"] for e in map(json.loads, triage.events_path().read_text().splitlines()[:5])}


def test_open_decisions_still_expire_while_decisions_are_off(home, pulse):
    from assistant import decisions
    five_days_ago = time.time() - 5 * 86400
    staged, _ = decisions.open_decision(event=event("s"), lane="staged", policy_id="r",
                                        ttl_h=72, now=five_days_ago)
    digest, _ = decisions.open_decision(event=event("d"), lane="digest", policy_id="r",
                                        ttl_h=24, now=five_days_ago)
    pulse.run_triage_step(1)
    status = {d["id"]: d["status"] for d in decisions.fold(decisions.read_log()).values()}
    assert status[staged["id"]] == "expired" and status[digest["id"]] == "expired"


def test_an_open_urgent_decision_stays_open_and_on_the_awaiting_list_while_decisions_are_off(home, pulse):
    from assistant import decisions
    urgent, _ = decisions.open_decision(event=event("u"), lane="escalate", policy_id="r",
                                        now=time.time() - 30 * 86400)
    cards = pulse.run_triage_step(1)["cards"]
    assert [c["key"] for c in cards] == [urgent["id"]]
    assert decisions.fold(decisions.read_log())[urgent["id"]]["status"] == "open"


def test_decisions_on_run_the_triage_step(home, pulse):
    from assistant import triage
    write_config(home, {"jobs": {"decisions": True}})
    with mock.patch.object(triage, "triage_new_events", return_value={"cards": ["x"]}) as lane:
        assert pulse.run_triage_step(1) == {"cards": ["x"]}
    lane.assert_called_once()


def test_with_the_brief_off_no_file_is_written_and_nothing_expires_unseen(home, pulse, monkeypatch):
    from assistant import brief
    monkeypatch.setenv("ASSISTANT_BRIEF_NARRATOR", "1")
    # pulse_step returns early before the wake hour, so pin it to 0: the guard
    # in the pulse is then the only thing that stops the brief at any hour.
    with mock.patch.object(brief, "wake_hour", return_value=0), \
            mock.patch.object(brief, "degrade_unseen") as degrade, \
            mock.patch.object(brief, "build_brief") as build, \
            mock.patch.object(importlib.util, "spec_from_file_location") as narrator:
        pulse.run_brief_step(1)
        pulse.run_narrator_step(1)
    degrade.assert_not_called()
    build.assert_not_called()
    narrator.assert_not_called()
    assert not list((home / ".assistant/brief").glob("*"))


def test_with_the_brief_on_the_step_builds_it(home, pulse):
    from assistant import brief
    write_config(home, {"jobs": {"brief": True}})
    with mock.patch.object(brief, "pulse_step", return_value={"built": True}) as step:
        pulse.run_brief_step(1)
    step.assert_called_once()


def planner_rows(home: Path) -> list[dict]:
    path = home / ".assistant/actions-ledger.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
    return [r for r in rows if r.get("kind") == "planner-skip"]


def test_with_the_planner_off_no_planner_rows_reach_the_ledger(home, pulse):
    (home / ".claude/assistant-goals.json").write_text(json.dumps({"_paused": True, "goals": []}))
    pulse.run_planner_step(1)
    assert planner_rows(home) == []


def test_with_the_planner_on_a_skip_is_ledgered(home, pulse):
    (home / ".claude/assistant-goals.json").write_text(json.dumps({"_paused": True, "goals": []}))
    write_config(home, {"jobs": {"planner": True}})
    pulse.run_planner_step(1)
    assert "planner:paused" in [r["key"] for r in planner_rows(home)]


# ─── the lesson extractor ────────────────────────────────────────────────────

def test_a_ledger_only_run_does_not_start_the_extractor_while_the_pass_is_off(pulse, monkeypatch):
    monkeypatch.setenv("ASSISTANT_LESSON_EXTRACTOR", "1")
    with mock.patch.object(pulse, "run", side_effect=AssertionError("must not start")):
        pulse.run_lesson_extractor(12)


def test_the_transcript_pass_still_runs_while_the_ledger_pass_is_off(pulse, monkeypatch):
    monkeypatch.setenv("ASSISTANT_LESSON_EXTRACTOR", "1")
    with mock.patch.object(pulse, "run", return_value=(0, "{}", "")) as run:
        pulse.run_lesson_extractor(pulse.LESSON_TRANSCRIPT_EVERY)
    assert "--ledger-only" not in run.call_args[0][0]


def test_the_ledger_pass_runs_ledger_only_when_turned_on(pulse, monkeypatch):
    monkeypatch.setenv("ASSISTANT_LESSON_EXTRACTOR", "1")
    monkeypatch.setenv("ASSISTANT_LESSON_LEDGER_PASS", "1")
    with mock.patch.object(pulse, "run", return_value=(0, "{}", "")) as run:
        pulse.run_lesson_extractor(12)
    assert "--ledger-only" in run.call_args[0][0]


@pytest.fixture
def extractor(home, tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("lesson_extractor_jobs_off", REPO / "bin/lesson-extractor.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "AUDIT_LOG", tmp_path / "audit.log")
    monkeypatch.setattr(module, "existing_triggers", lambda *a, **k: [])
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text("".join(json.dumps({
        "ts": "2026-06-06T00:00:00Z", "epoch": 1000 + i, "kind": "stranded",
        "ws_ref": f"workspace:{i}", "outcome": "verified",
        "evidence": "nudged idle agent to resume its task"}) + "\n" for i in range(4)))
    return module, ledger


def run_extract(extractor, tmp_path, **kwargs):
    module, ledger = extractor
    llm = lambda prompt: json.dumps({"trigger": "t", "rule": "r", "target": "assistant", "scope": "general"})  # noqa: E731
    return module.extract(ledger_only=True, llm=llm, ledger_path=ledger,
                          proposals_path=tmp_path / "proposals.jsonl", now=2000,
                          curator=tmp_path / "curator.py", **kwargs)


def test_repeated_successful_actions_propose_nothing_by_default(extractor, tmp_path):
    result = run_extract(extractor, tmp_path)
    assert result["n_proposed"] == 0 and result["n_entries"] == 0
    assert not (tmp_path / "proposals.jsonl").exists()


def test_the_variable_turns_the_ledger_pass_on(extractor, tmp_path, monkeypatch):
    monkeypatch.setenv("ASSISTANT_LESSON_LEDGER_PASS", "1")
    assert run_extract(extractor, tmp_path)["n_proposed"] == 1


def test_the_ledger_pass_can_be_asked_for_directly(extractor, tmp_path):
    assert run_extract(extractor, tmp_path, ledger_pass=True)["n_proposed"] == 1


# ─── the dashboard ───────────────────────────────────────────────────────────

def world(home: Path) -> None:
    (home / ".claude/cache").mkdir(exist_ok=True)
    (home / ".claude/cache/world.json").write_text(json.dumps({"counts": {}, "_meta": {}}))


def turn_on_json(job: str) -> str:
    return (f'add <code>"jobs": {{"{job}": true}}</code> to <code>~/.assistant/config.json</code>, '
            'next to <code>"mode"</code>. Write <code>true</code> without quotes. '
            "It takes effect at Assistant's next check")


def test_settings_list_the_three_jobs_as_off_with_the_json_to_add(renderer):
    html = renderer.render_job_settings()
    for job in ("decisions", "brief", "planner"):
        assert f'data-job="{job}" data-state="off"' in html
        assert turn_on_json(job) in html
    assert "<strong>Decisions are off.</strong> Assistant no longer turns GitHub and session events into a decision list." in html
    assert "<strong>The morning brief is off.</strong> Assistant no longer saves a brief each morning." in html
    assert ("<strong>The goals planner is off.</strong> Assistant no longer plans next steps for your goals "
            "or records their progress.") in html


def test_no_dashboard_copy_mentions_a_variable_or_the_pulse(home, renderer):
    write_config(home, {"jobs": {"decisions": True, "brief": "yes"}})
    html = renderer.render_job_settings() + renderer.render_brief_tab()[0]
    for word in ("ASSISTANT_", "pulse", "Pulse", "triage", "PRO_JOBS"):
        assert word not in html


def test_settings_say_what_to_expect_when_a_job_is_on(home, renderer):
    write_config(home, {"jobs": {"decisions": True, "brief": True, "planner": True}})
    html = renderer.render_job_settings()
    assert 'data-job="decisions" data-state="on"' in html
    assert "<strong>Decisions are on.</strong> New ones appear from Assistant's next check." in html
    assert ("<strong>The morning brief is on.</strong> A new one is saved each morning, "
            "at the first check after 7 AM.") in html
    assert "<strong>The goals planner is on.</strong> It plans a next step for a stalled goal" in html
    assert 'data-state="off"' not in html


def test_the_brief_line_names_the_wake_hour(home, renderer):
    (home / ".assistant/comms").mkdir()
    (home / ".assistant/comms/config.json").write_text(json.dumps({"brief": {"wake_hour": 13}}))
    write_config(home, {"jobs": {"brief": True}})
    assert "at the first check after 1 PM." in renderer.render_job_settings()


@pytest.mark.parametrize("value, shown", [("true", "&quot;true&quot;"), (1, "1"), ("yes", "&quot;yes&quot;")])
def test_settings_say_when_a_value_is_present_but_ignored(home, renderer, value, shown):
    write_config(home, {"jobs": {"brief": value}})
    html = renderer.render_job_settings()
    assert 'data-job="brief" data-state="off" data-ignored="true"' in html
    assert (f'<code>"brief"</code> is set to <code>{shown}</code> in <code>~/.assistant/config.json</code>. '
            "Only <code>true</code> or <code>false</code> without quotes is read, "
            "so the morning brief is off.") in html
    assert "Change it" not in html
    assert html.count("data-ignored") == 1


@pytest.mark.parametrize("config", [{"jobs": {"brief": True}}, {"jobs": {"brief": False}}, {}])
def test_settings_do_not_mention_an_ignored_value_for_true_false_or_absent(home, renderer, config):
    write_config(home, config)
    assert "data-ignored" not in renderer.render_job_settings()


def test_the_settings_panel_includes_the_job_lines(home, renderer):
    world(home)
    renderer.render()
    assert 'class="job-setting" data-job="planner"' in renderer.DASHBOARD_HTML.read_text()


@pytest.mark.real_mode
def test_default_mode_settings_do_not_list_the_pro_only_jobs(home, renderer):
    """Setting jobs.decisions in default mode would do nothing, so the page doesn't offer it there."""
    world(home)
    write_config(home, {"mode": "default"})
    renderer.render()
    assert 'class="job-setting"' not in renderer.DASHBOARD_HTML.read_text()


def make_decision_log(home: Path, *rows) -> None:
    path = home / ".assistant/decisions/decisions.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps({"schema": "decision/1", **row}) + "\n" for row in rows))


OPEN_ROW = {"id": "dec-1", "status": "open", "title": "Review the parser change",
            "lane": "escalate", "source": "github"}


def test_the_notifications_tab_says_decisions_are_off_in_place_of_the_empty_list(home, renderer):
    make_decision_log(home)
    html, count = renderer.render_brief_tab()
    assert count == 0
    assert ("<strong>Decisions are off.</strong> Nothing new is listed here, and Completed actions and "
            "Other updates get no new entries. How to turn them on is under Settings and background services."
            ) in html
    assert "jobs" not in html.split('data-job="decisions"')[1].split("</p>")[0]
    assert "No open notifications in the current log" not in html


def test_the_notifications_tab_says_decisions_are_off_when_no_log_was_ever_written(renderer):
    html, _ = renderer.render_brief_tab()
    assert 'class="job-off" data-job="decisions"' in html
    assert "unavailable" not in html.split('data-job="decisions"')[1].split("</section>")[0][:900]


def test_an_unreadable_log_keeps_its_own_message_beside_the_notice(home, renderer):
    (home / ".assistant/decisions/decisions.jsonl").mkdir(parents=True)
    html, _ = renderer.render_brief_tab()
    assert "The current list is unavailable" in html
    assert 'class="job-off" data-job="decisions"' in html


def test_open_decisions_keep_rendering_under_a_notice_while_decisions_are_off(home, renderer):
    make_decision_log(home, {**OPEN_ROW, "epoch": int(time.time()) - 60})
    html, count = renderer.render_brief_tab()
    assert count == 1 and "Review the parser change" in html
    notice = ("<strong>Decisions are off.</strong> No new ones are being created; these were made earlier. "
              "How to turn them on is under Settings and background services.")
    assert notice in html
    assert html.index(notice) < html.index("Review the parser change")


def test_the_notifications_tab_has_no_decisions_notice_when_they_are_on(home, renderer):
    make_decision_log(home, {**OPEN_ROW, "epoch": int(time.time()) - 60})
    write_config(home, {"jobs": {"decisions": True, "brief": True}})
    html, _ = renderer.render_brief_tab()
    assert 'data-job="decisions"' not in html and "Review the parser change" in html
    make_decision_log(home)
    assert "No open notifications in the current log." in renderer.render_brief_tab()[0]


def test_the_brief_off_notice_shows_even_when_briefs_are_saved(home, renderer):
    (home / ".assistant/brief").mkdir()
    (home / ".assistant/brief/brief-2026-11-01.json").write_text("{}")
    html, _ = renderer.render_brief_tab()
    assert ("<strong>The morning brief is off.</strong> This tab is a live summary of your open "
            "notifications; no copy is saved each morning. How to turn it on is under Settings and "
            "background services.") in html
    assert '"jobs"' not in html.split('data-job="brief"')[1].split("</p>")[0]


def test_with_the_brief_off_the_tab_does_not_say_one_was_built_saved_or_unseen(home, renderer):
    (home / ".assistant/brief").mkdir()
    (home / ".assistant/brief/brief-2026-11-01.json").write_text('{"queue": []}')
    html, _ = renderer.render_brief_tab()
    assert "Live summary · updated" in html
    assert "Brief · built" not in html and "Brief · saved" not in html
    assert "No copy is saved each morning." in html
    assert "unseen" not in html and "Expired unseen" not in html
    assert "data-brief-date" not in html


def test_with_the_brief_on_the_tab_keeps_its_original_wording(home, renderer):
    write_config(home, {"jobs": {"brief": True, "decisions": True}})
    (home / ".assistant/brief").mkdir()
    (home / ".assistant/brief/brief-2026-11-01.json").write_text('{"queue": []}')
    html, _ = renderer.render_brief_tab()
    assert "Expired unseen" in html
    assert "Brief · built" in html and "unseen — viewing this tab records it." in html
    assert 'data-brief-date="2026-11-01"' in html
    assert 'data-job="brief"' not in html


def test_the_saved_brief_wording_is_not_used_for_the_off_state_either(home, renderer, monkeypatch):
    monkeypatch.setattr(renderer.brief_store, "build_brief", mock.Mock(side_effect=OSError("x")))
    (home / ".assistant/brief").mkdir()
    (home / ".assistant/brief/brief-2026-11-01.json").write_text('{"ts": "2026-11-01T07:00:00Z", "queue": []}')
    html, _ = renderer.render_brief_tab()
    assert "Last saved brief · 2026-11-01" in html
    assert "Last saved brief from 2026-11-01T07:00:00Z." in html


def test_the_decisions_panel_keeps_its_empty_line_and_adds_no_claim_when_nothing_is_open(home, renderer):
    html, count = renderer.render_decisions({})
    assert count == 0 and "No decisions in the last 24h." in html
    assert "Decisions are off" not in html


def test_the_decisions_panel_points_to_open_ones_when_decisions_are_off(home, renderer):
    make_decision_log(home, {**OPEN_ROW, "epoch": int(time.time()) - 60})
    html, _ = renderer.render_decisions({})
    assert "No decisions in the last 24h." in html
    assert "<strong>Decisions are off.</strong> Older ones still open are under Awaiting your input." in html
    assert "so none are listed" not in html


def test_the_decisions_panel_has_no_notice_when_they_are_on(home, renderer):
    make_decision_log(home, {**OPEN_ROW, "epoch": int(time.time()) - 60})
    write_config(home, {"jobs": {"decisions": True}})
    html, _ = renderer.render_decisions({})
    assert "No decisions in the last 24h." in html and "Decisions are off" not in html


def test_an_unreadable_log_adds_no_notice_to_the_decisions_panel(home, renderer):
    (home / ".assistant/decisions/decisions.jsonl").mkdir(parents=True)
    assert "Decisions are off" not in renderer.render_decisions({})[0]


def test_with_the_brief_off_and_nothing_saved_or_built_the_page_says_so_without_a_dangling_date(home, renderer, monkeypatch):
    monkeypatch.setattr(renderer.brief_store, "build_brief", mock.Mock(side_effect=OSError("x")))
    html, _ = renderer.render_brief_tab()
    assert "Live summary unavailable" in html
    assert "Last saved brief" not in html and "from ?" not in html and "· </div>" not in html
    assert '<div class="brief-footer">Live summary unavailable. No copy is saved each morning.</div>' in html


# ─── docs that promise these features ────────────────────────────────────────

@pytest.mark.parametrize("path", ["README.md", "docs/dashboard.md", "docs/configuration.md", "skills/goal/SKILL.md"])
def test_docs_say_these_jobs_are_off_by_default(path):
    text = (REPO / path).read_text()
    assert "off by default" in text or "off until you turn" in text
    assert "ASSISTANT_DECISIONS" not in text and "ASSISTANT_PLANNER" not in text
