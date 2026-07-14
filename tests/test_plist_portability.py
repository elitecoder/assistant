"""Portability integration test — plists must render for ANY user/arch, and the
installer must leave no author-specific literal behind. NO launchctl is run
(honors the 'never launchctl load' rule); we exercise install.sh's staging into
a temp HOME and assert on the rendered files.

This is the gate for the C1 fix: the old `/Users/<user>/`+sed scheme was a silent
no-op that shipped /Users/mukuls to every other machine. A grep-for-mukuls alone
would NOT catch the other half (a hardcoded /opt/homebrew interpreter on an Intel
box), so we also assert the interpreter path exists and no token survives.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PLISTS = sorted((REPO / "launchagents").glob("*.plist"))
TOKENS = ("__HOME__", "__REPO__", "__PYTHON__", "__PATH__")


def test_no_author_literals_in_bin_scripts():
    """Runtime bin/ scripts must not hardcode the author's home or a fixed
    ~/dev/assistant checkout — those break on any other machine / relocated
    checkout (P1/P2/P3/P5/P6). Derive from $HOME, __file__, or an env override
    instead. This is the durable gate that keeps portability from regressing.

    ALLOWED: comments/docstrings that mention the path illustratively, and the
    literal inside a .replace()/env-default fallback is NOT allowed (those were
    the actual bugs). We scan non-comment code lines for the two literals."""
    bin_dir = REPO / "bin"
    literal_home = "/Users/mukuls"
    hardcoded_repo = re.compile(r'["\']~?/?[Uu]sers/mukuls|HOME\s*/\s*["\']dev["\']\s*/\s*["\']assistant["\']')
    offenders = []
    for py in sorted(bin_dir.rglob("*.py")):
        for i, line in enumerate(py.read_text(errors="replace").splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            # strip trailing inline comment (rough — good enough for a literal scan)
            code = line.split("  #")[0]
            if literal_home in code or re.search(r'HOME\s*/\s*["\']dev["\']\s*/\s*["\']assistant["\']', code):
                offenders.append(f"{py.relative_to(REPO)}:{i}: {stripped[:90]}")
    assert not offenders, (
        "bin/ scripts hardcode an author path (use $HOME / __file__ / env):\n  "
        + "\n  ".join(offenders))


def test_committed_plists_are_templates_not_literals():
    """No committed plist may carry a literal author path or hardcoded
    interpreter — those must be tokens the installer substitutes."""
    offenders = []
    for p in PLISTS:
        t = p.read_text()
        if "/Users/mukuls" in t:
            offenders.append(f"{p.name}: literal /Users/mukuls")
        if "/opt/homebrew/bin/python3" in t or "/usr/bin/python3" in t:
            offenders.append(f"{p.name}: hardcoded interpreter (use __PYTHON__)")
    assert not offenders, "committed plists must be tokenized:\n  " + "\n  ".join(offenders)


def test_every_plist_has_the_expected_tokens():
    """Each plist must reference __HOME__ and __REPO__ (all run a repo script as
    the user) — a sanity check that tokenization wasn't partial."""
    for p in PLISTS:
        t = p.read_text()
        assert "__HOME__" in t, f"{p.name} missing __HOME__"
        assert "__REPO__" in t, f"{p.name} missing __REPO__"


def _render(plist_text: str, home: str, repo: str, python: str, path: str) -> str:
    return (plist_text
            .replace("__PYTHON__", python)
            .replace("__REPO__", repo)
            .replace("__HOME__", home)
            .replace("__PATH__", path))


@pytest.mark.parametrize("plist", PLISTS, ids=[p.name for p in PLISTS])
def test_plist_renders_clean_for_arbitrary_user(plist: Path, tmp_path: Path):
    """Render each plist as a fake user on either arch: no token survives, no
    /Users/mukuls survives, and (if plutil is available) it's valid."""
    home = "/Users/janedoe"
    repo = f"{home}/dev/assistant"
    python = "/usr/local/bin/python3"   # Intel path — proves arch-independence
    path = "/usr/local/bin:/usr/bin:/bin:/Applications/cmux.app/Contents/Resources/bin"
    rendered = _render(plist.read_text(), home, repo, python, path)

    assert not re.search(r"__[A-Z]+__", rendered), f"{plist.name}: token survived render"
    assert "/Users/mukuls" not in rendered, f"{plist.name}: author path survived"
    assert home in rendered, f"{plist.name}: fake HOME not present after render"

    if shutil.which("plutil"):
        out = tmp_path / plist.name
        out.write_text(rendered)
        r = subprocess.run(["plutil", "-lint", str(out)], capture_output=True, text=True)
        assert r.returncode == 0, f"{plist.name}: plutil lint failed: {r.stdout}{r.stderr}"


