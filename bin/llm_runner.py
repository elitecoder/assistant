"""Provider-neutral headless LLM subprocess runner."""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import time
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

import agent_session


@dataclass(frozen=True)
class RouteConfig:
    # Default provider is claude — the always-present agent. Droid is opt-in
    # (config.json llm.provider / *_LLM_PROVIDER=droid): a missing or malformed
    # config must NOT silently route the fleet's judgment layer at a binary that
    # may not be installed, which fails open to empty verdicts every pulse.
    provider: str = "claude"
    droid_bin: str = ""
    droid_model: str = "glm-5.2"
    droid_reasoning_effort: str = "high"
    copilot_model: str = ""
    local_url: str = "http://127.0.0.1:8091/v1"
    local_model: str = "local"
    local_reasoning_effort: str = "high"

    @property
    def reasoning_effort(self) -> str:
        return (self.local_reasoning_effort if self.provider == "local"
                else self.droid_reasoning_effort)

    def model_for(self, claude_model: str) -> str:
        """The model id for this route's provider."""
        return {"droid": self.droid_model, "local": self.local_model,
                "copilot": self.copilot_model}.get(self.provider, claude_model)


PROVIDERS = (*agent_session.AGENTS, "local")
DEFAULT_LOCAL_URL = RouteConfig.local_url
DEFAULT_LOCAL_MODEL = RouteConfig.local_model
LOCAL_MAX_TOKENS = 16384


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


