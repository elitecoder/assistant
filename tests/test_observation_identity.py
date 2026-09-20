"""Exercise observation identity through real context and summary subprocesses."""
from __future__ import annotations

import ast
import copy
import importlib.util
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
BIN = REPO / "bin"
WORKSPACE_ID = "A1B2C3D4-1111-4111-8111-111111111111"
SURFACE_ID = "E5F6A7B8-2222-4222-8222-222222222222"
SESSION_ID = "abcd1234-3333-4333-8333-333333333333"
OTHER_ID = "abcd1234-4444-4444-8444-444444444444"
VERDICT = {"verdict": "active", "summary": "Observed task", "next": "Continue."}
FAKE_CMUX = """#!/usr/bin/env python3
import json
import os
import sys
import time
from pathlib import Path

home = Path(os.environ["HOME"])
fixture = json.loads((home / "cmux-fixture.json").read_text())
args = sys.argv[1:]
counter = home / "tree-reads"
reads = int(counter.read_text()) if counter.exists() else 0
if args == ["--id-format", "both", "tree", "--all", "--json"]:
    if reads == 0:
        (home / "first-read-at").write_text(str(time.time()))
    reads += 1
    counter.write_text(str(reads))
    tree = fixture["tree"]
    ws = tree["windows"][0]["workspaces"][0]
    if reads > 1 and fixture.get("replace_workspace"):
        ws["id"] = fixture["replace_workspace"]
    if reads > 1 and fixture.get("replace_surface"):
        ws["panes"][0]["surfaces"][0]["id"] = fixture["replace_surface"]
    print(json.dumps(tree))
elif args[0] == "list-panes":
    print("pane:1")
elif "list-pane-surfaces" in args:
    print("surface:7 " + fixture["surface_id"])
elif args[0] == "read-screen":
    print(fixture["screen"])
elif args[:3] == ["surface", "resume", "show"]:
    binding = fixture["binding"]
    if reads > 1 and fixture.get("replace_session"):
        binding["checkpointId"] = fixture["replace_session"]
    print(json.dumps({"binding": binding}))
else:
    sys.exit(1)
"""


@pytest.fixture(scope="session")
def cmux_fixture(tmp_path_factory):
    cmux = tmp_path_factory.getbasetemp() / "cmux-fixture"
    cmux.write_text(FAKE_CMUX.replace("#!/usr/bin/env python3", f"#!{sys.executable}"))
    cmux.chmod(0o755)
    return cmux


@pytest.fixture
def observed_home(tmp_path, cmux_fixture):
    home = tmp_path / "home"
    home.mkdir()
    env = {
        key: os.environ[key]
        for key in ("PATH", "TMPDIR", "COVERAGE_PROCESS_CONFIG")
        if key in os.environ
    }
    env.update({"HOME": str(home), "CMUX_BIN": str(cmux_fixture),
                "ASSISTANT_DIR": str(home / ".assistant")})
    return home, env


def write_fixture(home, *, provider="claude", **changes):
    fixture = {
        "tree": {"windows": [{"workspaces": [{
            "ref": "workspace:7", "id": WORKSPACE_ID,
            "panes": [{"surfaces": [{
                "ref": "surface:7", "id": SURFACE_ID, "type": "terminal",
            }]}],
        }]}]},
        "surface_id": SURFACE_ID,
        "binding": {"kind": "factory" if provider == "droid" else provider,
                    "checkpointId": SESSION_ID},
        "screen": ("Skills (2)\n? for help" if provider == "droid"
                   else "Claude Code v1\ncontext 10% │ #abcd1234"),
        **changes,
    }
    (home / "cmux-fixture.json").write_text(json.dumps(fixture))
    root = home / (".factory/sessions" if provider == "droid" else ".claude/projects")
    project = root / "fixture-project"
    project.mkdir(parents=True)
    transcript = project / f"{SESSION_ID}.jsonl"
    transcript.write_text(json.dumps(
        {"type": "session_start", "id": SESSION_ID} if provider == "droid"
        else {"sessionId": SESSION_ID}) + "\n")
    return transcript


