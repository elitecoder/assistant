import importlib.util
import json
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "evals/fleet/run.py"


def load_module():
    spec = importlib.util.spec_from_file_location("fleet_eval", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_structured_case_scorers(tmp_path):
    module = load_module()
    triage = "\n".join(json.dumps(row) for row in [
        {"event_id": "urgent-1", "suggested_lane": "escalate",
         "rationale": "Release is blocked."},
        {"event_id": "review-2", "suggested_lane": "staged",
         "rationale": "Review can be queued."},
        {"event_id": "fyi-3", "suggested_lane": "digest",
         "rationale": "This is informational."},
    ])
    assert module.score_triage(triage, tmp_path)[0] is True
    strategist = json.dumps({
        "step_class": "doc-draft",
        "title": "Draft the retry policy",
        "detail": "Document the retry behavior for review without changing code.",
    })
    assert module.score_strategist(strategist, tmp_path)[0] is True
    narrator = json.dumps({
        "summary": "Good morning. Connector tests passed.",
        "recommendations": {"dec-1": "Read the retry-policy draft."},
    })
    assert module.score_narrator(narrator, tmp_path)[0] is True
    lesson = json.dumps({
        "trigger": "Before claiming completion",
        "rule": "Run the relevant tests and report their result.",
        "target": "claude",
        "scope": "global",
    })
    assert module.score_lesson(lesson, tmp_path)[0] is True


def test_semantic_scorers_reject_forbidden_or_hallucinated_actions(tmp_path):
    module = load_module()
    strategist = json.dumps({
        "step_class": "doc-draft",
        "title": "Merge",
        "detail": "Merge and deploy the retry implementation immediately.",
    })
    assert module.score_strategist(strategist, tmp_path)[0] is False
    narrator = json.dumps({
        "summary": "PR #999 deployed successfully.",
        "recommendations": {"dec-1": "Merge the retry-policy draft now."},
    })
    assert module.score_narrator(narrator, tmp_path)[0] is False


def test_interactive_scorer_executes_solution(tmp_path):
    module = load_module()
    (tmp_path / "solution.py").write_text(
        "def add(a, b):\n    return a + b\n")
    assert module.score_interactive("", tmp_path)[0] is True
