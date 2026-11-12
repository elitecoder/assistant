import importlib.util
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]


def _lane(tmp_path, *, checked_ago, turn_ago, watcher_fresh=True):
    now = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(seconds=1)
    identity = {"surface_id": "s1", "provider": "claude", "session_id": "x1"}
    turn = (now - turn_ago).isoformat()
    world = {
        "_meta": {"built_at": now.isoformat()}, "counts": {}, "todo": {},
        "workspaces": [{"ws_ref": "workspace:1", "title": "Task", "workspace_id": "w1",
                        "surfaces": [{"surface_id": "s1"}], "session_ids": ["x1"]}],
        "live_sessions": [{
            **identity, "ws_ref": "workspace:1", "workspace_id": "w1",
            "identity_status": "verified", "context_status": "verified",
            "pending_tool_use": False,
            "context_checked_at": (now - (timedelta(0) if watcher_fresh else timedelta(hours=1))).isoformat(),
            "last_user": {"ts": turn, "text": "go"},
            "last_assistant": {"ts": turn, "text": "Which option do you want?"}}],
    }
    summaries = tmp_path / ".assistant/observer-summaries"
    summaries.mkdir(parents=True)
    (summaries / "workspace_1.json").write_text(json.dumps({
        "ws_ref": "workspace:1", "verdict": "needs_user", "workspace_id": "w1",
        "observed_sessions": [identity], "observation_complete": True,
        "observed_at": (now - checked_ago).timestamp(), "ts": (now - checked_ago).timestamp(),
        "summary": "Waiting for a choice.", "next": "Pick an option.", "title": "Choose"}))
    with mock.patch.dict(os.environ, {"HOME": str(tmp_path)}):
        spec = importlib.util.spec_from_file_location("renderer", REPO / "bin/render-assistant-page.py")
        renderer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(renderer)
        cards, _, _ = renderer.overview_cards(world)
    return cards[0]["lane"]


def test_an_unchanged_session_keeps_its_hour_old_result(tmp_path):
    assert _lane(tmp_path, checked_ago=timedelta(hours=1), turn_ago=timedelta(hours=2)) == "needs-you"


def test_a_session_that_moved_on_drops_its_result(tmp_path):
    assert _lane(tmp_path, checked_ago=timedelta(hours=1), turn_ago=timedelta(minutes=5)) != "needs-you"


def test_a_result_older_than_six_hours_drops(tmp_path):
    assert _lane(tmp_path, checked_ago=timedelta(hours=7), turn_ago=timedelta(hours=8)) != "needs-you"


def test_an_old_result_drops_when_the_session_watcher_is_stale(tmp_path):
    assert _lane(tmp_path, checked_ago=timedelta(hours=1), turn_ago=timedelta(hours=2),
                 watcher_fresh=False) != "needs-you"
