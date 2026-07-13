import importlib.util
import json
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "install/patch-factory-settings.py"


def load_module():
    spec = importlib.util.spec_from_file_location(
        "patch_factory_settings", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_patch_settings_operator_wins_over_repo_defaults(tmp_path):
    # OPERATOR-WINS: repo defaults SEED missing keys but never override a value
    # the operator hand-set. A defaults-win merge would silently re-raise a
    # deliberately-lowered droid autonomy on every self-update.
    module = load_module()
    target = tmp_path / "settings.json"
    source = tmp_path / "glm.json"
    target.write_text(json.dumps({
        "enableDroidShield": True,
        "sessionDefaultSettings": {"autonomyLevel": "low", "other": "keep"},
    }))
    source.write_text(json.dumps({
        "sessionDefaultSettings": {
            "model": "glm-5.2",
            "autonomyLevel": "high",
            "reasoningEffort": "high",
        },
    }))
    assert module.patch_settings(target, source) is True
    result = json.loads(target.read_text())
    assert result["enableDroidShield"] is True
    assert result["sessionDefaultSettings"]["other"] == "keep"
    assert result["sessionDefaultSettings"]["model"] == "glm-5.2"       # filled
    assert result["sessionDefaultSettings"]["reasoningEffort"] == "high"  # filled
    assert result["sessionDefaultSettings"]["autonomyLevel"] == "low"   # PRESERVED
    assert list(tmp_path.glob("settings.json.bak-*"))
    assert module.patch_settings(target, source) is False


def test_patch_settings_seeds_defaults_on_fresh_box(tmp_path):
    module = load_module()
    target = tmp_path / "settings.json"  # absent
    source = tmp_path / "glm.json"
    source.write_text(json.dumps({
        "sessionDefaultSettings": {"model": "glm-5.2", "autonomyLevel": "high"},
    }))
    assert module.patch_settings(target, source) is True
    result = json.loads(target.read_text())
    assert result["sessionDefaultSettings"]["model"] == "glm-5.2"
    assert result["sessionDefaultSettings"]["autonomyLevel"] == "high"


def test_patch_settings_malformed_target_does_not_raise(tmp_path):
    # A corrupt settings.json must be backed up and treated as empty, NOT raise
    # (which would abort the installer under `set -euo pipefail`).
    module = load_module()
    target = tmp_path / "settings.json"
    source = tmp_path / "glm.json"
    target.write_text("{not valid json")
    source.write_text(json.dumps({
        "sessionDefaultSettings": {"model": "glm-5.2"},
    }))
    assert module.patch_settings(target, source) is True
    result = json.loads(target.read_text())
    assert result["sessionDefaultSettings"]["model"] == "glm-5.2"
    assert list(tmp_path.glob("settings.json.corrupt-*"))


def test_patch_settings_skips_symlinked_target(tmp_path):
    # Never write through a symlink (operator/machine-config managed target).
    module = load_module()
    real = tmp_path / "real-settings.json"
    real.write_text(json.dumps({"sessionDefaultSettings": {"autonomyLevel": "low"}}))
    target = tmp_path / "settings.json"
    target.symlink_to(real)
    source = tmp_path / "glm.json"
    source.write_text(json.dumps({
        "sessionDefaultSettings": {"model": "glm-5.2", "autonomyLevel": "high"},
    }))
    assert module.patch_settings(target, source) is False
    # The real target behind the symlink is untouched.
    assert json.loads(real.read_text())["sessionDefaultSettings"] == {"autonomyLevel": "low"}
