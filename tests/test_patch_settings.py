from __future__ import annotations

import importlib.util
import json
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def load_module():
    spec = importlib.util.spec_from_file_location(
        "patch_settings_test", REPO / "install" / "patch-settings.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def commands(document, event):
    return [
        hook["command"]
        for block in document.get("hooks", {}).get(event, [])
        for hook in block.get("hooks", [])
    ]


def test_patches_claude_and_factory_without_deleting_existing_hooks(tmp_path):
    module = load_module()
    claude = tmp_path / ".claude" / "settings.json"
    factory = tmp_path / ".factory" / "hooks.json"
    claude.parent.mkdir()
    factory.parent.mkdir()
    claude.write_text(json.dumps({
        "permissions": {"allow": ["Read"]},
        "hooks": {
            "SessionStart": [{
                "matcher": "",
                "hooks": [{"type": "command", "command": "existing-claude"}],
            }],
            "Stop": [{
                "matcher": "",
                "hooks": [{
                    "type": "command",
                    "command": module.LEDGER_END_CMD,
                }],
            }],
        },
    }))
    factory.write_text(json.dumps({
        "custom": True,
        "hooks": {
            "PostToolUse": [{
                "matcher": "Edit",
                "hooks": [{"type": "command", "command": "existing-factory"}],
            }],
        },
    }))

    assert module.patch_path(claude)
    assert module.patch_path(factory)

    claude_doc = json.loads(claude.read_text())
    factory_doc = json.loads(factory.read_text())
    assert claude_doc["permissions"] == {"allow": ["Read"]}
    assert "existing-claude" in commands(claude_doc, "SessionStart")
    assert module.LEDGER_END_CMD not in commands(claude_doc, "Stop")
    assert factory_doc["custom"] is True
    assert "existing-factory" in commands(factory_doc, "PostToolUse")
    for document in (claude_doc, factory_doc):
        assert module.AUTO_RESUME_CMD in commands(document, "SessionStart")
        assert module.LEDGER_START_CMD in commands(document, "SessionStart")
        assert module.LEDGER_END_CMD in commands(document, "SessionEnd")

    assert len(list(claude.parent.glob("settings.json.bak-*"))) == 1
    assert len(list(factory.parent.glob("hooks.json.bak-*"))) == 1


def test_new_factory_hooks_file_has_no_backup_and_is_idempotent(tmp_path):
    module = load_module()
    path = tmp_path / ".factory" / "hooks.json"

    assert module.patch_path(path)
    first = path.read_text()
    assert not list(path.parent.glob("hooks.json.bak-*"))

    assert module.patch_path(path) is False
    assert path.read_text() == first
    assert not list(path.parent.glob("hooks.json.bak-*"))
