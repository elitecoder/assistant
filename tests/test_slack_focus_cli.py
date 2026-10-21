"""Tests for bin/slack-focus.py — the Slack tab's refresh runner. The fake
Slack workspace, a fake gh, and a fake model stand in for every outside call;
HOME is a tmp dir, so ~/.assistant is never touched."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from assistant.slack_focus import store, triage
from slack_focus_fixtures import CONFIG, FakeSlack, write_config

BIN = Path(__file__).resolve().parent.parent / "bin"


def _load():
    spec = importlib.util.spec_from_file_location("slack_focus_cli", str(BIN / "slack-focus.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cli = _load()


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(cli, "rerender", lambda: None)
    write_config(tmp_path, CONFIG)
    return tmp_path


def offline_resolver(home):
    def run(cmd, **kwargs):
        if cmd[1:3] == ["pr", "list"]:
            return subprocess.CompletedProcess(cmd, 0, "[]", "")
        return subprocess.CompletedProcess(cmd, 0, json.dumps({"state": "OPEN"}), "")

    from assistant.slack_focus import settings

    return triage.Resolver(settings.load(home), run=run, env={})


def model_keep_first(prompt):
    return json.dumps([{"id": 0, "lane": "Do first", "title": "Answer Aalia", "why": "she asked", "effort": "Quick"}])


def test_full_refresh_writes_every_file(home):
    focus = cli.refresh(home, pull=True, use_judge=True, token="xoxp-t", transport=FakeSlack(),
                        model=model_keep_first, resolver=offline_resolver(home))
    root = store.base_dir(home)
    for name in ("threads.json", "mine.json", "focus.json", "judge-cache.json"):
        assert (root / name).exists(), name
    assert [i["text"] for i in focus["active"]] == ["Answer Aalia"]
    assert store.read_json(root / "focus.json") == focus
    assert store.read_state(home)["phase"] == "Asking the model which threads need you"


def test_triage_only_refresh_reuses_the_last_pull(home):
    cli.refresh(home, pull=True, use_judge=False, token="t", transport=FakeSlack(), resolver=offline_resolver(home))
    calls = FakeSlack()
    focus = cli.refresh(home, pull=False, use_judge=False, token="", transport=calls,
                        resolver=offline_resolver(home))
    assert calls.calls == [] and "judge" not in focus and focus["active"]


def test_refresh_explains_missing_setup(home):
    (home / ".assistant/comms/config.json").write_text("{}")
    with pytest.raises(RuntimeError, match="slack_focus block"):
        cli.refresh(home, pull=True, use_judge=False, token="t")
    write_config(home, CONFIG)
    with pytest.raises(RuntimeError, match="no Slack pull"):
        cli.refresh(home, pull=False, use_judge=False, token="t")


def test_main_records_success_and_failure(home, monkeypatch):
    renders = []
    monkeypatch.setattr(cli, "rerender", lambda: renders.append(store.read_state(home)["running"]))
    monkeypatch.setattr(cli, "refresh", lambda h, **kw: {"counts": {"active": 1}, "kw": kw})
    assert cli.main([]) == 0
    state = store.read_state(home)
    assert (state["running"], state["ok"], state["error"], state["phase"]) == (False, True, None, None)
    assert state["pid"] == os.getpid() and state["finished"] and renders == [False]

    def boom(h, **kw):
        raise cli.slack_client.SlackError("Slack refused the token: invalid_auth")

    monkeypatch.setattr(cli, "refresh", boom)
    assert cli.main(["--triage", "--no-judge"]) == 1
    state = store.read_state(home)
    assert (state["ok"], state["error"]) == (False, "Slack refused the token: invalid_auth")

    monkeypatch.setattr(cli, "refresh", lambda h, **kw: (_ for _ in ()).throw(KeyError()))
    assert cli.main([]) == 1 and store.read_state(home)["error"] == "KeyError"


def test_main_passes_flags_and_token(home, monkeypatch):
    seen = {}
    monkeypatch.setenv("SLACK_USER_TOKEN", "xoxp-env")
    monkeypatch.setattr(cli, "refresh", lambda h, **kw: seen.update(kw) or {"counts": {}})
    cli.main(["--triage", "--no-judge"])
    assert seen == {"pull": False, "use_judge": False, "token": "xoxp-env"}


def test_main_skips_while_another_refresh_runs(home, capsys):
    with store.locked(store.base_dir(home) / "refresh.lock"):
        assert cli.main([]) == 0
    assert "already running" in capsys.readouterr().err
    assert store.read_state(home) == {}


def test_call_model_runs_tools_off_and_meters(home, monkeypatch):
    seen = {}

    def fake_run(cmd, *, input_text=None, timeout=30, merge_bedrock=False):
        seen.update(cmd=cmd, prompt=input_text, timeout=timeout)
        return 0, json.dumps({"type": "result", "subtype": "success", "result": "[1]",
                              "usage": {"input_tokens": 10, "output_tokens": 2}, "total_cost_usd": 0.01}), ""

    monkeypatch.setattr(cli, "run", fake_run)
    assert cli.call_model("judge these") == "[1]"
    assert "--tools" in seen["cmd"] and "--strict-mcp-config" in seen["cmd"]
    assert seen["prompt"] == "judge these" and seen["timeout"] == cli.JUDGE_TIMEOUT_SEC
    row = json.loads((home / ".assistant/cost-ledger.jsonl").read_text().splitlines()[-1])
    assert (row["caller"], row["status"], row["est_usd"]) == ("slack_focus", "ok", 0.01)


def test_call_model_failure_returns_nothing(home, monkeypatch, capsys):
    monkeypatch.setattr(cli, "run", lambda cmd, **kw: (1, "", "auth failed"))
    assert cli.call_model("x") == ""
    assert "rc=1" in capsys.readouterr().err
    row = json.loads((home / ".assistant/cost-ledger.jsonl").read_text().splitlines()[-1])
    assert row["status"] == "failed"

    def broken_meter(**kw):
        raise OSError("disk full")

    monkeypatch.setattr(cli.metering, "append_cost_row", broken_meter)
    assert cli.call_model("x") == ""
    assert "metering failed" in capsys.readouterr().err


def test_run_returns_output_and_kills_on_timeout(home, tmp_path):
    (home / ".zprofile").write_text('export AWS_REGION="us-west-2"\n')
    rc, out, err = cli.run([sys.executable, "-c", "import os,sys; print(os.environ.get('AWS_REGION'), sys.stdin.read())"],
                           input_text="hi", merge_bedrock=True)
    assert (rc, out.strip()) == (0, "us-west-2 hi")
    rc, out, err = cli.run([sys.executable, "-c", "import time; time.sleep(30)"], timeout=1)
    assert rc == 124 and "timeout after 1s" in err
    assert cli.run([str(tmp_path / "missing-binary")])[0] == 127


def test_run_falls_back_to_kill_when_the_group_is_gone(home, monkeypatch):
    def no_group(pid):
        raise ProcessLookupError()

    monkeypatch.setattr(cli.os, "getpgid", no_group)
    assert cli.run([sys.executable, "-c", "import time; time.sleep(30)"], timeout=1)[0] == 124


def test_rerender_runs_the_dashboard_renderer(monkeypatch):
    calls = []
    monkeypatch.setattr(cli.subprocess, "run", lambda cmd, **kw: calls.append((cmd, kw)))
    cli.rerender()
    assert calls[0][0] == [sys.executable, str(BIN / "render-assistant-page.py")]
    assert calls[0][1]["check"] is False


def test_run_gives_up_on_output_after_the_kill(home, monkeypatch):
    class Stuck:
        pid = 1

        def __init__(self, *args, **kwargs):
            self.calls = 0

        def communicate(self, input=None, timeout=None):
            raise subprocess.TimeoutExpired("cmd", timeout)

        def kill(self):
            pass

    monkeypatch.setattr(cli.subprocess, "Popen", Stuck)
    monkeypatch.setattr(cli.os, "getpgid", lambda pid: (_ for _ in ()).throw(ProcessLookupError()))
    assert cli.run(["x"], timeout=1) == (124, "", "\ntimeout after 1s")


def test_script_entry_point_reports_missing_setup(tmp_path):
    env = {**os.environ, "HOME": str(tmp_path)}
    done = subprocess.run([sys.executable, str(BIN / "slack-focus.py"), "--triage", "--no-judge"],
                          capture_output=True, text=True, env=env, timeout=60)
    assert done.returncode == 1 and "slack_focus block" in done.stderr
    assert store.read_state(tmp_path)["error"].startswith("Add a slack_focus block")


def test_main_stops_a_refresh_that_runs_too_long(home, monkeypatch):
    monkeypatch.setattr(cli, "REFRESH_TIMEOUT_SEC", 1)
    monkeypatch.setattr(cli, "refresh", lambda h, **kw: __import__("time").sleep(5))
    assert cli.main([]) == 1
    assert store.read_state(home)["error"] == "The refresh took longer than 0 minutes."
