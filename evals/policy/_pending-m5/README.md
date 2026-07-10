# Pending M5 fixtures — NOT active

These fixtures belonged to bootstrap rules for gmail/github event sources whose
producers do not exist until Keel M5. The rules were removed from
`config/policies.bootstrap.json` in the M2 adversarial review: a rule (worst of
all a case-sensitive `unsubscribe` DROP) written against a *guessed* schema is
a data-hiding hazard the moment a real producer ships with different field
shapes.

TODO(M5): when each connector lands, re-derive its rule against the REAL event
schema, move the fixture back to `evals/policy/<rule-id>/`, update it to match
the real payload, and add the rule to the bootstrap with a version bump.

The `_` prefix keeps this directory out of `tests/test_policy.py`'s
orphan-fixture gate; nothing here is loaded or evaluated.
