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
REQUIRED_FILES = ("install.sh", "bin/pulse.py", "bin/app_update.py")
DAEMON_LABEL = "com.mukul.assistant-daemon"
HERE = Path(__file__).resolve()


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
    """The archive's one top folder and its members. Refuses `..`, more than
    one top folder, and anything but plain files and folders: the repo holds
    no links, and a link could point outside the folder."""
    members = tar.getmembers()
    tops = {m.name.split("/", 1)[0] for m in members}
    if len(tops) != 1 or not next(iter(tops)):
        raise ValueError(f"the archive has {len(tops)} top folders, not one named folder")
    for m in members:
        if m.name.startswith("/") or ".." in Path(m.name).parts:
            raise ValueError(f"the archive entry {m.name!r} points outside its folder")
        if not (m.isfile() or m.isdir()):
            raise ValueError(f"the archive entry {m.name!r} isn't a plain file or folder")
    return tops.pop(), members


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
    """(True, "ok"), or (False, the first problem): a missing core file, a
    conflict marker in a file Assistant runs or installs, or a Python file that
    won't parse."""
    for name in REQUIRED_FILES:
        if not (tree / name).is_file():
            return False, f"{name} is missing"
    for prefix in self_update.SYNTAX_GATE_PATHS:
        start = tree / prefix
        paths = [start] if start.is_file() else sorted(p for p in start.rglob("*") if p.is_file())
        for path in paths:
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


def starts(tree: Path) -> tuple[bool, str]:
    """(True, "ok") when the new copy's updater loads its imports and starts,
    so the next update can run; else (False, why)."""
    try:
        p = subprocess.run([sys.executable, "-I", str(tree / "bin/app_update.py"), "--help"],
                           capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return False, "bin/app_update.py --help didn't finish in 60s"
    if p.returncode:
        return False, f"bin/app_update.py won't start: {(p.stderr or p.stdout).strip()[-300:]}"
    return True, "ok"


def daemon_running() -> bool:
    """True when the opt-in single-process daemon is running. It runs the pulse
    inside itself, so an update can't restart it."""
    p = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{DAEMON_LABEL}"],
                       capture_output=True, text=True)
    return p.returncode == 0 and "state = running" in p.stdout


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
    if old and old != sha:
        _point(root / "previous", old)
    _point(root / "current", sha)
    return old


def restart_failed() -> bool:
    """install.sh leaves this note when it couldn't restart a running service."""
    return (Path(os.environ["HOME"]) / ".assistant" / "service-restart-failed").exists()


def prune(root: Path) -> list[str]:
    """Delete downloaded folders that `current` and `previous` don't point at.
    install.sh restarts every running service onto a new folder, so none of
    them still runs from a folder this deletes."""
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
                           timeout=INSTALL_TIMEOUT_SEC if from_pulse else None,
                           stdin=subprocess.DEVNULL if from_pulse else None)
    except subprocess.TimeoutExpired:
        return -1, f"install.sh --apply timed out after {INSTALL_TIMEOUT_SEC}s"
    return p.returncode, ((p.stderr or p.stdout or "")[-500:] if p.returncode else "")


def repoint_services(root: Path) -> list[str]:
    """Reload each Assistant LaunchAgent whose file runs from `current` but
    whose loaded copy doesn't, such as a service you started by hand before
    turning this on. install.sh only reloads the services it manages."""
    current = str(root / "current")
    domain = f"gui/{os.getuid()}"
    moved = []
    agents = Path(os.environ["HOME"]) / "Library/LaunchAgents"
    for plist in sorted([*agents.glob("com.assistant.*.plist"), *agents.glob("com.mukul.assistant-*.plist")]):
        try:
            if current.encode() not in plist.read_bytes():
                continue
        except OSError:
            continue
        label = plist.stem
        loaded = subprocess.run(["launchctl", "print", f"{domain}/{label}"],
                                capture_output=True, text=True)
        if loaded.returncode != 0 or current in loaded.stdout:
            continue
        subprocess.run(["launchctl", "bootout", f"{domain}/{label}"], capture_output=True)
        for attempt in (["bootstrap", domain, str(plist)], ["bootstrap", domain, str(plist)],
                        ["load", str(plist)]):
            started = subprocess.run(["launchctl", *attempt], capture_output=True, text=True)
            now = subprocess.run(["launchctl", "print", f"{domain}/{label}"], capture_output=True, text=True)
            if now.returncode == 0 and current in now.stdout:
                break
            time.sleep(1)
        else:
            why = started.stderr.strip() or "it isn't loaded from current afterwards"
            moved.append(f"{label} (couldn't start: {why[:200]})")
            continue
        moved.append(label)
    return moved


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


def _move_services(root: Path, result: dict) -> None:
    result["moved_services"] = moved = repoint_services(root)
    stuck = [m for m in moved if "couldn't start" in m]
    if stuck:
        result["error"] = "couldn't start " + "; ".join(stuck)


def _linked(root: Path) -> bool:
    """True when ~/.claude/bin leads into `current`, as install.sh leaves it."""
    link = Path(os.environ["HOME"]) / ".claude/bin"
    return link.resolve() == (root / "current" / "bin").resolve()


def _rejected_key(sha: str) -> str:
    return f"{sha} python{sys.version_info[0]}.{sys.version_info[1]}"


