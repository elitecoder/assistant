#!/usr/bin/env python3
"""app_update — keep Assistant current by downloading main from GitHub.

Off unless ~/.assistant/config.json has {"self_update": {"download": true}}.
While it's off, the pulse keeps using bin/self_update.py.

Assistant runs from ~/.assistant/app/current, a link to
~/.assistant/app/<commit>/. Your checkout is never read or written, and no git
runs. Once an hour, the pulse:

  1. Asks the GitHub API for the newest commit on main.
  2. If it's new, downloads that commit's archive from codeload.github.com and
     unpacks it to ~/.assistant/app/<commit>/.
  3. Checks the unpacked files: no conflict markers, and every Python file
     parses. A commit that fails is refused and stays refused, with a reminder
     once a day, until main moves.
  4. Points `current` at the new folder in one step, points `previous` at the
     old one, and runs install.sh --apply from `current`.
  5. Deletes every other downloaded folder.

Run it by hand:
  app_update.py --force     update now, ignoring the hourly limit
  app_update.py --rollback  point `current` back at `previous`
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import self_update  # noqa: E402
from assistant import mode  # noqa: E402

DEFAULT_REPO = "elitecoder/assistant"
BRANCH = "main"
DEFAULT_INTERVAL_SEC = 3600
REJECT_REMIND_SEC = 86400
INSTALL_TIMEOUT_SEC = 300
NET_TIMEOUT_SEC = 60
SHA = re.compile(r"[0-9a-f]{40}")
SLUG = re.compile(r"[\w.-]+/[\w.-]+")
SELF_PLIST = f"launchagents/{self_update.SELF_PLIST_LABEL}.plist"


def app_root() -> Path:
    return Path(os.environ["HOME"]) / ".assistant" / "app"


def marker_path() -> Path:
    return Path(os.environ["HOME"]) / ".assistant" / "app-update.json"


def _locked():
    """An open, exclusively locked file, or None when another update holds it."""
    path = marker_path().with_suffix(".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def settings() -> dict | None:
    return mode.download_updates()


def repo_slug(block: dict) -> str:
    slug = block.get("repo", DEFAULT_REPO)
    if not isinstance(slug, str) or not SLUG.fullmatch(slug):
        raise ValueError(f"self_update.repo must look like owner/name, not {slug!r}")
    return slug


def _get(url: str, accept: str, opener=None):
    req = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": "assistant-self-update"})
    return (opener or urllib.request.urlopen)(req, timeout=NET_TIMEOUT_SEC)


def latest_sha(slug: str, opener=None) -> str:
    with _get(f"https://api.github.com/repos/{slug}/commits/{BRANCH}",
              "application/vnd.github.sha", opener) as resp:
        sha = resp.read(100).decode("ascii", "replace").strip()
    if not SHA.fullmatch(sha):
        raise ValueError(f"GitHub returned {sha[:60]!r} instead of a commit id")
    return sha


def _checked_members(tar: tarfile.TarFile) -> tuple[str, list[tarfile.TarInfo]]:
    """The archive's one top folder and its members, after refusing anything
    that would land outside that folder or isn't a plain file, folder, or link
    inside it."""
    members = tar.getmembers()
    tops = {m.name.split("/", 1)[0] for m in members}
    if len(tops) != 1:
        raise ValueError(f"the archive has {len(tops)} top folders, not one")
    top = tops.pop()
    for m in members:
        parts = Path(m.name).parts
        if m.name.startswith("/") or ".." in parts:
            raise ValueError(f"the archive entry {m.name!r} points outside its folder")
        if m.issym():
            target = os.path.normpath(os.path.join(os.path.dirname(m.name), m.linkname))
            if m.linkname.startswith("/") or not (target == top or target.startswith(top + "/")):
                raise ValueError(f"the archive link {m.name!r} points outside its folder")
        elif not (m.isfile() or m.isdir()):
            raise ValueError(f"the archive entry {m.name!r} isn't a file, folder, or link")
    return top, members


def download(slug: str, sha: str, root: Path, opener=None) -> Path:
    """Unpack `sha` to root/<sha> and return that folder. A folder already
    there is complete, because it's only renamed into place once unpacked."""
    dest = root / sha
    if dest.is_dir():
        return dest
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".incoming-", dir=root) as tmp:
        archive = Path(tmp) / "archive.tar.gz"
        with _get(f"https://codeload.github.com/{slug}/tar.gz/{sha}", "application/gzip", opener) as resp, \
                archive.open("wb") as out:
            shutil.copyfileobj(resp, out)
        with tarfile.open(archive, "r:gz") as tar:
            top, members = _checked_members(tar)
            extra = {"filter": "data"} if hasattr(tarfile, "data_filter") else {}
            tar.extractall(tmp, members=members, **extra)
        os.replace(Path(tmp) / top, dest)
    return dest


