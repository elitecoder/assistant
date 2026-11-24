"""tests/mutation_smoke.py --shard i/n splits the checks so the shards together run every check exactly once,
and the quality workflow runs every shard and fails its required check unless all of them pass."""
from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path

import pytest

import mutation_smoke

REPO = Path(__file__).resolve().parents[1]
WORKFLOW = (REPO / ".github/workflows/quality.yml").read_text()


@pytest.mark.parametrize("n", [1, 2, 3, 4, 6])
def test_the_shards_together_run_every_check_exactly_once(n):
    shards = [mutation_smoke.shard(f"{i}/{n}") for i in range(1, n + 1)]
    seen = Counter(check for checks in shards for check in checks)
    assert set(seen) == set(mutation_smoke.CHECKS) and set(seen.values()) == {1}
    assert sum(map(len, shards)) == len(mutation_smoke.CHECKS)
    assert max(map(len, shards)) - min(map(len, shards)) <= 1


@pytest.mark.parametrize("spec", ["0/2", "3/2", "1", "a/b", "1/0", "-1/2", " 1/2"])
def test_a_shard_that_isnt_one_of_n_is_refused(spec):
    with pytest.raises(argparse.ArgumentTypeError, match="isn't a shard like 1/2"):
        mutation_smoke.shard(spec)


def test_a_shard_with_no_checks_is_refused_instead_of_passing():
    n = len(mutation_smoke.CHECKS) + 1
    with pytest.raises(argparse.ArgumentTypeError, match=f"shard {n}/{n} has no checks"):
        mutation_smoke.shard(f"{n}/{n}")
    with pytest.raises(SystemExit):
        mutation_smoke.main(["--shard", f"{n}/{n}", "--output", "/dev/null"])


def test_a_shard_run_checks_only_its_share_and_records_them(tmp_path, monkeypatch, capsys):
    ran = []
    monkeypatch.setattr(mutation_smoke, "run_check", lambda name, *rest: ran.append(name) or
                        {"check": name, "mutant": "rejected"})
    out = tmp_path / "shard.json"
    mutation_smoke.main(["--shard", "2/2", "--output", str(out)])
    assert ran == [c[0] for c in mutation_smoke.CHECKS[1::2]]
    assert json.loads(out.read_text()) == {"passed": True, "checks": [{"check": n, "mutant": "rejected"} for n in ran]}
    ran.clear()
    mutation_smoke.main(["--output", str(out)])
    assert ran == [c[0] for c in mutation_smoke.CHECKS]
    assert f"{len(ran)} targeted mutations rejected" in capsys.readouterr().out


def test_the_workflow_runs_every_shard_and_requires_them_all():
    shards = re.search(r"shard: \[([0-9, ]+)\]", WORKFLOW).group(1).split(", ")
    total = re.search(r'--shard "\$\{\{ matrix\.shard \}\}/([0-9]+)"', WORKFLOW).group(1)
    assert shards == [str(i) for i in range(1, int(total) + 1)]
    assert "fail-fast: false" in WORKFLOW
    gate = WORKFLOW[WORKFLOW.index("  quality-gate:"):]
    assert "name: Tests and 100% new-code coverage" in gate and WORKFLOW.count("name: Tests and 100% new-code coverage") == 1
    assert "if: always()" in gate and "needs: [changed-code-coverage, mutations]" in gate
    assert 'test "$COVERAGE" = success && test "$MUTATIONS" = success' in gate
    assert "COVERAGE: ${{ needs.changed-code-coverage.result }}" in gate and "MUTATIONS: ${{ needs.mutations.result }}" in gate
    assert "tests/mutation_smoke.py --output" not in WORKFLOW, "no job runs the checks unsharded as well"


def test_a_check_never_runs_in_a_folder_whose_path_has_a_double_underscore(monkeypatch, tmp_path):
    names = iter(["assistant-mutation-a__b", "assistant-mutation-__c", "assistant-mutation-ok"])
    made, cleaned = [], []

    class Folder:
        def __init__(self, prefix):
            self.name = str(tmp_path / next(names))
            made.append(self.name)

        def cleanup(self):
            cleaned.append(self.name)

    monkeypatch.setattr(mutation_smoke, "TemporaryDirectory", Folder)
    assert mutation_smoke.scratch().name == str(tmp_path / "assistant-mutation-ok")
    assert cleaned == made[:2], "each folder with __ in its name is removed before the next try"
