"""Provider-neutral headless LLM subprocess runner."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable


@dataclass(frozen=True)
class RouteConfig:
    # Default provider is claude — the always-present agent. Droid is opt-in
    # (config.json llm.provider / *_LLM_PROVIDER=droid): a missing or malformed
    # config must NOT silently route the fleet's judgment layer at a binary that
    # may not be installed, which fails open to empty verdicts every pulse.
    provider: str = "claude"
    droid_canary_percent: int = 100
    droid_bin: str = "~/.local/bin/droid"
    droid_model: str = "glm-5.2"
    droid_reasoning_effort: str = "high"


@dataclass(frozen=True)
class LLMResult:
    provider: str
    model: str
    rc: int
    stdout: str
    stderr: str
    wall_ms: int
    tokens_in: int
    tokens_out: int
    cost_usd: float | None
    usage_source: str
    session_id: str | None
    result_text: str
    usable: bool

    def metadata(self) -> dict:
        return asdict(self)


def config_path(home: Path | None = None,
                env: dict | None = None) -> Path:
    env = os.environ if env is None else env
    override = env.get("ASSISTANT_LLM_CONFIG")
    if override:
        return Path(override).expanduser()
    return (home or Path.home()) / ".assistant/comms/config.json"


def _bounded_percent(value, default: int = 100) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if 0 <= parsed <= 100 else default


def _provider(value) -> str:
    # Unrecognized / empty coerces to claude (fail-closed to the always-present
    # agent), NOT droid — an opt-in binary that may be absent.
    value = str(value or "").strip().lower()
    return value if value in {"claude", "canary", "droid"} else "claude"


def _reasoning_effort(value) -> str:
    value = str(value or "").strip().lower()
    return value if value in {"off", "high", "max"} else "high"


def load_route_config(path: Path, section: str = "triage",
                      env: dict | None = None) -> RouteConfig:
    env = os.environ if env is None else env
    doc: dict = {}
    try:
        loaded = json.loads(path.expanduser().read_text())
        if isinstance(loaded, dict):
            doc = loaded
    except (OSError, json.JSONDecodeError):
        pass

    legacy = doc.get(section) if isinstance(doc.get(section), dict) else {}
    llm = doc.get("llm") if isinstance(doc.get("llm"), dict) else {}
    features = llm.get("features") \
        if isinstance(llm.get("features"), dict) else {}
    feature = features.get(section) \
        if isinstance(features.get(section), dict) else {}
    global_droid = llm.get("droid") \
        if isinstance(llm.get("droid"), dict) else {}
    feature_droid = feature.get("droid") \
        if isinstance(feature.get("droid"), dict) else {}

    prefix = section.upper()
    provider = env.get(
        f"{prefix}_LLM_PROVIDER",
        feature.get("provider", llm.get(
            "provider", legacy.get("provider", "claude"))),
    )
    percent = env.get(
        f"{prefix}_DROID_CANARY_PERCENT",
        feature.get(
            "droid_canary_percent",
            llm.get(
                "droid_canary_percent",
                legacy.get("droid_canary_percent", 100),
            ),
        ),
    )
    droid_bin = env.get(
        "DROID_BIN",
        feature_droid.get(
            "bin",
            feature.get(
                "droid_bin",
                global_droid.get(
                    "bin",
                    legacy.get(
                        "droid_bin", str(Path.home() / ".local/bin/droid")),
                ),
            ),
        ),
    )
    droid_model = env.get(
        f"{prefix}_DROID_MODEL",
        feature_droid.get(
            "model",
            feature.get(
                "droid_model",
                global_droid.get(
                    "model", legacy.get("droid_model", "glm-5.2")),
            ),
        ),
    )
    reasoning = env.get(
        f"{prefix}_DROID_REASONING_EFFORT",
        feature_droid.get(
            "reasoning_effort",
            feature.get(
                "droid_reasoning_effort",
                global_droid.get(
                    "reasoning_effort",
                    legacy.get("droid_reasoning_effort", "high"),
                ),
            ),
        ),
    )
    return RouteConfig(
        provider=_provider(provider),
        droid_canary_percent=_bounded_percent(percent),
        droid_bin=str(droid_bin),
        droid_model=str(droid_model or "glm-5.2"),
        droid_reasoning_effort=_reasoning_effort(reasoning),
    )


def canary_bucket(keys: list[str]) -> int:
    stable = "\n".join(sorted(str(key) for key in keys))
    digest = hashlib.sha256(stable.encode()).digest()
    return int.from_bytes(digest[:8], "big") % 100


def select_provider(config: RouteConfig, routing_keys: list[str]) -> str:
    if config.provider in {"claude", "droid"}:
        return config.provider
    return "droid" if canary_bucket(routing_keys) < config.droid_canary_percent \
        else "claude"


def resolve_executable(value: str) -> str:
    expanded = Path(value).expanduser()
    if expanded.is_absolute():
        return str(expanded)
    return shutil.which(value) or value


def parse_usage_envelope(stdout: str) -> dict | None:
    try:
        doc = json.loads((stdout or "").strip())
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(doc, dict) or not isinstance(doc.get("usage"), dict):
        return None
    usage = doc["usage"]

    def _integer(key: str) -> int:
        value = usage.get(key)
        return int(value) if isinstance(value, (int, float)) else 0

    cost = doc.get("total_cost_usd")
    return {
        "tokens_in": (
            _integer("input_tokens")
            + _integer("cache_creation_input_tokens")
            + _integer("cache_read_input_tokens")
        ),
        "tokens_out": _integer("output_tokens"),
        "cost_usd": float(cost) if isinstance(cost, (int, float)) else None,
        "session_id": doc.get("session_id"),
        "result_text": doc.get("result") if isinstance(doc.get("result"), str)
        else "",
    }


def parse_result_envelope(provider: str, stdout: str) -> dict | None:
    parsed = parse_usage_envelope(stdout)
    if parsed is None:
        return None
    doc = json.loads((stdout or "").strip())
    if doc.get("type") == "result" and (
        doc.get("is_error") is True
        or doc.get("subtype") not in (None, "success")
    ):
        return None
    if provider == "droid" and (
        doc.get("type") != "result"
        or doc.get("subtype") != "success"
        or doc.get("is_error") is not False
    ):
        return None
    return parsed


def invoke(*, provider: str, prompt: str, model: str, run_dir: Path,
           timeout: int, run: Callable, claude_bin: str,
           droid_bin: str = "~/.local/bin/droid",
           reasoning_effort: str = "high",
           disable_tools: bool = False,
           json_schema: dict | None = None,
           extra_dirs: list[Path] | None = None,
           tag: str = "assistant") -> LLMResult:
    started = time.time()
    provider = _provider(provider)
    if provider == "droid":
        disabled_tools: list[str] = []
        if disable_tools:
            tools_cmd = [
                resolve_executable(droid_bin),
                "exec",
                "--model", model,
                "--list-tools",
                "--output-format", "json",
            ]
            tools_rc, tools_out, tools_err = run(
                tools_cmd,
                timeout=min(timeout, 30),
                merge_bedrock=False,
            )
            try:
                tools = json.loads(tools_out)
                if tools_rc != 0 or not isinstance(tools, list):
                    raise ValueError
                disabled_tools = [
                    str(tool["id"]) for tool in tools
                    if isinstance(tool, dict)
                    and tool.get("currentlyAllowed") is True
                    and tool.get("id")
                ]
            except (json.JSONDecodeError, TypeError, ValueError):
                return LLMResult(
                    provider="droid", model=model, rc=126,
                    stdout=tools_out or "", stderr=tools_err or
                    "failed to enumerate Droid tools",
                    wall_ms=int((time.time() - started) * 1000),
                    tokens_in=0, tokens_out=0, cost_usd=None,
                    usage_source="none", session_id=None, result_text="",
                    usable=False,
                )
        cmd = [
            resolve_executable(droid_bin),
            "exec",
            "--model", model,
            "--reasoning-effort", _reasoning_effort(reasoning_effort),
            "--output-format", "json",
            "--cwd", str(run_dir),
            "--tag", tag,
        ]
        if disabled_tools:
            cmd += ["--disabled-tools", ",".join(disabled_tools)]
        elif not disable_tools:
            cmd += ["--auto", "high"]
        merge_bedrock = False
    else:
        cmd = [
            resolve_executable(claude_bin),
            "--model", model,
            "--print",
            "--output-format", "json",
        ]
        if json_schema is not None:
            cmd += [
                "--json-schema",
                json.dumps(json_schema, separators=(",", ":")),
            ]
        if disable_tools:
            cmd += [
                "--tools", "",
                "--disable-slash-commands",
                "--mcp-config", '{"mcpServers":{}}',
                "--strict-mcp-config",
            ]
        else:
            cmd += [
                "--dangerously-skip-permissions",
                "--add-dir", str(run_dir),
            ]
            for directory in extra_dirs or []:
                cmd += ["--add-dir", str(directory)]
        merge_bedrock = True

    rc, stdout, stderr = run(
        cmd,
        input_text=prompt,
        timeout=timeout,
        merge_bedrock=merge_bedrock,
    )
    wall_ms = int((time.time() - started) * 1000)
    parsed = parse_result_envelope(provider, stdout)
    usage = parse_usage_envelope(stdout)
    if usage is None:
        tokens_in = max(0, len(prompt) // 4)
        tokens_out = max(0, len(stdout or "") // 4)
        usage_source = "estimated"
    else:
        tokens_in = int(usage.get("tokens_in") or 0)
        tokens_out = int(usage.get("tokens_out") or 0)
        usage_source = "cli"
    return LLMResult(
        provider=provider,
        model=model,
        rc=rc,
        stdout=stdout or "",
        stderr=stderr or "",
        wall_ms=wall_ms,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost_usd=(usage or {}).get("cost_usd"),
        usage_source=usage_source,
        session_id=(usage or {}).get("session_id"),
        result_text=(parsed or {}).get("result_text") or "",
        usable=rc == 0 and parsed is not None,
    )