def test_install_sh_substitutes_all_tokens_end_to_end(tmp_path: Path):
    """The real portability gate: copy the repo to a mukuls-FREE path, then run
    install.sh --apply against a fully sandboxed fake HOME (no launchctl — the
    script only stages+copies plists, loads nothing). Because BOTH the checkout
    path and HOME are now free of the author's username, ZERO '/Users/mukuls'
    may survive in any rendered plist — this is what a fresh machine for a
    different engineer actually looks like. Also assert no token survives and
    the interpreter path resolves."""
    if not shutil.which("rsync"):
        pytest.skip("rsync unavailable")
    # pytest's tmp_path lives under /private/var/folders/.../pytest-of-mukuls/…,
    # so it can't be mukuls-free. Use an explicit temp root under /tmp
    # (→ /private/tmp, no username) so both the checkout AND HOME are free of the
    # author's name — a true "different engineer's machine" simulation.
    import tempfile
    sandbox = Path(tempfile.mkdtemp(prefix="assistant-portability-", dir="/tmp"))
    try:
        _run_portability_e2e(sandbox)
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)


def _run_portability_e2e(sandbox: Path):
    # Copy the working tree (incl. uncommitted changes) minus the heavy/derived
    # dirs, to a path with no 'mukuls' in it.
    repo_copy = sandbox / "checkout" / "assistant"
    repo_copy.parent.mkdir(parents=True)
    rc = subprocess.run(
        ["rsync", "-a", "--exclude=.git", "--exclude=node_modules",
         "--exclude=__pycache__", "--exclude=.venv-mem0",
         f"{REPO}/", str(repo_copy) + "/"],
        capture_output=True, text=True, timeout=120)
    assert rc.returncode == 0, f"rsync failed: {rc.stderr}"
    assert "mukuls" not in str(repo_copy), "test setup: copy path must be mukuls-free"

    fake_home = sandbox / "home"
    (fake_home / "Library" / "LaunchAgents").mkdir(parents=True)
    (fake_home / ".claude").mkdir(parents=True)
    (fake_home / ".assistant").mkdir(parents=True)

    env = {
        "HOME": str(fake_home),
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/usr/local/bin",
        "ASSISTANT_SELF_UPDATE": "1",  # report-only preflight; never blocks
    }
    r = subprocess.run(
        ["bash", str(repo_copy / "install.sh"), "--apply"],
        capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL, timeout=180)
    la = fake_home / "Library" / "LaunchAgents"
    rendered = list(la.glob("com.assistant.*.plist")) + list(la.glob("com.mukul.*.plist"))
    assert rendered, f"no plists staged into {la}\nstdout:\n{r.stdout[-2000:]}\nstderr:\n{r.stderr[-1000:]}"

    for p in rendered:
        t = p.read_text()
        assert not re.search(r"__[A-Z]+__", t), f"{p.name}: token survived install"
        assert "/Users/mukuls" not in t, f"{p.name}: /Users/mukuls survived install (not portable!)"
        assert str(fake_home) in t, f"{p.name}: fake HOME not substituted"
        assert str(repo_copy) in t, f"{p.name}: repo path not substituted"
        # the interpreter (ProgramArguments[0]) must be a real executable
        m = re.search(r"<key>ProgramArguments</key>\s*<array>\s*<string>([^<]+)</string>", t)
        assert m, f"{p.name}: no ProgramArguments interpreter"
        interp = m.group(1)
        if interp not in ("/bin/zsh", "/bin/bash"):  # launcher scripts use a shell
            assert Path(interp).exists(), f"{p.name}: interpreter {interp} does not exist"
