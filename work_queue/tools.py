"""Run the command-line tools the sources read through: gh, gog, npx (Work IQ), and uv (Playwright)."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
# The Playwright release the quality workflow tests with (.github/workflows/quality.yml).
PLAYWRIGHT = "playwright==1.63.0"
PROFILE = '[ -f "$HOME/.zprofile" ] && . "$HOME/.zprofile" >/dev/null 2>&1; exec "$0" "$@"'


class SourceError(RuntimeError):
    """A source couldn't be read. The message says what failed, never a secret."""


class ToolMissing(SourceError):
    """The command-line tool a source needs isn't installed."""


def run(cmd: list[str], *, timeout: int = 120, cwd: Path | None = None, env: dict | None = None) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd,
                           env={**os.environ, **(env or {})})
    except FileNotFoundError as exc:
        raise ToolMissing(f"{cmd[0]} isn't installed") from exc
    except subprocess.TimeoutExpired as exc:
        raise SourceError(f"{cmd[0]} didn't answer within {timeout} seconds") from exc
    if r.returncode:
        raise SourceError(f"{' '.join(cmd[:3])} failed: {(r.stderr or r.stdout).strip()[-300:]}")
    return r.stdout


def run_json(cmd: list[str], **kw):
    out = run(cmd, **kw)
    try:
        return json.loads(out)
    except ValueError as exc:
        raise SourceError(f"{' '.join(cmd[:3])} didn't return JSON") from exc


def with_profile(cmd: list[str]) -> list[str]:
    """Run cmd under zsh with ~/.zprofile sourced; launchd gives the server neither your tokens nor your PATH."""
    return ["/bin/zsh", "-c", PROFILE, *cmd]


def playwright_cmd(script: str, *args: str) -> list[str]:
    return ["uv", "run", "--no-project", "--quiet", "--with", PLAYWRIGHT, "python", str(REPO / "work_queue" / script), *args]