def _install(root: Path, result: dict, marker: dict, sha: str, from_pulse: bool) -> None:
    rc, err = run_install(root, from_pulse=from_pulse)
    result.update(installed=True, install_rc=rc)
    if rc:
        result["error"] = err or f"install.sh --apply exited {rc}"
        marker.pop("installed", None)
    else:
        marker["installed"] = sha
        if restart_failed():
            result["error"] = "install.sh couldn't restart a running service; the next run tries again"
    _write_marker(marker)


def update(block: dict, *, now: float, from_pulse: bool, opener=None) -> dict:
    """One update attempt. Returns what happened in the shape the pulse logs.
    Never raises for a network, archive, or install failure."""
    root = app_root()
    result: dict = {"attempted": True, "changed": False, "installed": False,
                    "skipped_reason": None, "source": "download"}
    marker = _read_marker()
    old = target(root / "current")
    result["from_sha"] = _short(old)
    if from_pulse and root.resolve() not in HERE.parents:
        result.update(skipped_reason="not-set-up", error=(
            "download updates are on, but the pulse still runs from your checkout; "
            "run bin/app_update.py --force from your checkout once"))
        return result
    if daemon_running():
        result.update(skipped_reason="daemon-running", error=(
            f"download updates don't support the opt-in single-process daemon; stop it for good with "
            f"launchctl disable gui/{os.getuid()}/{DAEMON_LABEL} and "
            f"launchctl bootout gui/{os.getuid()}/{DAEMON_LABEL}, or turn download updates off"))
        return result
    try:
        slug = repo_slug(block)
        result["remote"], result["branch"] = slug, BRANCH
        sha = latest_sha(slug, opener)
    except Exception as exc:  # noqa: BLE001 — any network or reply failure is reported, never raised
        result.update(skipped_reason="fetch-failed", error=f"could not read {BRANCH}: {exc}"[:500])
        return result
    result["to_sha"] = _short(sha)
    if marker.get("held") not in (None, sha) or (marker.get("held") and not from_pulse):
        marker.pop("held")
        _write_marker(marker)
    if old == sha and (root / sha).is_dir() or marker.get("held") == sha:
        if marker.get("held") == sha:
            result["skipped_reason"] = "rolled-back"
        if old and (marker.get("installed") != old or restart_failed() or not _linked(root)):
            _install(root, result, marker, old, from_pulse)
        if old and not from_pulse and not result.get("error"):
            _move_services(root, result)
        return result
    if marker.get("rejected") == _rejected_key(sha) \
            and now - marker.get("rejected_ts", 0) < REJECT_REMIND_SEC:
        result["skipped_reason"] = "syntax-fail-known"
        return result
    try:
        tree = download(slug, sha, root, opener)
    except Exception as exc:  # noqa: BLE001 — a cut-off or bad archive is reported, never raised
        result.update(skipped_reason="fetch-failed", error=f"could not download {sha[:12]}: {exc}"[:500])
        return result
    ok, detail = check_tree(tree)
    if ok:
        ok, detail = starts(tree)
    if not ok:
        shutil.rmtree(tree)
        marker.update(rejected=_rejected_key(sha), rejected_ts=now)
        _write_marker(marker)
        result.update(skipped_reason="syntax-fail", syntax_error=detail)
        return result
    plist_changed = not (old and _same_file(root / old / SELF_PLIST, tree / SELF_PLIST))
    switch(root, sha)
    result["changed"] = True
    _install(root, result, marker, sha, from_pulse)
    if not result.get("error") and from_pulse and plist_changed:
        result["self_plist_reload_deferred"] = True
    if not result.get("error") and not from_pulse:
        _move_services(root, result)
    if not result.get("error"):
        result["pruned"] = prune(root)
    return result


def maybe_update(*, now: float | None = None, force: bool = False,
                 interval_sec: int = DEFAULT_INTERVAL_SEC, from_pulse: bool = True,
                 opener=None, log=None) -> dict | None:
    """Throttled update with failure counting, like self_update.maybe_update.
    None when the setting is off, the hour isn't up, repeated failures have
    paused it, or another update is running."""
    block = settings()
    if block is None:
        return None
    now = time.time() if now is None else now
    lock = _locked()
    if lock is None:
        if log:
            log.info("self-update: another update is running")
        return None
    with lock:
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
        result = update(block, now=now, from_pulse=from_pulse, opener=opener)
    if log:
        log.info("self-update: %s", json.dumps({k: v for k, v in result.items() if k != "pruned"}))
    if watch and (result["skipped_reason"] not in ("syntax-fail-known", "rolled-back") or result.get("error")):
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
    """Point `current` at `previous`, and hold back the version it left until
    main moves past it, so the next hourly update doesn't undo the rollback."""
    root = app_root()
    cur, prev = target(root / "current"), target(root / "previous")
    if not cur or not prev or not (root / prev).is_dir():
        return {"rolled_back": False, "error": f"there's no previous version in {root}"}
    _point(root / "previous", cur)
    _point(root / "current", prev)
    marker = _read_marker()
    marker["held"] = cur
    result = {"rolled_back": True, "from_sha": _short(cur), "to_sha": _short(prev)}
    _install(root, result, marker, prev, from_pulse)
    return {k: v for k, v in result.items() if k != "installed"}


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
    if out is None:
        print("Nothing ran: it ran less than an hour ago, another update is running, or repeated "
              "failures paused it. --force tries anyway, unless another update is running.", file=sys.stderr)
        return 1
    print(json.dumps(out, indent=2))
    return 1 if out.get("error") or out.get("syntax_error") else 0


if __name__ == "__main__":
    sys.exit(main())