def check_tree(tree: Path) -> tuple[bool, str]:
    """(True, "ok"), or (False, the first problem): a conflict marker in a file
    Assistant runs or installs, or a Python file that won't parse."""
    for prefix in self_update.SYNTAX_GATE_PATHS:
        start = tree / prefix
        paths = [start] if start.is_file() else sorted(p for p in start.rglob("*") if p.is_file())
        for path in paths:
            if path.is_symlink():
                continue
            name = path.relative_to(tree).as_posix()
            source = path.read_bytes()
            line = self_update._conflict_marker_line(source.decode("utf-8", errors="replace"))
            if line:
                return False, f"conflict marker at {name}:{line}"
            if name.endswith(".py"):
                try:
                    compile(source, name, "exec", dont_inherit=True)
                except (SyntaxError, ValueError) as exc:
                    return False, f"{name}: {exc}"[:500]
    return True, "ok"


def target(link: Path) -> str | None:
    try:
        return os.readlink(link)
    except OSError:
        return None


def _point(link: Path, name: str) -> None:
    tmp = link.with_name(f".{link.name}.{os.getpid()}")
    tmp.unlink(missing_ok=True)
    tmp.symlink_to(name)
    os.replace(tmp, link)


def switch(root: Path, sha: str) -> str | None:
    """Point `current` at root/<sha> and `previous` at what `current` was.
    Returns the old commit, or None on a first install."""
    old = target(root / "current")
    _point(root / "current", sha)
    if old and old != sha:
        _point(root / "previous", old)
    return old


def prune(root: Path) -> list[str]:
    """Delete downloaded folders that `current` and `previous` don't point at."""
    keep = {target(root / "current"), target(root / "previous")}
    removed = []
    for path in root.iterdir():
        if path.is_dir() and not path.is_symlink() and path.name not in keep \
                and (SHA.fullmatch(path.name) or path.name.startswith(".incoming-")):
            shutil.rmtree(path)
            removed.append(path.name)
    return sorted(removed)


def run_install(root: Path, *, from_pulse: bool) -> tuple[int, str]:
    """Run install.sh --apply from `current`, so every link and LaunchAgent it
    writes goes through `current`. From the pulse, install.sh skips reloading
    the pulse's own LaunchAgent and reads no answers."""
    current = root / "current"
    env = dict(os.environ)
    if from_pulse:
        env["ASSISTANT_SELF_UPDATE"] = "1"
    try:
        p = subprocess.run(["bash", str(current / "install.sh"), "--apply"],
                           cwd=str(current), env=env, capture_output=from_pulse, text=True,
                           timeout=INSTALL_TIMEOUT_SEC,
                           stdin=subprocess.DEVNULL if from_pulse else None)
    except subprocess.TimeoutExpired:
        return -1, f"install.sh --apply timed out after {INSTALL_TIMEOUT_SEC}s"
    return p.returncode, ((p.stderr or p.stdout or "")[-500:] if p.returncode else "")


def _same_file(a: Path, b: Path) -> bool:
    try:
        return a.read_bytes() == b.read_bytes()
    except OSError:
        return False


def _read_marker() -> dict:
    try:
        return json.loads(marker_path().read_text())
    except (OSError, ValueError):
        return {}


def _write_marker(data: dict) -> None:
    path = marker_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)


def _short(name: str | None) -> str:
    return (name or "")[:12]


