"""Refusal matrix + idempotency + ledger invariant for the gated outbound
dispatcher (Keel M7.b/c). Pure Python — no LLM, no network, no sends."""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
from assistant import decisions  # noqa: E402

ACCEPTED = "dec-aaaaaaaaaaaaaaaa"
OTHER = "dec-bbbbbbbbbbbbbbbb"


def load_dispatch():
    spec = importlib.util.spec_from_file_location(
        "outbound_dispatch_mod", REPO / "bin" / "outbound-dispatch.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class OutboundDispatchTests(unittest.TestCase):
    def setUp(self):
        self._tmp_obj = TemporaryDirectory()
        self.home = Path(self._tmp_obj.name)
        self._old_home = os.environ.get("HOME")
        os.environ["HOME"] = str(self.home)
        self.mod = load_dispatch()

    def tearDown(self):
        if self._old_home is not None:
            os.environ["HOME"] = self._old_home
        else:
            os.environ.pop("HOME", None)
        self._tmp_obj.cleanup()

    # ── helpers ──────────────────────────────────────────────────────────────
    def _seed_decision(self, dec_id, status, cls="email.draft"):
        # A decision is opened/accepted FOR a specific action class (recommended.
        # class); the dispatcher binds the requested class to it. cls=None models
        # a decision with no action (e.g. an escalate) — not dispatchable.
        p = decisions.decisions_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        now = time.time()
        rec = {"schema": decisions.SCHEMA, "id": dec_id, "status": status,
               "epoch": int(now), "ts": decisions.utc_iso(now)}
        if cls is not None:
            rec["recommended"] = {"class": cls, "summary": "x",
                                  "payload_path": None}
        with open(p, "a") as f:
            f.write(json.dumps(rec) + "\n")

    def _ledger(self):
        return self.mod.read_outbound_ledger()

    # ── Gate 1: authorization ────────────────────────────────────────────────
    def test_no_decision_refuses_and_writes_no_outbound_row(self):
        out, code = self.mod.dispatch(ACCEPTED, "email.draft")
        self.assertEqual(code, self.mod.EXIT_REFUSED)
        self.assertEqual(out["outcome"], "refused")
        self.assertFalse(out["gate1_authorization"]["ok"])
        # INVARIANT: a Gate-1 refusal (no accepted decision) writes NO outbound
        # row — the outbound ledger only ever holds rows with an accepted dec.
        self.assertEqual(self._ledger(), [])
        # …but it IS audited in the actions-ledger.
        al = (self.home / ".assistant/actions-ledger.jsonl")
        self.assertIn("outbound-refused", al.read_text())

    def test_open_decision_is_not_authorization(self):
        self._seed_decision(ACCEPTED, "open")
        out, code = self.mod.dispatch(ACCEPTED, "email.draft")
        self.assertEqual(code, self.mod.EXIT_REFUSED)
        self.assertFalse(out["gate1_authorization"]["ok"])
        self.assertEqual(self._ledger(), [])

    def test_rejected_decision_is_not_authorization(self):
        self._seed_decision(OTHER, "open")
        self._seed_decision(OTHER, "rejected")  # latest wins
        out, code = self.mod.dispatch(OTHER, "email.draft")
        self.assertEqual(code, self.mod.EXIT_REFUSED)
        self.assertFalse(out["gate1_authorization"]["ok"])

    # ── Gate 1: class↔acceptance binding (the confused-deputy fix) ───────────
    def test_accepting_one_class_does_not_authorize_another(self):
        # BLOCKER fix: a decision accepted FOR todo.create must NOT authorize a
        # dispatch of email.draft / github.merge / ws.close on the same id.
        self._seed_decision(ACCEPTED, "accepted", cls="todo.create")
        for other in ("email.draft", "github.merge", "ws.close"):
            out, code = self.mod.dispatch(ACCEPTED, other)
            self.assertEqual(code, self.mod.EXIT_REFUSED, other)
            self.assertFalse(out["gate1_authorization"]["ok"], other)
            self.assertEqual(out["gate1_authorization"]["accepted_class"],
                             "todo.create", other)
        # class-mismatch refers to another class's acceptance → NO outbound row,
        # audited in the actions-ledger instead.
        self.assertEqual(self._ledger(), [])
        self.assertIn("class-mismatch",
                      (self.home / ".assistant/actions-ledger.jsonl").read_text())

    def test_decision_with_no_action_is_not_dispatchable(self):
        self._seed_decision(ACCEPTED, "accepted", cls=None)  # e.g. an escalate
        out, code = self.mod.dispatch(ACCEPTED, "email.draft")
        self.assertEqual(code, self.mod.EXIT_REFUSED)
        self.assertIsNone(out["gate1_authorization"]["accepted_class"])

    # ── Gate 2: class registry ───────────────────────────────────────────────
    def test_named_forbidden_send_refuses_with_a_row(self):
        cases = {"dec-" + "c" * 16: "email.send",
                 "dec-" + "d" * 16: "slack.reply.send"}
        for dec, cls in cases.items():
            self._seed_decision(dec, "accepted", cls=cls)
            out, code = self.mod.dispatch(dec, cls)
            self.assertEqual(code, self.mod.EXIT_REFUSED, cls)
            self.assertEqual(out["gate2_registry"]["gate"], "forbidden", cls)
        rows = self._ledger()
        self.assertEqual(len(rows), len(cases))
        self.assertTrue(all(r["outcome"] == "refused" for r in rows))

    def test_unknown_class_refuses(self):
        self._seed_decision(ACCEPTED, "accepted", cls="made.up.class")
        out, code = self.mod.dispatch(ACCEPTED, "made.up.class")
        self.assertEqual(code, self.mod.EXIT_REFUSED)
        self.assertIsNone(out["gate2_registry"]["gate"])
        self.assertEqual(self._ledger()[0]["outcome"], "refused")

    def test_disabled_class_refuses(self):
        self._seed_decision(ACCEPTED, "accepted", cls="todo.create")
        from assistant import action_classes  # noqa: PLC0415
        action_classes.ensure_action_classes_installed()
        p = action_classes.action_classes_path()
        doc = json.loads(p.read_text())
        doc["classes"]["todo.create"]["enabled"] = False
        p.write_text(json.dumps(doc))
        out, code = self.mod.dispatch(ACCEPTED, "todo.create")
        self.assertEqual(code, self.mod.EXIT_REFUSED)

    # ── permitted (no handler wired in the a–d chokepoint) ──────────────────
    def test_permitted_class_is_unimplemented_and_writes_no_row(self):
        self._seed_decision(ACCEPTED, "accepted", cls="email.draft")
        out, code = self.mod.dispatch(ACCEPTED, "email.draft")
        self.assertEqual(code, self.mod.EXIT_USAGE)          # 3: not built yet
        self.assertEqual(out["outcome"], "unimplemented")
        self.assertTrue(out["gate1_authorization"]["ok"])
        self.assertEqual(out["gate2_registry"]["gate"], "draft_only")
        # No handler → no idempotency meaning → NO ledger row (retry-loop safe).
        self.assertEqual(self._ledger(), [])

    def test_edited_decision_is_authorized(self):
        self._seed_decision(ACCEPTED, "open", cls="todo.create")
        self._seed_decision(ACCEPTED, "edited", cls="todo.create")
        out, code = self.mod.dispatch(ACCEPTED, "todo.create")
        self.assertTrue(out["gate1_authorization"]["ok"])
        self.assertEqual(out["outcome"], "unimplemented")

    # ── M7.c: idempotency ───────────────────────────────────────────────────
    def test_replay_of_actioned_dec_class_refuses(self):
        self._seed_decision(ACCEPTED, "accepted", cls="email.draft")
        with self.mod._outbound_lock():   # a prior successful draft (future handler)
            self.mod._append_row(ACCEPTED, "email.draft", None, "drafted",
                                 "prior draft", time.time())
        out, code = self.mod.dispatch(ACCEPTED, "email.draft")
        self.assertEqual(code, self.mod.EXIT_REFUSED)
        self.assertFalse(out["idempotency"]["ok"])

    def test_refused_row_does_not_block_retry(self):
        # A prior REFUSED row must NOT count as actioned — only drafted/verified.
        self._seed_decision(ACCEPTED, "accepted", cls="email.draft")
        with self.mod._outbound_lock():
            self.mod._append_row(ACCEPTED, "email.draft", None, "refused",
                                 "some earlier refusal", time.time())
        out, code = self.mod.dispatch(ACCEPTED, "email.draft")
        self.assertTrue(out["idempotency"]["ok"])   # not blocked

    # ── usage ────────────────────────────────────────────────────────────────
    def test_bad_dec_id_is_usage_error(self):
        out, code = self.mod.dispatch("not-a-dec", "email.draft")
        self.assertEqual(code, self.mod.EXIT_USAGE)
        self.assertEqual(out["outcome"], "usage_error")
        self.assertEqual(self._ledger(), [])

    def test_ledger_invariant_every_row_has_a_matching_decision(self):
        # EVERY outbound-ledger row must reference a dec that is accepted/edited
        # AND accepted for that row's class.
        self._seed_decision(ACCEPTED, "accepted", cls="email.send")
        self._seed_decision(OTHER, "accepted", cls="email.draft")
        self.mod.dispatch(ACCEPTED, "email.send")   # forbidden → refused row
        self.mod.dispatch(OTHER, "email.draft")     # permitted → no row
        self.mod.dispatch("dec-" + "e" * 16, "email.draft")  # no dec → no row
        folded = decisions.fold(decisions.read_log())
        rows = self._ledger()
        self.assertTrue(rows)  # at least the forbidden refusal
        for row in rows:
            rec = folded.get(row["dec_id"])
            self.assertIsNotNone(rec, row)
            self.assertIn(rec.get("status"), ("accepted", "edited"), row)
            self.assertEqual((rec.get("recommended") or {}).get("class"),
                             row["class"], row)


if __name__ == "__main__":
    unittest.main()
