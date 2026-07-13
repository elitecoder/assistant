import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace


REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "evals/observer/run.py"


def load_module():
    spec = importlib.util.spec_from_file_location("observer_eval", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_extract_verdict_from_jsonl():
    module = load_module()
    verdict = module.extract_verdict(
        '{"ws_ref":"workspace:1","verdict":"active"}\n')
    assert verdict["verdict"] == "active"


def test_eval_prompt_repeats_complete_response_schema(tmp_path, monkeypatch):
    module = load_module()
    prompt = tmp_path / "observer.md"
    prompt.write_text("Base prompt.")
    monkeypatch.setattr(module, "OBSERVER_PROMPT", prompt)

    text = module.build_eval_prompt(
        tmp_path, {"ws_ref": "workspace:1"})

    assert "`ws_ref`, `verdict`, `summary`, and `next`" in text
    assert "`needs_user` verdict additionally requires" in text
    assert "`title` and `detail`" in text
    assert "`stranded` verdict additionally requires" in text
    assert "`nudge_text`" in text
    assert text.rstrip().endswith(
        "Do not write output files or add prose.")


def test_stage_transcript_keeps_first_and_last_records(tmp_path):
    module = load_module()
    source = tmp_path / "source.jsonl"
    destination = tmp_path / "staged.jsonl"
    lines = [json.dumps({"n": index, "text": "x" * 40})
             for index in range(20)]
    source.write_text("\n".join(lines) + "\n")
    module.stage_transcript(source, destination, max_bytes=300)
    staged = [json.loads(line) for line in destination.read_text().splitlines()]
    assert staged[0]["n"] == 0
    assert staged[-1]["n"] == 19
    assert destination.stat().st_size <= 300


def test_production_observer_is_single_workspace_isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    pulse_path = REPO / "bin/pulse.py"
    spec = importlib.util.spec_from_file_location("pulse_eval_contract", pulse_path)
    pulse = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pulse)
    assert pulse.WS_BATCH_SIZE == 1


def test_run_one_scores_provider_result(tmp_path, monkeypatch):
    module = load_module()
    fixture = tmp_path / "case"
    fixture.mkdir()
    (fixture / "ctx.json").write_text(json.dumps({"cwd": str(fixture)}))
    (fixture / "transcript.jsonl").write_text("{}\n")
    (fixture / "expected.json").write_text(json.dumps({
        "verdict": "active",
        "forbidden_verdicts": ["ready_for_cleanup"],
    }))
    prompt = tmp_path / "observer.md"
    prompt.write_text("Return Observer JSONL.")
    monkeypatch.setattr(module, "OBSERVER_PROMPT", prompt)
    monkeypatch.setattr(module.llm_runner, "invoke", lambda **kwargs: SimpleNamespace(
        rc=0,
        stderr="",
        usable=True,
        session_id="droid-session",
        tokens_in=10,
        tokens_out=5,
        result_text=json.dumps({
            "ws_ref": "workspace:eval-case",
            "verdict": "active",
            "summary": "Work continues.",
            "next": "The agent validates the change.",
        }),
    ))
    ok, _, details = module.run_one(fixture, "droid")
    assert ok is True
    assert details["passed"] is True
    assert details["provider"] == "droid"
