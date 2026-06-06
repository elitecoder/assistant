#!/usr/bin/env python3
"""self_update — keep the running Assistant current with its git remote.

Folded into the pulse (bin/pulse.py step 0), not a separate daemon: the
existing 5-min LaunchAgent is the one knob, and a new persistent service
would need an explicit `launchctl load`. Throttled to once an hour.

What it does, in order, against the repo pulse.py lives in (so it is
location-independent — a user who installed it anywhere, not just
~/dev/assistant, gets the same behavior):

  1. Throttle on a marker file. Return None (no work) if the last attempt
     was < interval_sec ago.
  2. Read repo status: dirty tree? local commits ahead of remote? how many
     behind? Refuses to touch a repo that is dirty or ahead — it never
     discards or rewrites the operator's work (no reset / clean / force).
     It surfaces that state instead.
  3. If behind: `git pull --ff-only <remote> <branch>`. Fast-forward only,
     so a diverged history fails loudly rather than merging blindly.
     bin/ and prompts/ are symlinked / read live, so a pull alone makes
     code + Observer-prompt changes take effect on the very next pulse.
  4. If the pull touched COPIED artifacts (skills/, launchagents/, the
     installer itself), run `install.sh --apply` to re-copy + reload them.
     ASSISTANT_SELF_UPDATE=1 tells install.sh to skip reloading the pulse's
     OWN plist (reloading it mid-pulse would kill this very process).

Returns a result dict the caller logs to the actions-ledger, so the comms
daemon pings the phone when Assistant updates itself. Never raises for an
operational failure — git/install problems come back as a dict the caller
records; only genuinely unexpected bugs propagate.
"""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

# A pull that changes any of these COPIED / install-time artifacts needs
# `install.sh --apply` to take effect. bin/, prompts/, hooks/, and docs/ are
# symlinked or read live, so a bare pull already makes them current.
INSTALL_REQUIRING_PREFIXES = ("skills/", "launchagents/", "install/")
INSTALL_REQUIRING_FILES = ("install.sh",)

# Reloading this label mid-pulse would SIGTERM the updater. install.sh honors
# ASSISTANT_SELF_UPDATE=1 by skipping it; the plist change applies on the next
# manual install or reboot. Plist changes are rare; code changes are not.
SELF_PLIST_LABEL = "com.assistant.assistant-pulse"

DEFAULT_INTERVAL_SEC = 3600


def _git(repo: Path, *args: str, timeout: int = 90) -> tuple[int, str, str]:
    """Run a git command in `repo`. Returns (rc, stdout, stderr); never raises."""
    try:
        p = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True, text=True, timeout=timeout,
        )
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except subprocess.TimeoutExpired:
        return -1, "", f"git {' '.join(args)} timed out after {timeout}s"
    except Exception as e:  # noqa: BLE001
        return -1, "", str(e)


def resolve_remote_branch(repo: Path) -> tuple[str, str] | None:
    """Pick the (remote, branch) to track. Prefers the branch's configured
    upstream; falls back to the sole remote when tracking isn't set (the
    common case on a fresh clone). Returns None if no remote exists."""
    rc, branch, _ = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    if rc != 0 or not branch or branch == "HEAD":
        return None

    rc, upstream, _ = _git(repo, "rev-parse", "--abbrev-ref",
                           "--symbolic-full-name", "@{u}")
    if rc == 0 and "/" in upstream:
        remote, up_branch = upstream.split("/", 1)
        return remote, up_branch

    rc, remotes_out, _ = _git(repo, "remote")
    remotes = [r for r in remotes_out.splitlines() if r.strip()]
    if not remotes:
        return None
    # Exactly one remote → unambiguous. Otherwise prefer origin, then the first.
    remote = "origin" if "origin" in remotes else remotes[0]
    return remote, branch


def repo_status(repo: Path, remote: str, branch: str) -> dict:
    """Fetch and report tree cleanliness + ahead/behind vs remote/branch."""
    rc, _, err = _git(repo, "fetch", remote, branch)
    if rc != 0:
        return {"error": f"fetch failed: {err}"}

    ref = f"{remote}/{branch}"
    _, head, _ = _git(repo, "rev-parse", "HEAD")
    _, remote_sha, _ = _git(repo, "rev-parse", ref)
    _, porcelain, _ = _git(repo, "status", "--porcelain")
    _, behind_out, _ = _git(repo, "rev-list", "--count", f"HEAD..{ref}")
    _, ahead_out, _ = _git(repo, "rev-list", "--count", f"{ref}..HEAD")

    def _int(s: str) -> int:
        try:
            return int(s.strip())
        except (ValueError, AttributeError):
            return 0

    return {
        "head": head,
        "remote_sha": remote_sha,
        "dirty": bool(porcelain.strip()),
        "behind": _int(behind_out),
        "ahead": _int(ahead_out),
    }


def classify_changed_paths(files: list[str]) -> dict:
    """Given paths changed by the pull, decide whether install.sh must run
    and whether the change touches the pulse's own plist (deferred reload)."""
    needs_install = any(
        f.startswith(INSTALL_REQUIRING_PREFIXES) or f in INSTALL_REQUIRING_FILES
        for f in files
    )
    touches_self_plist = any(
        f == f"launchagents/{SELF_PLIST_LABEL}.plist" for f in files
    )
    return {"needs_install": needs_install, "touches_self_plist": touches_self_plist}


