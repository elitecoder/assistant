"""Decisions, the morning brief, and the goals planner are off until someone
turns them on, and so is the lesson extractor's pass over repeated actions.

conftest's `_watcher_on` fixture forces Pro mode and turns these jobs on for
other tests; each test here clears the variables to see the defaults.
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

JOB_VARS = ("ASSISTANT_DECISIONS", "ASSISTANT_BRIEF", "ASSISTANT_PLANNER")


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".assistant").mkdir()
    (tmp_path / ".claude").mkdir()
    for var in (*JOB_VARS, "ASSISTANT_LESSON_LEDGER_PASS"):
        monkeypatch.delenv(var, raising=False)
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


@pytest.mark.parametrize("name, var", [("decisions", "ASSISTANT_DECISIONS"),
                                       ("brief", "ASSISTANT_BRIEF"),
                                       ("planner", "ASSISTANT_PLANNER")])
def test_the_variable_turns_each_job_on(home, monkeypatch, name, var):
    monkeypatch.setenv(var, "1")
    assert mode.job_settings()[name] is True
    assert sum(mode.job_settings().values()) == 1
    monkeypatch.setenv(var, "0")
    assert mode.job_settings()[name] is False


@pytest.mark.parametrize("value", ["true", "yes", 1, "1", [True], {"enabled": True}, None, False, 0])
def test_only_the_json_true_counts(home, value):
    write_config(home, {"jobs": {"decisions": value, "brief": value, "planner": value}})
    assert not any(mode.job_settings().values())


@pytest.mark.parametrize("config", ['{"jobs": true}', '{"jobs": ["decisions"]}', '{"jobs": null}',
                                    '{"mode": "pro"}', "{torn", "[]"])
def test_a_wrong_shape_or_unreadable_config_leaves_every_job_off(home, config):
    write_config(home, config)
    assert not any(mode.job_settings().values())


def test_the_variable_still_works_when_the_config_is_unreadable(home, monkeypatch):
    write_config(home, "{torn")
    monkeypatch.setenv("ASSISTANT_BRIEF", "on")
    assert mode.job_settings() == {"decisions": False, "brief": True, "planner": False}


# ─── the pulse ───────────────────────────────────────────────────────────────

def test_pro_mode_alone_runs_none_of_the_three_jobs(pulse, monkeypatch):
    monkeypatch.setenv("ASSISTANT_STRATEGIST", "1")
    assert not any(pulse.job_enabled(job) for job in ("triage", "brief", "planner"))
    assert pulse.job_enabled("strategist")


def test_config_and_variables_turn_the_jobs_on_in_pro_mode(home, pulse, monkeypatch):
    write_config(home, {"jobs": {"decisions": True, "planner": True}})
    assert pulse.job_enabled("triage") and pulse.job_enabled("planner")
    assert not pulse.job_enabled("brief")
    monkeypatch.setenv("ASSISTANT_BRIEF", "1")
    assert pulse.job_enabled("brief")


def test_default_mode_runs_none_of_them_even_when_turned_on(home, pulse, monkeypatch):
    monkeypatch.setattr(mode, "current", lambda: "default")
    write_config(home, {"jobs": {"decisions": True, "brief": True, "planner": True}})
    monkeypatch.setenv("ASSISTANT_BRIEF_NARRATOR", "1")
    assert not any(pulse.job_enabled(job) for job in ("triage", "brief", "planner", "narrator"))


def test_the_narrator_needs_the_brief(home, pulse, monkeypatch):
    monkeypatch.setenv("ASSISTANT_BRIEF_NARRATOR", "1")
    assert not pulse.job_enabled("narrator")
    monkeypatch.setenv("ASSISTANT_BRIEF", "1")
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


def test_with_decisions_off_no_event_becomes_a_decision_and_the_cursor_skips(home, pulse):
    from assistant import triage
    events = seed_events(5)
    with mock.patch.object(triage, "triage_new_events") as lane:
        assert pulse.run_triage_step(1) == {"cards": []}
    lane.assert_not_called()
    assert json.loads(triage.cursor_path().read_text())["offset"] == events.stat().st_size


def test_turning_decisions_on_later_replays_nothing(home, pulse):
    from assistant import triage
    seed_events(5)
    pulse.run_triage_step(1)
    assert triage.scan_new_events()[0] == []
    write_config(home, {"jobs": {"decisions": True}})
    seed_events(1)
    new, _ = triage.scan_new_events()
    assert len(new) == 1


def test_open_decisions_still_expire_while_decisions_are_off(home, pulse):
    from assistant import decisions
    with mock.patch.object(decisions, "expire_open", return_value=[]) as expire:
        pulse.run_triage_step(1)
    expire.assert_called_once()


def test_decisions_on_run_the_triage_step(home, pulse):
    from assistant import triage
    write_config(home, {"jobs": {"decisions": True}})
    with mock.patch.object(triage, "triage_new_events", return_value={"cards": ["x"]}) as lane:
        assert pulse.run_triage_step(1) == {"cards": ["x"]}
    lane.assert_called_once()


def test_with_the_brief_off_no_file_is_written_and_nothing_expires_unseen(home, pulse, monkeypatch):
    from assistant import brief
    monkeypatch.setenv("ASSISTANT_BRIEF_NARRATOR", "1")
    with mock.patch.object(brief, "degrade_unseen") as degrade, \
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

def test_settings_list_the_three_jobs_as_off_with_the_setting(renderer):
    html = renderer.render_job_settings()
    for job, var in (("decisions", "ASSISTANT_DECISIONS"), ("brief", "ASSISTANT_BRIEF"),
                     ("planner", "ASSISTANT_PLANNER")):
        assert f'data-job="{job}" data-state="off"' in html
        assert f"<code>jobs.{job}</code>" in html and f"{var}=1" in html
    assert "Decisions are off." in html
    assert "The morning brief is off." in html
    assert "The goals planner is off." in html
    assert "~/.assistant/config.json" in html


def test_settings_say_a_job_is_on_when_config_turns_it_on(home, renderer):
    write_config(home, {"jobs": {"brief": True}})
    html = renderer.render_job_settings()
    assert 'data-job="brief" data-state="on"' in html
    assert "The morning brief is on." in html
    assert 'data-job="decisions" data-state="off"' in html


def test_the_settings_panel_includes_the_job_lines(home, renderer):
    (home / ".claude/cache").mkdir()
    (home / ".claude/cache/world.json").write_text(json.dumps({"counts": {}, "_meta": {}}))
    renderer.render()
    html = renderer.DASHBOARD_HTML.read_text()
    assert 'class="job-setting" data-job="planner"' in html


def make_decision_log(home: Path, *rows) -> None:
    path = home / ".assistant/decisions/decisions.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps({"schema": "decision/1", **row}) + "\n" for row in rows))


def test_the_notifications_tab_says_decisions_are_off_in_place_of_the_empty_list(home, renderer):
    make_decision_log(home)
    html, count = renderer.render_brief_tab()
    assert count == 0
    assert 'class="job-off" data-job="decisions"' in html
    assert "<strong>Decisions are off.</strong> Assistant no longer turns GitHub and session events into a decision list." in html
    assert "To turn them on, set <code>jobs.decisions</code> to <code>true</code>" in html
    assert "No open notifications in the current log" not in html


def test_the_notifications_tab_says_decisions_are_off_when_no_log_was_ever_written(renderer):
    html, _ = renderer.render_brief_tab()
    assert 'class="job-off" data-job="decisions"' in html


def test_an_unreadable_log_is_not_called_a_job_that_is_off(home, renderer):
    path = home / ".assistant/decisions/decisions.jsonl"
    path.mkdir(parents=True)
    html, _ = renderer.render_brief_tab()
    assert 'data-job="decisions"' not in html
    assert "The current list is unavailable" in html


def test_the_notifications_tab_says_the_brief_is_off_until_one_is_saved(home, renderer):
    html, _ = renderer.render_brief_tab()
    assert 'class="job-off" data-job="brief"' in html
    assert "<strong>The morning brief is off.</strong> Assistant no longer saves a brief each morning." in html
    (home / ".assistant/brief").mkdir()
    (home / ".assistant/brief/brief-2026-11-01.json").write_text("{}")
    assert 'data-job="brief"' not in renderer.render_brief_tab()[0]


def test_the_notifications_tab_is_plain_when_the_jobs_are_on(home, renderer):
    make_decision_log(home)
    write_config(home, {"jobs": {"decisions": True, "brief": True}})
    html, _ = renderer.render_brief_tab()
    assert "job-off" not in html
    assert "No open notifications in the current log." in html


def test_open_decisions_keep_rendering_while_decisions_are_off(home, renderer):
    make_decision_log(home, {
        "id": "dec-1", "status": "open", "title": "Review the parser change",
        "epoch": int(time.time()) - 60, "lane": "escalate", "source": "github"})
    html, count = renderer.render_brief_tab()
    assert count == 1 and "Review the parser change" in html
    assert 'data-job="decisions"' not in html
