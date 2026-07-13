"""Tests for the outbound action-class gate registry (Keel M7.a)."""
from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
from assistant import action_classes  # noqa: E402


class ActionClassesTests(unittest.TestCase):
    def setUp(self):
        self._tmp_obj = TemporaryDirectory()
        self.home = Path(self._tmp_obj.name)
        self._old_home = os.environ.get("HOME")
        os.environ["HOME"] = str(self.home)

    def tearDown(self):
        if self._old_home is not None:
            os.environ["HOME"] = self._old_home
        else:
            os.environ.pop("HOME", None)
        self._tmp_obj.cleanup()

    def test_bootstrap_installs_from_repo(self):
        self.assertTrue(action_classes.ensure_action_classes_installed())
        doc = json.loads(action_classes.action_classes_path().read_text())
        self.assertEqual(doc["version"], 1)
        self.assertIn("email.send", doc["classes"])
        # idempotent second run
        self.assertFalse(action_classes.ensure_action_classes_installed())

    def test_gates_resolve_per_bootstrap(self):
        action_classes.ensure_action_classes_installed()
        self.assertEqual(action_classes.resolve_gate("email.draft"), "draft_only")
        self.assertEqual(action_classes.resolve_gate("slack.reply.draft"),
                         "draft_only")
        self.assertEqual(action_classes.resolve_gate("github.merge"), "confirm")
        self.assertEqual(action_classes.resolve_gate("todo.create"), "standing")
        self.assertEqual(action_classes.resolve_gate("ws.close"), "confirm")

    def test_send_classes_are_forbidden(self):
        action_classes.ensure_action_classes_installed()
        self.assertEqual(action_classes.resolve_gate("email.send"), "forbidden")
        self.assertEqual(action_classes.resolve_gate("slack.reply.send"),
                         "forbidden")

    def test_unknown_and_disabled_classes_resolve_none(self):
        action_classes.ensure_action_classes_installed()
        self.assertIsNone(action_classes.resolve_gate("made.up.class"))
        # disable a class in the live file → resolves None (refused)
        p = action_classes.action_classes_path()
        doc = json.loads(p.read_text())
        doc["classes"]["todo.create"]["enabled"] = False
        p.write_text(json.dumps(doc))
        self.assertIsNone(action_classes.resolve_gate("todo.create"))

    def test_config_cannot_un_forbid_a_send(self):
        # The load-bearing guarantee: even if the live config file is edited to
        # mark email.send as draft_only+enabled, resolve_gate STILL returns
        # forbidden (code-enforced), so a config flip cannot enable an auto-send.
        action_classes.ensure_action_classes_installed()
        p = action_classes.action_classes_path()
        doc = json.loads(p.read_text())
        doc["classes"]["email.send"] = {"gate": "draft_only", "enabled": True}
        p.write_text(json.dumps(doc))
        self.assertEqual(action_classes.resolve_gate("email.send"), "forbidden")

    def test_code_forbid_is_case_and_whitespace_insensitive(self):
        # A mis-cased or padded send class added to the config cannot un-forbid.
        action_classes.ensure_action_classes_installed()
        p = action_classes.action_classes_path()
        doc = json.loads(p.read_text())
        doc["classes"]["Email.Send"] = {"gate": "draft_only", "enabled": True}
        doc["classes"]["EMAIL.SEND"] = {"gate": "confirm", "enabled": True}
        p.write_text(json.dumps(doc))
        for variant in ("Email.Send", "EMAIL.SEND", "email.send ", " email.send"):
            self.assertEqual(action_classes.resolve_gate(variant), "forbidden",
                             variant)

    def test_any_send_verb_is_code_forbidden(self):
        # A brand-new send class (sms.send, webhook.send) cannot be enabled by
        # config alone — anything ending in `.send` is code-forbidden.
        action_classes.ensure_action_classes_installed()
        p = action_classes.action_classes_path()
        doc = json.loads(p.read_text())
        doc["classes"]["sms.send"] = {"gate": "draft_only", "enabled": True}
        p.write_text(json.dumps(doc))
        self.assertEqual(action_classes.resolve_gate("sms.send"), "forbidden")

    def test_config_cannot_downgrade_a_confirm_gate(self):
        # A config edit may make a class STRICTER, never looser: downgrading
        # github.merge/ws.close to `standing` (auto, no prompt) is clamped back
        # up to the code-pinned `confirm` floor.
        action_classes.ensure_action_classes_installed()
        p = action_classes.action_classes_path()
        doc = json.loads(p.read_text())
        doc["classes"]["github.merge"] = {"gate": "standing", "enabled": True}
        doc["classes"]["ws.close"] = {"gate": "draft_only", "enabled": True}
        p.write_text(json.dumps(doc))
        self.assertEqual(action_classes.resolve_gate("github.merge"), "confirm")
        self.assertEqual(action_classes.resolve_gate("ws.close"), "confirm")
        # …but a STRICTER override is honored.
        doc["classes"]["github.merge"] = {"gate": "forbidden", "enabled": True}
        p.write_text(json.dumps(doc))
        self.assertEqual(action_classes.resolve_gate("github.merge"), "forbidden")

    def test_missing_registry_fails_safe(self):
        # No live file, no install run → load falls back to the repo bootstrap
        # (never opens up); an unknown class is still refused and sends still
        # forbidden.
        self.assertEqual(action_classes.resolve_gate("email.send"), "forbidden")
        self.assertEqual(action_classes.resolve_gate("email.draft"), "draft_only")
        self.assertIsNone(action_classes.resolve_gate("made.up"))

    def test_additive_upgrade_preserves_operator_classes(self):
        # Live file at an older version, missing a bootstrap class and carrying
        # an operator class → upgrade ADDS the missing bootstrap class, bumps the
        # version, and never touches the operator's class or edits.
        p = action_classes.action_classes_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({
            "version": 0,
            "classes": {
                "email.send": {"gate": "forbidden", "enabled": True},
                "custom.op.class": {"gate": "standing", "enabled": True},
            },
        }))
        self.assertTrue(action_classes.ensure_action_classes_installed())
        doc = json.loads(p.read_text())
        self.assertEqual(doc["version"], 1)
        self.assertIn("email.draft", doc["classes"])          # added
        self.assertIn("custom.op.class", doc["classes"])      # preserved
        self.assertEqual(doc["classes"]["custom.op.class"]["gate"], "standing")


if __name__ == "__main__":
    unittest.main()
