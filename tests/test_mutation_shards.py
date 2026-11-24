"""tests/mutation_smoke.py --shard i/n splits the checks so the shards together run every check exactly once,
and the quality workflow runs every shard and fails its required check unless all of them pass."""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
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


class Ran:
    def __init__(self, returncode=0):
        self.returncode, self.stdout, self.stderr = returncode, "1 failed" if returncode else "1 passed", ""


def test_a_tests_baseline_runs_once_however_many_checks_name_it(monkeypatch, tmp_path):
    runs, shared = [], mutation_smoke.Baselines()
    monkeypatch.setattr(mutation_smoke, "run_test", lambda repo, home, selector: runs.append(selector) or Ran())
    for name in ("first", "second", "third"):
        mutation_smoke.prove_baseline(name, tmp_path, tmp_path, "tests/a.py::one", shared)
    mutation_smoke.prove_baseline("other", tmp_path, tmp_path, "tests/a.py::two", shared)
    assert runs == ["tests/a.py::one", "tests/a.py::two"]


def test_without_a_shared_record_every_check_runs_its_own_baseline(monkeypatch, tmp_path):
    """run_check called on its own (a test, or someone proving one new check) remembers nothing between calls."""
    runs = []
    monkeypatch.setattr(mutation_smoke, "run_test", lambda repo, home, selector: runs.append(selector) or Ran())
    for name in ("first", "second"):
        mutation_smoke.prove_baseline(name, tmp_path, tmp_path, "tests/a.py::one")
    assert runs == ["tests/a.py::one"] * 2


def test_a_failed_baseline_stops_the_check_and_is_never_remembered(monkeypatch, tmp_path):
    answers, shared = iter([Ran(1), Ran(0)]), mutation_smoke.Baselines()
    monkeypatch.setattr(mutation_smoke, "run_test", lambda *args: next(answers))
    with pytest.raises(RuntimeError, match="first: baseline failed"):
        mutation_smoke.prove_baseline("first", tmp_path, tmp_path, "tests/a.py::one", shared)
    assert shared.passed == set(), "a later check must run this baseline again"
    mutation_smoke.prove_baseline("second", tmp_path, tmp_path, "tests/a.py::one", shared)
    assert shared.passed == {"tests/a.py::one"}


def test_a_baseline_that_never_finishes_stops_the_check_and_is_never_remembered(monkeypatch, tmp_path):
    def hang(*args):
        raise mutation_smoke.subprocess.TimeoutExpired("pytest", mutation_smoke.TEST_TIMEOUT_SEC)

    shared = mutation_smoke.Baselines()
    monkeypatch.setattr(mutation_smoke, "run_test", hang)
    with pytest.raises(RuntimeError, match="slow: baseline tests/a.py::one didn't finish within"):
        mutation_smoke.prove_baseline("slow", tmp_path, tmp_path, "tests/a.py::one", shared)
    assert shared.passed == set()


def test_two_checks_naming_one_test_at_once_run_its_baseline_once(monkeypatch, tmp_path):
    import threading

    runs, entered, release, shared = [], threading.Event(), threading.Event(), mutation_smoke.Baselines()

    def slow(repo, home, selector):
        runs.append(selector)
        entered.set()
        assert release.wait(5)
        return Ran()

    monkeypatch.setattr(mutation_smoke, "run_test", slow)
    workers = [threading.Thread(target=mutation_smoke.prove_baseline,
                                args=(n, tmp_path, tmp_path, "tests/a.py::one", shared)) for n in ("first", "second")]
    workers[0].start()
    assert entered.wait(5)
    workers[1].start()  # must wait for the first one's answer, not start a second baseline
    release.set()
    for worker in workers:
        worker.join(5)
    assert runs == ["tests/a.py::one"]