def build_context(env):
    result = subprocess.run(
        [sys.executable, str(BIN / "build-ws-context.py"),
         "--ws-ref", "workspace:7", "--title", "Same reused title", "--cwd", ""],
        env=env, capture_output=True, text=True, timeout=90, check=True,
    )
    return json.loads(result.stdout)


def save(env, identity=None, verdict=None, observation_complete=None):
    args = [sys.executable, str(BIN / "save-ws-summary.py"),
            "--ws-ref", "workspace:7", "--json", json.dumps(verdict or VERDICT)]
    if identity is not None:
        args.extend(["--observation-json", json.dumps(identity)])
    if observation_complete is not None:
        args.extend(["--observation-complete", json.dumps(observation_complete)])
    return subprocess.run(args, env=env, capture_output=True, text=True, timeout=60)


def saved_summary(home):
    return json.loads((home / ".assistant/observer-summaries/workspace_7.json").read_text())


def expected_identity(provider="claude", observed_at=1700000000.25):
    return {
        "ws_ref": "workspace:7", "workspace_id": WORKSPACE_ID,
        "observed_at": observed_at,
        "observed_sessions": [
            {"surface_id": SURFACE_ID, "provider": provider, "session_id": SESSION_ID},
        ],
    }


@pytest.fixture
def context_module(observed_home, monkeypatch):
    home, _env = observed_home
    monkeypatch.setenv("HOME", str(home))
    spec = importlib.util.spec_from_file_location("identity_edge_context", BIN / "build-ws-context.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("payload", ["{broken", "[]", "null", '{"windows": []}'])
def test_identity_snapshot_rejects_unreadable_or_missing_workspace(context_module, monkeypatch, payload):
    monkeypatch.setattr(context_module, "_cmux", lambda _args: subprocess.CompletedProcess(
        ["cmux"], 0, stdout=payload, stderr=""))
    assert context_module.workspace_identity_snapshot("workspace:7") is None


def test_identity_snapshot_rejects_missing_workspace_uuid(context_module, monkeypatch):
    payload = {"windows": [{"workspaces": [{"ref": "workspace:7"}]}]}
    monkeypatch.setattr(context_module, "_cmux", lambda _args: subprocess.CompletedProcess(
        ["cmux"], 0, stdout=json.dumps(payload), stderr=""))
    assert context_module.workspace_identity_snapshot("workspace:7") is None


def test_identity_snapshot_ignores_browser_and_unidentified_surfaces(context_module, monkeypatch):
    payload = {"windows": [{"workspaces": [{
        "ref": "workspace:7", "id": WORKSPACE_ID,
        "panes": [{"surfaces": [
            {"ref": "surface:1", "id": "browser-id", "type": "browser"},
            {"ref": "surface:2", "type": "terminal"},
            {"id": "missing-ref", "type": "terminal"},
        ]}],
    }]}]}
    calls = []

    def cmux(args):
        calls.append(args)
        return subprocess.CompletedProcess(["cmux"], 0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr(context_module, "_cmux", cmux)
    assert context_module.workspace_identity_snapshot("workspace:7") == {
        "workspace_id": WORKSPACE_ID, "surfaces": {}}
    assert len(calls) == 1


@pytest.mark.parametrize("change", ["surface-id", "status-prefix"])
def test_resolved_pane_must_agree_with_observed_identity(context_module, change):
    snapshot = {"workspace_id": WORKSPACE_ID, "surfaces": {
        "surface:7": {"surface_id": SURFACE_ID,
                      "binding": {"agent": "claude", "session_id": SESSION_ID}}}}
    resolved = {"agent_surface": "surface:7", "agent_surface_id": SURFACE_ID,
                "agent_provider": "claude", "session_id8": SESSION_ID[:8],
                "transcript_path": None}
    if change == "surface-id":
        resolved["agent_surface_id"] = "other-surface"
    else:
        resolved["session_id8"] = "deadbeef"
    result = context_module.observation_identity(
        "workspace:7", snapshot, copy.deepcopy(snapshot), resolved, 1000)
    assert result == {"ws_ref": "workspace:7", "workspace_id": None,
                      "observed_sessions": [], "observed_at": 1000}


def test_unknown_provider_binding_is_not_treated_as_claude(context_module, monkeypatch):
    monkeypatch.setattr(context_module, "_cmux", lambda _args: subprocess.CompletedProcess(
        ["cmux"], 0, stdout=json.dumps({"kind": "other-agent", "checkpointId": SESSION_ID}), stderr=""))
    assert context_module.resume_binding_for_surface("surface:7", "workspace:7") is None


@pytest.mark.parametrize("identity", [
    [],
    {**expected_identity(), "workspace_id": ""},
    {**expected_identity(), "observed_sessions": {}},
    {**expected_identity(), "observed_sessions": [None]},
    {**expected_identity(), "observed_sessions": [{"surface_id": SURFACE_ID}]},
    {**expected_identity(), "workspace_id": None},
])
def test_summary_rejects_invalid_identity_shapes_without_saving(observed_home, identity):
    home, env = observed_home
    result = save(env, identity)
    assert result.returncode == 2
    assert "--observation-json invalid" in result.stderr
    assert not (home / ".assistant/observer-summaries/workspace_7.json").exists()


@pytest.mark.parametrize("provider", ["claude", "droid"])
def test_observed_full_identity_survives_save_after_workspace_recycling(observed_home, provider):
    home, env = observed_home
    write_fixture(home, provider=provider)
    context = build_context(env)
    identity = context["observation_identity"]
    assert identity == expected_identity(provider, identity["observed_at"])
    fixture_path = home / "cmux-fixture.json"
    fixture = json.loads(fixture_path.read_text())
    fixture["tree"]["windows"][0]["workspaces"][0]["id"] = OTHER_ID
    fixture["binding"]["checkpointId"] = OTHER_ID
    fixture_path.write_text(json.dumps(fixture))
    reads_before = (home / "tree-reads").read_text()
    assert save(env, context["observation_identity"]).returncode == 0
    summary = saved_summary(home)
    assert summary["workspace_id"] == WORKSPACE_ID
    assert summary["observed_sessions"] == expected_identity(provider)["observed_sessions"]
    assert summary["observed_at"] == identity["observed_at"]
    assert (home / "tree-reads").read_text() == reads_before


@pytest.mark.parametrize("change", ["replace_workspace", "replace_surface", "replace_session"])
def test_recycling_during_observation_drops_identity(observed_home, change):
    home, env = observed_home
    write_fixture(home, **{change: OTHER_ID})
    identity = build_context(env)["observation_identity"]
    assert identity == {"ws_ref": "workspace:7", "workspace_id": None,
                        "observed_sessions": [], "observed_at": identity["observed_at"]}


def test_uuid_case_changes_preserve_observed_identity(observed_home):
    home, env = observed_home
    write_fixture(home, replace_workspace=WORKSPACE_ID.lower(),
                  replace_surface=SURFACE_ID.lower())
    identity = build_context(env)["observation_identity"]
    assert identity == expected_identity(observed_at=identity["observed_at"])


def test_provider_session_case_changes_drop_observed_identity(observed_home):
    home, env = observed_home
    write_fixture(home, replace_session=SESSION_ID.upper())
    assert build_context(env)["observation_identity"]["observed_sessions"] == []


def test_full_session_mismatch_is_not_hidden_by_shared_prefix(observed_home):
    home, env = observed_home
    transcript = write_fixture(home)
    transcript.write_text(json.dumps({"sessionId": OTHER_ID}) + "\n")
    assert build_context(env)["observation_identity"]["workspace_id"] is None


def test_provider_mismatch_does_not_borrow_another_providers_transcript(observed_home):
    home, env = observed_home
    write_fixture(home, provider="droid", screen="Claude Code v1\ncontext 10% │ #abcd1234")
    assert build_context(env)["observation_identity"]["observed_sessions"] == []


def test_unknown_binding_provider_remains_unverified(observed_home):
    home, env = observed_home
    write_fixture(home, binding={"kind": "unknown", "checkpointId": SESSION_ID})
    assert build_context(env)["observation_identity"]["workspace_id"] is None


def test_missing_cmux_is_unverified_even_with_matching_transcript(observed_home):
    home, env = observed_home
    write_fixture(home)
    env["CMUX_BIN"] = str(home / "no-cmux")
    identity = build_context(env)["observation_identity"]
    assert identity["workspace_id"] is None
    assert identity["observed_sessions"] == []


def test_binding_preserves_full_identity_without_a_transcript(observed_home):
    home, env = observed_home
    write_fixture(home, provider="droid",
                  binding={"kind": "factory", "checkpointId": OTHER_ID})
    context = build_context(env)
    assert context["transcript_path"] is None
    assert context["observation_identity"]["observed_sessions"] == [
        {"surface_id": SURFACE_ID, "provider": "droid", "session_id": OTHER_ID},
    ]


def test_legacy_save_ignores_identity_claimed_by_verdict(observed_home):
    home, env = observed_home
    assert save(env, verdict={**VERDICT, **expected_identity(),
                              "observation_complete": True}).returncode == 0
    summary = saved_summary(home)
    assert summary["summary"] == VERDICT["summary"]
    assert summary["workspace_id"] is None
    assert summary["observed_sessions"] == []
    assert summary["observation_complete"] is None
    assert summary["observed_at"] is None


def test_observed_at_is_captured_before_first_cmux_read(observed_home):
    home, env = observed_home
    write_fixture(home)
    identity = build_context(env)["observation_identity"]
    assert 0 < identity["observed_at"] <= float((home / "first-read-at").read_text())


def test_save_does_not_refresh_observed_at(observed_home):
    home, env = observed_home
    identity = expected_identity()
    assert save(env, identity).returncode == 0
    first = saved_summary(home)
    assert save(env, identity).returncode == 0
    second = saved_summary(home)
    assert first["observed_at"] == second["observed_at"] == identity["observed_at"]
    assert second["last_updated_ts"] > second["observed_at"]


@pytest.mark.parametrize("observed_at", [None, 12, 12.5])
def test_save_accepts_positive_epoch_or_unknown_observed_at(observed_home, observed_at):
    home, env = observed_home
    assert save(env, expected_identity(observed_at=observed_at)).returncode == 0
    assert saved_summary(home)["observed_at"] == observed_at


@pytest.mark.parametrize("observed_at", [
    0, -1, True, False, "123", [], {}, float("nan"), float("inf"), float("-inf"),
    pytest.param(10 ** 400, id="overflow"),
])
def test_save_rejects_invalid_observed_at(observed_home, observed_at):
    home, env = observed_home
    result = save(env, expected_identity(observed_at=observed_at))
    assert result.returncode == 2
    assert "--observation-json invalid" in result.stderr
    assert not (home / ".assistant/observer-summaries/workspace_7.json").exists()


def test_save_rejects_observation_for_another_reference(observed_home):
    home, env = observed_home
    identity = {**expected_identity(), "ws_ref": "workspace:99"}
    result = save(env, identity)
    assert result.returncode == 2
    assert "reference differs" in result.stderr
    assert not (home / ".assistant/observer-summaries/workspace_7.json").exists()


def test_state_age_does_not_transfer_to_a_new_session(observed_home):
    home, env = observed_home
    assert save(env, expected_identity()).returncode == 0
    summary_path = home / ".assistant/observer-summaries/workspace_7.json"
    summary = saved_summary(home)
    summary["state_unchanged_since_ts"] = 1
    summary_path.write_text(json.dumps(summary))
    identity = expected_identity()
    identity["observed_sessions"][0]["session_id"] = OTHER_ID
    assert save(env, identity).returncode == 0
    assert saved_summary(home)["state_unchanged_since_ts"] > 1


@pytest.mark.parametrize("change_session_case", [False, True])
def test_state_age_compares_uuid_case_but_not_session_case(observed_home, change_session_case):
    home, env = observed_home
    assert save(env, expected_identity()).returncode == 0
    summary_path = home / ".assistant/observer-summaries/workspace_7.json"
    summary = saved_summary(home)
    summary["state_unchanged_since_ts"] = 1
    summary_path.write_text(json.dumps(summary))
    identity = expected_identity()
    identity["workspace_id"] = WORKSPACE_ID.lower()
    identity["observed_sessions"][0]["surface_id"] = SURFACE_ID.lower()
    if change_session_case:
        identity["observed_sessions"][0]["session_id"] = SESSION_ID.upper()
    assert save(env, identity).returncode == 0
    unchanged = saved_summary(home)["state_unchanged_since_ts"]
    assert (unchanged > 1) if change_session_case else (unchanged == 1)


def test_save_retains_provider_namespaces_with_identical_session_ids(observed_home):
    home, env = observed_home
    identity = expected_identity()
    identity["observed_sessions"].append(
        {"surface_id": OTHER_ID, "provider": "droid", "session_id": SESSION_ID})
    assert save(env, identity).returncode == 0
    assert saved_summary(home)["observed_sessions"] == identity["observed_sessions"]


def test_pulse_save_helper_passes_observed_envelope_without_running_pulse(observed_home):
    home, env = observed_home
    write_fixture(home)
    identity = build_context(env)["observation_identity"]
    tree = ast.parse((BIN / "pulse.py").read_text())
    function = next(node for node in tree.body
                    if isinstance(node, ast.FunctionDef) and node.name == "save_summary")
    calls = []

    def run(command):
        calls.append(command)
        result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=60)
        return result.returncode, result.stdout, result.stderr

    namespace = {"sys": sys, "json": json, "BIN": BIN, "run": run,
                 "log": logging.getLogger(__name__)}
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(BIN / "pulse.py"), "exec"),
         namespace)
    namespace["save_summary"](
        {"ref": "workspace:7", "workspace_id": OTHER_ID}, VERDICT, identity)
    args = calls[0]
    assert json.loads(args[args.index("--observation-json") + 1]) == identity
    assert saved_summary(home)["workspace_id"] == WORKSPACE_ID