def _provider(value) -> str:
    # Unrecognized / empty coerces to claude (fail-closed to the always-present
    # agent), NOT droid — an opt-in binary that may be absent.
    value = str(value or "").strip().lower()
    if value in PROVIDERS:
        return value
    if value:
        logging.getLogger("llm_runner").warning(
            "unknown LLM provider %r, falling back to claude", value)
    return "claude"


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
    droid_bin = env.get(
        agent_session.bin_env_var(agent_session.DROID),
        feature_droid.get(
            "bin",
            feature.get(
                "droid_bin",
                global_droid.get(
                    "bin",
                    legacy.get(
                        "droid_bin", ""),
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
    global_local = llm.get("local") if isinstance(llm.get("local"), dict) else {}
    feature_local = feature.get("local") \
        if isinstance(feature.get("local"), dict) else {}
    local_url = env.get("ASSISTANT_LOCAL_LLM_URL", feature_local.get(
        "url", global_local.get("url", DEFAULT_LOCAL_URL)))
    local_model = env.get(f"{prefix}_LOCAL_MODEL", feature_local.get(
        "model", global_local.get("model", DEFAULT_LOCAL_MODEL)))
    local_reasoning = env.get(f"{prefix}_LOCAL_REASONING_EFFORT", feature_local.get(
        "reasoning_effort", global_local.get("reasoning_effort", "high")))
    global_copilot = llm.get("copilot") \
        if isinstance(llm.get("copilot"), dict) else {}
    feature_copilot = feature.get("copilot") \
        if isinstance(feature.get("copilot"), dict) else {}
    copilot_model = env.get(f"{prefix}_COPILOT_MODEL", env.get(
        "COPILOT_MODEL", feature_copilot.get(
            "model", global_copilot.get("model", ""))))
    return RouteConfig(
        provider=_provider(provider),
        copilot_model=str(copilot_model or ""),
        droid_bin=str(droid_bin),
        droid_model=str(droid_model or "glm-5.2"),
        droid_reasoning_effort=_reasoning_effort(reasoning),
        local_url=str(local_url or DEFAULT_LOCAL_URL),
        local_model=str(local_model or DEFAULT_LOCAL_MODEL),
        local_reasoning_effort=_reasoning_effort(local_reasoning),
    )


def select_provider(config: RouteConfig, routing_keys: list[str]) -> str:
    return config.provider


def resolve_executable(value: str) -> str:
    expanded = Path(value).expanduser()
    if expanded.is_absolute():
        return str(expanded)
    return shutil.which(value) or value


def parse_usage_envelope(stdout: str, provider: str = "claude") -> dict | None:
    return agent_session.parse_usage(provider, stdout)


def parse_result_envelope(provider: str, stdout: str) -> dict | None:
    return agent_session.parse_headless(provider, stdout)


def invoke_local(*, prompt: str, model: str, url: str, timeout: int,
                 reasoning_effort: str = "high",
                 json_schema: dict | None = None) -> LLMResult:
    """One chat completion from an OpenAI-compatible local server
    (llama-server, Ollama, LM Studio). It has no tools."""
    started = time.time()
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.2,
        "max_tokens": LOCAL_MAX_TOKENS,
        "chat_template_kwargs": {
            "enable_thinking": _reasoning_effort(reasoning_effort) != "off"},
    }
    if json_schema is not None:
        body["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "result", "schema": json_schema}}
    request = urllib.request.Request(
        url.rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode()
        doc = json.loads(raw)
        text = doc["choices"][0]["message"].get("content") or ""
        usage = doc.get("usage") if isinstance(doc.get("usage"), dict) else {}
        rc, stderr = 0, ""
    except (OSError, ValueError, KeyError, IndexError, TypeError,
            AttributeError) as exc:
        raw, text, usage, rc = "", "", {}, 1
        stderr = f"local model at {url} failed: {exc}"
    text = re.sub(r"(?s)<think>.*?</think>", "", text).strip()
    return LLMResult(
        provider="local", model=model, rc=rc, stdout=raw, stderr=stderr,
        wall_ms=int((time.time() - started) * 1000),
        tokens_in=int(usage.get("prompt_tokens") or 0),
        tokens_out=int(usage.get("completion_tokens") or 0),
        cost_usd=0.0, usage_source="local", session_id=None,
        result_text=text, usable=rc == 0 and bool(text),
    )


def invoke_local(*, prompt: str, model: str, url: str, timeout: int,
                 reasoning_effort: str = "high",
                 json_schema: dict | None = None) -> LLMResult:
    """One chat completion from an OpenAI-compatible local server
    (llama-server, Ollama, LM Studio). It has no tools."""
    started = time.time()
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.2,
        "max_tokens": LOCAL_MAX_TOKENS,
        "chat_template_kwargs": {
            "enable_thinking": _reasoning_effort(reasoning_effort) != "off"},
    }
    if json_schema is not None:
        body["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "result", "schema": json_schema}}
    request = urllib.request.Request(
        url.rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode()
        doc = json.loads(raw)
        text = doc["choices"][0]["message"].get("content") or ""
        usage = doc.get("usage") if isinstance(doc.get("usage"), dict) else {}
        rc, stderr = 0, ""
    except (OSError, ValueError, KeyError, IndexError, TypeError,
            AttributeError) as exc:
        raw, text, usage, rc = "", "", {}, 1
        stderr = f"local model at {url} failed: {exc}"
    text = re.sub(r"(?s)<think>.*?</think>", "", text).strip()
    return LLMResult(
        provider="local", model=model, rc=rc, stdout=raw, stderr=stderr,
        wall_ms=int((time.time() - started) * 1000),
        tokens_in=int(usage.get("prompt_tokens") or 0),
        tokens_out=int(usage.get("completion_tokens") or 0),
        cost_usd=0.0, usage_source="local", session_id=None,
        result_text=text, usable=rc == 0 and bool(text),
    )


def invoke(*, provider: str, prompt: str, model: str, run_dir: Path,
           timeout: int, run: Callable, claude_bin: str | None = None,
           droid_bin: str | None = None,
           copilot_bin: str | None = None,
           reasoning_effort: str = "high",
           disable_tools: bool = False,
           json_schema: dict | None = None,
           extra_dirs: list[Path] | None = None,
           tag: str = "assistant",
           local_url: str = DEFAULT_LOCAL_URL) -> LLMResult:
    started = time.time()
    provider = _provider(provider)
    if provider == "local":
        return invoke_local(prompt=prompt, model=model, url=local_url,
                            timeout=timeout, reasoning_effort=reasoning_effort,
                            json_schema=json_schema)
    if provider == "droid":
        # Explicit binary pre-flight so a missing/non-executable droid produces
        # ONE unambiguous failure row (rc=127) that the cost ledger + the brief's
        # provider-health card can surface — instead of a murky enumeration rc.
        droid_exe = resolve_executable(
            droid_bin or agent_session.binary(agent_session.DROID))
        if not (os.path.isfile(droid_exe) and os.access(droid_exe, os.X_OK)):
            return LLMResult(
                provider="droid", model=model, rc=127, stdout="",
                stderr=f"droid binary not found or not executable: {droid_exe}",
                wall_ms=int((time.time() - started) * 1000),
                tokens_in=0, tokens_out=0, cost_usd=None,
                usage_source="none", session_id=None, result_text="",
                usable=False,
            )
        disabled_tools: list[str] = []
        if disable_tools:
            tools_cmd = agent_session.droid_list_tools_argv(droid_exe, model)
            tools_rc, tools_out, tools_err = run(
                tools_cmd,
                timeout=min(timeout, 30),
                merge_bedrock=False,
            )
            try:
                tools = json.loads(tools_out)
                if tools_rc != 0 or not isinstance(tools, list):
                    raise ValueError
                # Disable EVERY enumerated tool id, not only currentlyAllowed==True:
                # a tool that is not currently allowed at enumeration time can still
                # be enabled at exec time (a fresh profile, an MCP server that
                # materializes on launch), and a read-only judge over untrusted
                # content must have ALL tools off. Crucially, FAIL CLOSED on ANY
                # entry we cannot name (not a dict, or a missing/empty id): we
                # cannot pass an un-nameable tool to --disabled-tools, so a
                # partially-malformed list must refuse, not launch with a subset.
                disabled_tools = []
                for tool in tools:
                    tid = tool.get("id") if isinstance(tool, dict) else None
                    if not (isinstance(tid, str) and tid):
                        raise ValueError
                    disabled_tools.append(tid)
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
            # FAIL CLOSED: if enumeration yielded no ids to disable, we cannot
            # prove tools are off. Launching exec anyway would carry neither
            # --disabled-tools nor --auto and run under droid's SESSION DEFAULT
            # (config sets autonomyLevel=high) — so a "read-only" judge could act
            # on untrusted event/transcript text. Refuse instead of running
            # unrestricted; the caller degrades (visible via provider health).
            if not disabled_tools:
                return LLMResult(
                    provider="droid", model=model, rc=126,
                    stdout=tools_out or "", stderr=(tools_err or "") +
                    " (refusing: could not build a tools-disabled droid command; "
                    "failing closed rather than run under the session autonomy "
                    "default)",
                    wall_ms=int((time.time() - started) * 1000),
                    tokens_in=0, tokens_out=0, cost_usd=None,
                    usage_source="none", session_id=None, result_text="",
                    usable=False,
                )
        cmd = agent_session.headless_argv(
            provider, binary_path=droid_exe, model=model, run_dir=run_dir,
            disable_tools=disable_tools,
            reasoning_effort=_reasoning_effort(reasoning_effort), tag=tag,
            disabled_tools=disabled_tools)
    else:
        explicit = claude_bin if provider == "claude" else copilot_bin
        exe = resolve_executable(explicit or agent_session.binary(provider))
        if provider == "copilot" and not (
                os.path.isfile(exe) and os.access(exe, os.X_OK)):
            return LLMResult(
                provider=provider, model=model, rc=127, stdout="",
                stderr=f"copilot binary not found or not executable: {exe}",
                wall_ms=int((time.time() - started) * 1000),
                tokens_in=0, tokens_out=0, cost_usd=None,
                usage_source="none", session_id=None, result_text="",
                usable=False,
            )
        cmd = agent_session.headless_argv(
            provider, binary_path=exe, model=model, run_dir=run_dir,
            disable_tools=disable_tools, json_schema=json_schema,
            extra_dirs=extra_dirs, tag=tag,
            reasoning_effort=_reasoning_effort(reasoning_effort))

    rc, stdout, stderr = run(
        cmd,
        input_text=agent_session.headless_prompt(provider, prompt, json_schema),
        timeout=timeout,
        merge_bedrock=agent_session.merges_bedrock(provider),
    )
    wall_ms = int((time.time() - started) * 1000)
    parsed = agent_session.parse_headless(provider, stdout)
    usage = agent_session.parse_usage(provider, stdout)
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
        stderr="\n".join(filter(None, [
            stderr or "", agent_session.headless_errors(provider, stdout)])),
        wall_ms=wall_ms,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost_usd=(usage or {}).get("cost_usd"),
        usage_source=usage_source,
        session_id=(usage or {}).get("session_id"),
        result_text=(parsed or {}).get("result_text") or "",
        usable=rc == 0 and parsed is not None,
    )