def test_a_check_still_runs_the_mutant_when_its_baseline_was_already_proved(monkeypatch, tmp_path):
    """The mutant run is the check itself; only the repeated baseline is skipped."""
    selector, runs, shared = "tests/a.py::one", [], mutation_smoke.Baselines()
    shared.passed.add(selector)

    def copy(destination):
        (destination / "module.py").write_text("x = 1\n")

    def run(repo, home, chosen):
        runs.append((repo / "module.py").read_text())
        failed = Ran(1)
        failed.stdout = f"FAILED {chosen} - assert 2 == 1"
        return failed

    monkeypatch.setattr(mutation_smoke, "copy_sources", copy)
    monkeypatch.setattr(mutation_smoke, "run_test", run)
    result = mutation_smoke.run_check("a check", "module.py", "x = 1\n", "x = 2\n", selector, shared)
    assert runs == ["x = 2\n"], "one run, on the mutated copy"
    assert result["mutant"] == "rejected" and result["failure"] == [f"FAILED {selector} - assert 2 == 1"]


def test_one_run_shares_one_record_of_baselines_between_its_checks(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(mutation_smoke, "run_check", lambda name, *rest: seen.append(rest[-1]) or
                        {"check": name, "mutant": "rejected"})
    monkeypatch.setattr(mutation_smoke, "CHECKS", mutation_smoke.CHECKS[:3])
    mutation_smoke.main(["--output", str(tmp_path / "a.json")])
    assert isinstance(seen[0], mutation_smoke.Baselines) and all(shared is seen[0] for shared in seen)
    mutation_smoke.main(["--output", str(tmp_path / "b.json")])
    assert seen[3] is not seen[0], "a new run starts with nothing remembered"


def test_several_workers_run_checks_at_once_and_keep_their_order(tmp_path, monkeypatch):
    import threading

    together = threading.Barrier(3, timeout=5)

    def check(name, *rest):
        together.wait()  # passes only when three checks are running at the same moment
        return {"check": name, "mutant": "rejected"}

    monkeypatch.setattr(mutation_smoke, "run_check", check)
    monkeypatch.setattr(mutation_smoke, "CHECKS", mutation_smoke.CHECKS[:6])
    out = tmp_path / "all.json"
    mutation_smoke.main(["--workers", "3", "--output", str(out)])
    assert [c["check"] for c in json.loads(out.read_text())["checks"]] == [c[0] for c in mutation_smoke.CHECKS]


def test_a_failed_check_fails_the_run_with_several_workers(tmp_path, monkeypatch):
    def check(name, *rest):
        if name == mutation_smoke.CHECKS[1][0]:
            raise RuntimeError(f"{name}: mutant was not rejected")
        return {"check": name, "mutant": "rejected"}

    monkeypatch.setattr(mutation_smoke, "run_check", check)
    monkeypatch.setattr(mutation_smoke, "CHECKS", mutation_smoke.CHECKS[:4])
    out = tmp_path / "all.json"
    with pytest.raises(RuntimeError, match="mutant was not rejected"):
        mutation_smoke.main(["--workers", "2", "--output", str(out)])
    assert not out.exists(), "no evidence file claims a pass"


@pytest.mark.parametrize("spec", ["0", "9", "3.5", "-1", "", "two"])
def test_a_worker_count_that_isnt_one_to_eight_is_refused(spec):
    with pytest.raises(argparse.ArgumentTypeError, match="isn't a number of workers from 1 to 8"):
        mutation_smoke.workers(spec)


def test_the_workflow_runs_as_many_jobs_as_the_account_runs_at_once():
    """Five macOS jobs run at a time here: the shards plus the coverage job must not exceed that, or jobs queue."""
    shards = re.search(r"shard: \[([0-9, ]+)\]", WORKFLOW).group(1).split(", ")
    assert len(shards) + 1 <= 5
    assert '--workers 3 \\' in WORKFLOW


# ─── checks kept one file per change, and the shape every check must have ─────

GOOD = ("name", "bin/pulse.py", "original\n", "broken\n", "tests/test_pulse.py::test_one")


def write_checks(folder: Path, name: str, body: str) -> None:
    folder.mkdir(exist_ok=True)
    (folder / name).write_text(body)


def test_checks_kept_one_file_per_change_join_the_list_in_file_name_order(tmp_path):
    write_checks(tmp_path, "b_second.py", "CHECKS = (('two', 'f', 'o', 'b', 't'),)\n")
    write_checks(tmp_path, "a_first.py", "CHECKS = (('one', 'f', 'o', 'b', 't'), ('one-b', 'f', 'o', 'b', 't'))\n")
    (tmp_path / "notes.txt").write_text("not a file of checks")
    assert [c[0] for c in mutation_smoke.checks_in(tmp_path)] == ["one", "one-b", "two"]


def test_a_folder_with_no_files_of_checks_adds_nothing(tmp_path):
    assert mutation_smoke.checks_in(tmp_path) == () and mutation_smoke.checks_in(tmp_path / "missing") == ()


@pytest.mark.parametrize("name", ["._mine.py", ".#mine.py", "__init__.py", "_helpers.py"])
def test_a_file_an_editor_or_macos_left_behind_is_not_read(tmp_path, name):
    write_checks(tmp_path, name, "this is not Python \x00")
    write_checks(tmp_path, "mine.py", "CHECKS = (('one', 'f', 'o', 'b', 't'),)\n")
    assert [c[0] for c in mutation_smoke.checks_in(tmp_path)] == ["one"]


@pytest.mark.parametrize("body, complaint", [
    ("CHECKS = (('one', 'f', 'o', 'b', 't'), ('two', 'f', 'o', 'b'))\n", "mine.py: mutation check 2 isn't"),
    ("CHECKS = (('one', 'f', 'o', 'b', 't'), ('one', 'g', 'o', 'b', 't'))\n", "mine.py: two mutation checks are named 'one'"),
    ("CHECKS = (('one', 'f', 'o', 'b', 't'))\n", "mine.py: mutation check 1 is a single string, not five. A list of "
                                                 "one check needs a comma after it"),
], ids=["a-short-entry", "a-repeated-name", "one-entry-with-no-comma"])
def test_a_mistake_in_a_file_of_checks_names_the_file_and_the_entry_within_it(tmp_path, body, complaint):
    write_checks(tmp_path, "earlier.py", "CHECKS = (('zero', 'f', 'o', 'b', 't'),)\n")
    write_checks(tmp_path, "mine.py", body)
    with pytest.raises(ValueError, match=re.escape(complaint)):
        mutation_smoke.checks_in(tmp_path)


def test_two_files_of_checks_sharing_a_name_are_refused_when_the_list_is_joined(tmp_path, monkeypatch):
    write_checks(tmp_path, "a.py", "CHECKS = (('same', 'f', 'o', 'b', 't'),)\n")
    write_checks(tmp_path, "b.py", "CHECKS = (('same', 'g', 'o', 'b', 't'),)\n")
    with pytest.raises(ValueError, match="^two mutation checks are named 'same'"):
        mutation_smoke.well_formed(mutation_smoke.checks_in(tmp_path))


@pytest.mark.parametrize("body", ["CHECKS = [('one', 'f', 'o', 'b', 't')]\n", "OTHER = ()\n", "CHECKS = None\n"],
                         ids=["a-list", "no-checks", "none"])
def test_a_file_that_doesnt_define_a_tuple_of_checks_is_refused(tmp_path, body):
    write_checks(tmp_path, "mine.py", body)
    with pytest.raises(ValueError, match="mine.py must define CHECKS as a tuple"):
        mutation_smoke.checks_in(tmp_path)


def test_the_list_the_shards_run_includes_every_file_in_the_folder():
    kept = mutation_smoke.checks_in(mutation_smoke.CHECKS_DIR)
    assert kept, "the folder holds at least the checks for its own loading"
    assert mutation_smoke.CHECKS[-len(kept):] == kept, "they follow the list in mutation_smoke.py, in order"
    assert mutation_smoke.CHECKS_DIR == REPO / "tests/mutation_checks"


def test_a_well_formed_list_is_returned_as_it_is():
    checks = (GOOD, ("deletes its target", "bin/pulse.py", "original\n", "", "tests/test_pulse.py::test_two"))
    assert mutation_smoke.well_formed(checks) is checks


def test_two_entries_run_together_into_one_are_refused():
    with pytest.raises(ValueError, match="mutation check 2 isn't"):
        mutation_smoke.well_formed((GOOD, ("joined",) + GOOD[1:4] + ("tests/a.py::one", "second", "f", "o", "b",
                                                                      "tests/a.py::two")))


@pytest.mark.parametrize("check", [
    GOOD[:4], list(GOOD), None, GOOD[:4] + (7,), ("",) + GOOD[1:], GOOD[:1] + ("",) + GOOD[2:],
    GOOD[:2] + ("",) + GOOD[3:], GOOD[:4] + ("",), GOOD[:3] + (GOOD[2],) + GOOD[4:],
], ids=["four-parts", "a-list", "not-a-tuple", "a-part-that-isnt-text", "no-name", "no-file", "no-original",
        "no-test", "a-broken-that-changes-nothing"])
def test_a_malformed_check_is_refused(check):
    with pytest.raises(ValueError, match="mutation check 1 isn't"):
        mutation_smoke.well_formed((check,))


def test_two_checks_with_one_name_are_refused():
    other = ("other", "bin/other.py", "o", "b", "tests/a.py::two")
    with pytest.raises(ValueError, match="^two mutation checks are named 'name'"):
        mutation_smoke.well_formed((GOOD, other, GOOD[:1] + other[1:]))


def test_a_single_string_where_a_check_should_be_says_a_comma_is_missing():
    with pytest.raises(ValueError, match="^mutation check 2 is a single string, not five"):
        mutation_smoke.well_formed((GOOD, "name"))


def test_name_runs_only_the_checks_whose_name_contains_the_text(tmp_path, monkeypatch, capsys):
    ran = []
    monkeypatch.setattr(mutation_smoke, "run_check", lambda name, *rest: ran.append(name) or {"name": name, "mutant": "rejected"})
    mine = [c[0] for c in mutation_smoke.CHECKS if "refused" in c[0]]
    mutation_smoke.main(["--name", "refused", "--output", str(tmp_path / "out.json")])
    assert ran == mine and len(mine) >= 2
    ran.clear()
    mutation_smoke.main(["--name", "refused", "--shard", "1/2", "--output", str(tmp_path / "out.json")])
    assert ran == [c[0] for c in mutation_smoke.CHECKS[0::2] if "refused" in c[0]]
    with pytest.raises(SystemExit):
        mutation_smoke.main(["--name", "no check is called this", "--output", str(tmp_path / "out.json")])
    assert "no check's name contains 'no check is called this'" in capsys.readouterr().err


def test_every_check_names_a_snippet_found_exactly_once_and_a_test_pytest_collects():
    """A check whose snippet has gone stale, or whose test was renamed, would otherwise fail 13 minutes into a
    shard. This says so in seconds, with every stale check at once. The tests are the ones pytest itself collects,
    so a renamed class or parameter id is found too."""
    files = sorted({selector.partition("::")[0] for *_rest, selector in mutation_smoke.CHECKS})
    absent = [name for name in files if not (REPO / name).is_file()]
    assert not absent, f"no such test file: {absent}"
    # Options from the environment, such as a second -q, change what --collect-only prints.
    env = {key: value for key, value in os.environ.items() if key != "PYTEST_ADDOPTS"}
    listed = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider", *files],
                            cwd=REPO, env=env, capture_output=True, text=True)
    assert listed.returncode == 0, listed.stdout[-2000:] + listed.stderr[-2000:]
    collected = {"tests/" + line.strip() for line in listed.stdout.splitlines() if "::" in line}
    collected |= {found.split("[", 1)[0] for found in collected}  # a test id without brackets runs every case of it
    sources: dict = {}
    wrong = []
    for name, filename, original, _broken, selector in mutation_smoke.CHECKS:
        if filename not in sources:
            sources[filename] = (REPO / filename).read_text() if (REPO / filename).is_file() else ""
        if (count := sources[filename].count(original)) != 1:
            wrong.append(f"{name}: its snippet is in {filename} {count} times, not once")
        if selector not in collected:
            wrong.append(f"{name}: pytest collects no test {selector}")
    assert not wrong, "\n".join(wrong)
