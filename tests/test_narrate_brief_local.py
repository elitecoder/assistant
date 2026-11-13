import importlib.util
import json
import os
from pathlib import Path
from unittest import mock

from local_llm_server import FakeLocalModel

REPO = Path(__file__).resolve().parents[1]


def test_the_narrator_sends_its_prompt_to_the_configured_local_model(tmp_path):
    with FakeLocalModel(reply='{"summary": "quiet day"}') as server:
        config = tmp_path / ".assistant/comms/config.json"
        config.parent.mkdir(parents=True)
        config.write_text(json.dumps({"llm": {"provider": "local", "local": {
            "url": server.url, "model": "gpt-oss-120b", "reasoning_effort": "off"}}}))
        with mock.patch.dict(os.environ, {"HOME": str(tmp_path)}):
            spec = importlib.util.spec_from_file_location("narrate_local", REPO / "bin/narrate-brief.py")
            cli = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(cli)
            cli._spawn("Narrate this.", tmp_path / "run", "out.json")
    body = server.requests[0]["body"]
    assert body["model"] == "gpt-oss-120b"
    assert body["messages"][0]["content"] == "Narrate this."
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert (tmp_path / "run/out.json").read_text() == '{"summary": "quiet day"}'