@pytest.mark.parametrize("verdict_name, prior_complete, expected_complete", [
    ("v", True, True),
    ("v", False, False),
    ("v", None, None),
    ("v", "missing", None),
    ("synth", True, False),
    ("v_for_save", False, True),
])
@pytest.mark.parametrize("prior_observed_at", [1700000000.25, None, "missing"])
def test_pulse_save_call_sites_keep_original_observation(
        observed_home, verdict_name, prior_complete, expected_complete, prior_observed_at):
    home, env = observed_home
    tree = ast.parse((BIN / "pulse.py").read_text())
    call = next(node for node in ast.walk(tree)
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "save_summary"
                and isinstance(node.args[1], ast.Name) and node.args[1].id == verdict_name)
    original = expected_identity(
        observed_at=None if prior_observed_at == "missing" else prior_observed_at)
    current = {**original, "workspace_id": OTHER_ID, "observed_at": 1800000000.5}
    captured = []
    verdict = {**VERDICT, **original}
    if prior_observed_at == "missing":
        verdict.pop("observed_at")
    if prior_complete != "missing":
        verdict["observation_complete"] = prior_complete

    def run(command):
        captured.append(command)
        result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=60)
        assert result.returncode == 0, result.stderr
        return result.returncode, result.stdout, result.stderr

    namespace = {
        "sys": sys, "json": json, "BIN": BIN, "run": run,
        "log": logging.getLogger(__name__),
        "ws": {"ref": "workspace:7", "workspace_id": OTHER_ID},
        "ws_ref": "workspace:7",
        "ctx": {"observation_identity": current if verdict_name == "v" else original},
        verdict_name: verdict,
    }
    function = next(node for node in tree.body
                    if isinstance(node, ast.FunctionDef) and node.name == "save_summary")
    exec(compile(ast.fix_missing_locations(ast.Module(
        body=[function, ast.Expr(value=call)], type_ignores=[])),
        str(BIN / "pulse.py"), "exec"), namespace)
    assert json.loads(captured[0][captured[0].index("--observation-json") + 1]) == original
    assert saved_summary(home)["workspace_id"] == WORKSPACE_ID
    assert saved_summary(home)["observation_complete"] is expected_complete
    assert saved_summary(home)["observed_at"] == original["observed_at"]
