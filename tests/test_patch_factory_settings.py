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


def test_patch_settings_preserves_unrelated_values(tmp_path):
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
    assert result["sessionDefaultSettings"]["model"] == "glm-5.2"
    assert result["sessionDefaultSettings"]["autonomyLevel"] == "high"
    assert list(tmp_path.glob("settings.json.bak-*"))
    assert module.patch_settings(target, source) is False
