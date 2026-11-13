"""The session check's PR snapshot reads the branch and repo from the
checkout's files and asks gh by repo and branch, so gh never runs git."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest import mock

import pytest

from test_observer_skip import load_pulse


@pytest.fixture
def pulse(tmp_path):
    return load_pulse(tmp_path / "home")


def make_checkout(root: Path, config: str, head: str = "ref: refs/heads/feat/x") -> Path:
    git = root / ".git"
    git.mkdir(parents=True)
    (git / "HEAD").write_text(head + "\n")
    (git / "config").write_text(config)
    return root


CONFIG = """[core]
\tbare = false
[remote "origin"]
\turl = git@github.com:acme/widgets.git
\tfetch = +refs/heads/*:refs/remotes/origin/*
[remote "personal"]
\turl = https://github.com/me/widgets.git
[branch "feat/x"]
\tremote = personal
\tmerge = refs/heads/main
"""


def test_reads_branch_and_its_remote(pulse, tmp_path):
    checkout = make_checkout(tmp_path / "repo", CONFIG)
    (checkout / "src").mkdir()
    assert pulse._checkout_pr_target(str(checkout / "src")) == (
        "github.com", "me/widgets", "feat/x")


def test_linked_worktree_uses_the_shared_config(pulse, tmp_path):
    main = make_checkout(tmp_path / "main", CONFIG.replace("[branch \"feat/x\"]\n\tremote = personal\n", ""))
    wt_git = main / ".git/worktrees/wt"
    wt_git.mkdir(parents=True)
    (wt_git / "HEAD").write_text("ref: refs/heads/fix/y\n")
    (wt_git / "commondir").write_text("../..\n")
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".git").write_text(f"gitdir: {wt_git}\n")
    assert pulse._checkout_pr_target(str(wt)) == ("github.com", "acme/widgets", "fix/y")


@pytest.mark.parametrize("url,expected", [
    ("git@github-personal:me/tool.git", ("github.com", "me/tool")),
    ("ssh://git@git.corp.example.com:22/team/app", ("git.corp.example.com", "team/app")),
    ("https://github.com/me/tool", ("github.com", "me/tool")),
])
def test_remote_url_forms(pulse, tmp_path, url, expected):
    make_checkout(tmp_path, f'[remote "only"]\n\turl = {url}\n')
    assert pulse._checkout_pr_target(str(tmp_path)) == (*expected, "feat/x")


@pytest.mark.parametrize("config,head", [
    (CONFIG, "0123456789abcdef0123456789abcdef01234567"),
    ('[remote "a"]\n\turl = x:o/r\n[remote "b"]\n\turl = y:o/r\n', "ref: refs/heads/feat/x"),
    ('[remote "origin"]\n\turl = not a url\n', "ref: refs/heads/feat/x"),
])
def test_no_target_without_a_branch_or_a_clear_remote(pulse, tmp_path, config, head):
    make_checkout(tmp_path, config, head)
    assert pulse._checkout_pr_target(str(tmp_path)) is None


def test_no_target_outside_a_checkout_or_when_unreadable(pulse, tmp_path):
    assert pulse._checkout_pr_target(str(tmp_path)) is None
    (tmp_path / "broken" / ".git").mkdir(parents=True)
    assert pulse._checkout_pr_target(str(tmp_path / "broken")) is None
    binary = make_checkout(tmp_path / "binary", "")
    (binary / ".git/config").write_bytes(b"[remote \"origin\"]\n\turl = \xff\xfe\n")
    assert pulse._checkout_pr_target(str(binary)) is None


def test_snapshot_asks_gh_by_repo_and_branch(pulse, tmp_path):
    checkout = make_checkout(tmp_path / "repo", CONFIG)
    pr = {"number": 7, "baseRefName": "main", "mergeStateStatus": "BLOCKED"}
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        out = json.dumps(pr) if argv[1] == "pr" else "active\n"
        return subprocess.CompletedProcess(argv, 0, out, "")

    with mock.patch.object(pulse.subprocess, "run", side_effect=fake_run):
        snap = pulse._observer_pr_snapshot(str(checkout))
    assert snap["number"] == 7 and snap["freeze_active"] is True
    assert calls[0][0][:6] == ["gh", "pr", "view", "feat/x", "--repo", "github.com/me/widgets"]
    assert calls[1][0][:4] == ["gh", "api", "--hostname", "github.com"]
    assert all("cwd" not in kwargs for _, kwargs in calls)


def test_snapshot_skips_gh_without_a_target(pulse, tmp_path):
    with mock.patch.object(pulse.subprocess, "run") as run:
        assert pulse._observer_pr_snapshot(str(tmp_path)) is None
    run.assert_not_called()


def test_snapshot_runs_no_git_on_a_real_checkout(pulse, tmp_path):
    repo = tmp_path / "real"
    subprocess.run(["git", "init", "-q", "-b", "feat/z", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin",
                    "https://github.com/acme/real.git"], check=True)
    with mock.patch.object(pulse.subprocess, "run",
                           return_value=subprocess.CompletedProcess([], 1, "", "")) as run:
        assert pulse._observer_pr_snapshot(str(repo)) is None
    argv = run.call_args.args[0]
    assert argv[:6] == ["gh", "pr", "view", "feat/z", "--repo", "github.com/acme/real"]


def test_comments_and_blank_lines_are_ignored(pulse, tmp_path):
    make_checkout(tmp_path, '[remote "origin"]\n\turl = git@github.com:right/repo.git\n'
                            '\n\t# url = git@github.com:wrong/repo.git\n\t; url = x:wrong/again\n')
    assert pulse._checkout_pr_target(str(tmp_path)) == ("github.com", "right/repo", "feat/x")