def update(block: dict, *, now: float, from_pulse: bool, opener=None) -> dict:
    """One update attempt. Returns what happened in the shape the pulse logs.
    Never raises for a network, archive, or install failure."""
    root = app_root()
    result: dict = {"attempted": True, "changed": False, "installed": False,
                    "skipped_reason": None, "source": "download"}
    marker = _read_marker()
    old = target(root / "current")
    result["from_sha"] = _short(old)
    try:
        slug = repo_slug(block)
        result["remote"], result["branch"] = slug, BRANCH
        sha = latest_sha(slug, opener)
    except (OSError, ValueError) as exc:
        result.update(skipped_reason="fetch-failed", error=f"could not read {BRANCH}: {exc}"[:500])
        return result
    result["to_sha"] = _short(sha)
    if old == sha and (root / sha).is_dir():
        return result
    if marker.get("rejected") == sha and now - marker.get("rejected_ts", 0) < REJECT_REMIND_SEC:
        result["skipped_reason"] = "syntax-fail-known"
        return result
    try:
        tree = download(slug, sha, root, opener)
    except (OSError, ValueError, tarfile.TarError) as exc:
        result.update(skipped_reason="fetch-failed", error=f"could not download {sha[:12]}: {exc}"[:500])
        return result
    ok, detail = check_tree(tree)
    if not ok:
        shutil.rmtree(tree)
        marker.update(rejected=sha, rejected_ts=now)
        _write_marker(marker)
        result.update(skipped_reason="syntax-fail", syntax_error=detail)
        return result
    plist_changed = not (old and _same_file(root / old / SELF_PLIST, tree / SELF_PLIST))
    switch(root, sha)
    result["changed"] = True
    rc, err = run_install(root, from_pulse=from_pulse)
    result.update(installed=True, install_rc=rc)
    if rc:
        result["error"] = err or f"install.sh --apply exited {rc}"
    elif from_pulse and plist_changed:
        result["self_plist_reload_deferred"] = True
    result["pruned"] = prune(root)
    return result


def maybe_update(*, now: float | None = None, force: bool = False,
                 interval_sec: int = DEFAULT_INTERVAL_SEC, from_pulse: bool = True,
                 opener=None, log=None) -> dict | None:
    """Throttled update with failure counting, like self_update.maybe_update.
    None when the setting is off, the hour isn't up, or repeated failures have
    paused it."""
    block = settings()
    if block is None:
        return None
    now = time.time() if now is None else now
    marker = _read_marker()
    last = marker.get("last_attempt_ts")
    if not force and last and now - last < interval_sec:
        return None
    marker["last_attempt_ts"] = now
    _write_marker(marker)
    watch = self_update._failwatch()
    context = f"download:{block.get('repo', DEFAULT_REPO)}"
    if watch and not force:
        paused_until = watch.backed_off(self_update.WATCH, now=now, context=context)
        if paused_until:
            if log:
                log.info("self-update: paused after repeating the same failure")
            return None
    lock = _locked()
    if lock is None:
        return None
    with lock:
        result = update(block, now=now, from_pulse=from_pulse, opener=opener)
    if log:
        log.info("self-update: %s", json.dumps({k: v for k, v in result.items() if k != "pruned"}))
    if watch and result["skipped_reason"] != "syntax-fail-known":
        problem = result.get("error") or result.get("syntax_error")
        if problem:
            watch.record_failure(self_update.WATCH, problem, now=now,
                                 cap=self_update.WATCH_CAP_SEC, context=context)
        else:
            watch.record_ok(self_update.WATCH, now=now)
    return result


def rollback(*, from_pulse: bool = False) -> dict:
    lock = _locked()
    if lock is None:
        return {"rolled_back": False, "error": "another update is running; try again in a minute"}
    with lock:
        return _rollback(from_pulse)


def _rollback(from_pulse: bool) -> dict:
    root = app_root()
    cur, prev = target(root / "current"), target(root / "previous")
    if not prev or not (root / prev).is_dir():
        return {"rolled_back": False, "error": f"there's no previous version in {root}"}
    _point(root / "current", prev)
    _point(root / "previous", cur)
    rc, err = run_install(root, from_pulse=from_pulse)
    return {"rolled_back": True, "from_sha": _short(cur), "to_sha": _short(prev),
            "install_rc": rc, **({"error": err} if rc else {})}


def main(argv: list[str] | None = None) -> int:
    import argparse
    import logging

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--force", action="store_true", help="Update now, ignoring the hourly limit.")
    ap.add_argument("--rollback", action="store_true", help="Point current back at previous.")
    args = ap.parse_args(argv)
    if settings() is None:
        print('Turn this on first: add "self_update": {"download": true} to ~/.assistant/config.json.',
              file=sys.stderr)
        return 2
    if args.rollback:
        out = rollback()
    else:
        out = maybe_update(force=args.force, interval_sec=DEFAULT_INTERVAL_SEC,
                           from_pulse=False, log=logging.getLogger("app_update"))
    print(json.dumps(out, indent=2))
    return 1 if out and out.get("error") else 0


if __name__ == "__main__":
    sys.exit(main())