def _read_marker(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _write_marker(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    tmp.replace(path)


def should_attempt(marker: dict, now: float, interval_sec: int) -> bool:
    """True if enough time has elapsed since the last attempt."""
    last = marker.get("last_attempt_ts", 0)
    return (now - last) >= interval_sec


def maybe_update(
    repo: Path,
    *,
    now: float | None = None,
    interval_sec: int = DEFAULT_INTERVAL_SEC,
    marker_path: Path | None = None,
    install_sh: Path | None = None,
    log=None,
) -> dict | None:
    """Throttled self-update. Returns None when throttled (no attempt this
    pulse), otherwise a result dict describing what happened. Never raises on
    an operational failure."""
    now = time.time() if now is None else now
    marker_path = marker_path or (repo.parent.parent / ".assistant" / "self-update.json")
    install_sh = install_sh or (repo / "install.sh")

    def _log(msg: str) -> None:
        if log:
            log.info("self-update: %s", msg)

    marker = _read_marker(marker_path)
    if not should_attempt(marker, now, interval_sec):
        return None

    # Stamp the attempt up front so a crash mid-update still respects the
    # throttle next pulse (we never want a hot retry loop).
    marker["last_attempt_ts"] = now
    _write_marker(marker_path, marker)

    result: dict = {"attempted": True, "changed": False, "installed": False,
                    "skipped_reason": None}

    rb = resolve_remote_branch(repo)
    if rb is None:
        result["skipped_reason"] = "no-remote"
        _log("no git remote configured; skipping")
        return result
    remote, branch = rb
    result["remote"], result["branch"] = remote, branch

    status = repo_status(repo, remote, branch)
    if status.get("error"):
        result["skipped_reason"] = "fetch-failed"
        result["error"] = status["error"]
        _log(status["error"])
        return result

    result.update({k: status[k] for k in ("dirty", "ahead", "behind")})
    result["from_sha"] = (status.get("head") or "")[:12]

    if status["dirty"]:
        result["skipped_reason"] = "dirty"
        _log("working tree is dirty — refusing to pull (surfacing instead)")
        return result
    if status["ahead"]:
        result["skipped_reason"] = "ahead"
        _log(f"local is {status['ahead']} commit(s) ahead of {remote}/{branch} "
             "— refusing to pull (unpushed work)")
        return result
    if status["behind"] == 0:
        _log("already up to date")
        return result

    # Fast-forward only — a diverged history fails rather than merging blindly.
    old_head = status["head"]
    rc, _, err = _git(repo, "pull", "--ff-only", remote, branch)
    if rc != 0:
        result["skipped_reason"] = "pull-failed"
        result["error"] = err
        _log(f"git pull --ff-only failed: {err}")
        return result

    _, new_head, _ = _git(repo, "rev-parse", "HEAD")
    result["changed"] = new_head != old_head
    result["to_sha"] = new_head[:12]
    if not result["changed"]:
        return result

    rc, files_out, _ = _git(repo, "diff", "--name-only", f"{old_head}..{new_head}")
    files = [f for f in files_out.splitlines() if f.strip()]
    result["files_changed"] = files
    _log(f"pulled {old_head[:12]}..{new_head[:12]} ({len(files)} file(s))")

    cls = classify_changed_paths(files)
    result["needs_install"] = cls["needs_install"]
    if not cls["needs_install"]:
        # bin/ + prompts/ are live via symlink — nothing else to do.
        return result

    if not install_sh.exists():
        result["install_rc"] = None
        result["error"] = f"install.sh not found at {install_sh}"
        _log(result["error"])
        return result

    import os
    env = dict(os.environ)
    env["ASSISTANT_SELF_UPDATE"] = "1"  # tell install.sh to skip self-plist reload
    try:
        p = subprocess.run(
            ["bash", str(install_sh), "--apply"],
            capture_output=True, text=True, timeout=300, env=env,
            cwd=str(repo),
        )
        result["installed"] = True
        result["install_rc"] = p.returncode
        if p.returncode != 0:
            result["error"] = (p.stderr or p.stdout)[-500:]
            _log(f"install.sh --apply rc={p.returncode}")
        else:
            _log("install.sh --apply ok")
        if cls["touches_self_plist"]:
            result["self_plist_reload_deferred"] = True
            _log(f"{SELF_PLIST_LABEL}.plist changed — reload deferred "
                 "(applies on next manual install or reboot)")
    except subprocess.TimeoutExpired:
        result["install_rc"] = -1
        result["error"] = "install.sh --apply timed out after 300s"
        _log(result["error"])

    return result


if __name__ == "__main__":  # manual run: ./self_update.py [--force]
    import argparse
    import logging
    import sys

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--force", action="store_true",
                    help="Ignore the throttle and attempt now.")
    ap.add_argument("--repo", default=None, help="Repo path (default: this repo).")
    args = ap.parse_args()

    repo = Path(args.repo).resolve() if args.repo else Path(__file__).resolve().parent.parent
    out = maybe_update(
        repo,
        interval_sec=0 if args.force else DEFAULT_INTERVAL_SEC,
        log=logging.getLogger("self_update"),
    )
    print(json.dumps(out, indent=2))
    sys.exit(0)
