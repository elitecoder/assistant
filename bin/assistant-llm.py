#!/usr/bin/env python3
"""Configure the Assistant's LLM provider: claude, copilot, droid, or local."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
import tempfile
from pathlib import Path

BIN = Path(__file__).resolve().parent
if str(BIN) not in sys.path:
    sys.path.insert(0, str(BIN))

import llm_runner  # noqa: E402

FEATURES = (
    "triage",
    "observer",
    "observer_audit",
    "strategist",
    "narrator",
    "lessons",
    "slack_judge",
    "comms",
)


def load_document(path: Path) -> dict:
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return raw


def write_document(path: Path, update) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with open(lock_path, "a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        document = load_document(path)
        update(document)
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{path.name}.", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w") as tmp:
                json.dump(document, tmp, indent=2)
                tmp.write("\n")
                tmp.flush()
                os.fsync(tmp.fileno())
            os.replace(tmp_name, path)
        finally:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass
        return document


def feature_pins(path: Path, provider: str) -> list[tuple[str, str]]:
    """(feature, provider) for each feature pinned to a provider other than
    `provider` in the config."""
    features = (load_document(path).get("llm") or {}).get("features") or {}
    return [(name, str(cfg.get("provider"))) for name, cfg in features.items()
            if isinstance(cfg, dict) and cfg.get("provider")
            and cfg.get("provider") != provider]


def set_provider(path: Path, provider: str, feature: str | None) -> None:
    if feature == "comms" and provider not in llm_runner.agent_session.AGENTS:
        raise ValueError("the comms session runs an agent: claude, copilot, or droid")

    def update(document: dict) -> None:
        llm = document.setdefault("llm", {})
        if not isinstance(llm, dict):
            raise ValueError("config key 'llm' must be an object")
        target = llm
        if feature:
            features = llm.setdefault("features", {})
            if not isinstance(features, dict):
                raise ValueError("config key 'llm.features' must be an object")
            target = features.setdefault(feature, {})
            if not isinstance(target, dict):
                raise ValueError(
                    f"config key 'llm.features.{feature}' must be an object")
        target["provider"] = provider
        if provider == "local":
            local = llm.setdefault("local", {})
            if not isinstance(local, dict):
                raise ValueError("config key 'llm.local' must be an object")
            local.setdefault("url", llm_runner.DEFAULT_LOCAL_URL)
            local.setdefault("model", llm_runner.DEFAULT_LOCAL_MODEL)
            local.setdefault("reasoning_effort", "high")
            return
        droid = llm.setdefault("droid", {})
        if not isinstance(droid, dict):
            raise ValueError("config key 'llm.droid' must be an object")
        droid.setdefault("bin", llm_runner.agent_session.binary(
            llm_runner.agent_session.DROID))
        droid.setdefault("model", "glm-5.2")
        droid.setdefault("reasoning_effort", "high")

    write_document(path, update)


def inherit_provider(path: Path, feature: str) -> None:
    def update(document: dict) -> None:
        llm = document.get("llm")
        if not isinstance(llm, dict):
            return
        features = llm.get("features")
        if not isinstance(features, dict):
            return
        target = features.get(feature)
        if not isinstance(target, dict):
            return
        target.pop("provider", None)
        if not target:
            features.pop(feature, None)
        if not features:
            llm.pop("features", None)

    write_document(path, update)


def status(path: Path) -> dict:
    document = load_document(path)
    llm = document.get("llm") if isinstance(document.get("llm"), dict) else {}
    # Default claude — fail-closed to the always-present agent (mirrors
    # llm_runner.RouteConfig). Droid is opt-in.
    # Coerce through _provider() so status reflects actual routing, not raw
    # config text (e.g. a stale "canary" value shows as "claude").
    global_provider = llm_runner._provider(
        llm.get("provider", "claude"))
    features = {}
    for feature in FEATURES:
        route = llm_runner.load_route_config(path, feature)
        configured = llm_runner.load_route_config(path, feature, env={})
        prefix = feature.upper()
        environment_overrides = {
            key: value for key in (
                f"{prefix}_LLM_PROVIDER",
                f"{prefix}_DROID_MODEL",
                f"{prefix}_DROID_REASONING_EFFORT",
                f"{prefix}_LOCAL_MODEL",
                f"{prefix}_LOCAL_REASONING_EFFORT",
                f"{prefix}_COPILOT_MODEL",
                "COPILOT_MODEL",
                "ASSISTANT_LOCAL_LLM_URL",
                llm_runner.agent_session.bin_env_var(llm_runner.agent_session.DROID),
            ) if (value := os.environ.get(key)) is not None
        }
        features[feature] = {
            "provider": (llm_runner.agent_session.warm_session_agent() if feature == "comms"
                         else route.provider),
            "configured_provider": configured.provider,
            "droid_model": route.droid_model,
            "droid_reasoning_effort": route.droid_reasoning_effort,
            "copilot_model": route.copilot_model,
            "local_url": route.local_url,
            "local_model": route.local_model,
            "local_reasoning_effort": route.local_reasoning_effort,
            "environment_override": environment_overrides.get(
                f"{prefix}_LLM_PROVIDER"),
            "environment_overrides": environment_overrides,
        }
    return {
        "config_path": str(path),
        "global_provider": global_provider,
        "features": features,
    }


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Choose Claude, Copilot, Factory Droid, or a local model for Assistant LLM work.")
    commands = result.add_subparsers(dest="command", required=True)
    show = commands.add_parser("status")
    show.add_argument("--json", action="store_true")

    set_cmd = commands.add_parser("set")
    set_cmd.add_argument("provider", choices=llm_runner.PROVIDERS)
    set_cmd.add_argument("--feature", choices=FEATURES)

    inherit = commands.add_parser("inherit")
    inherit.add_argument("--feature", choices=FEATURES, required=True)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    path = llm_runner.config_path()
    try:
        if args.command == "status":
            current = status(path)
            if args.json:
                print(json.dumps(current, indent=2))
            else:
                print(
                    f"Configured global provider: "
                    f"{current['global_provider']}")
                for name, feature in current["features"].items():
                    detail = feature["provider"]
                    if name == "comms":
                        print(f"{name}: {detail} (interactive session; its own launch settings)")
                        continue
                    if detail == "droid":
                        detail += f" ({feature['droid_model']}, " \
                            f"{feature['droid_reasoning_effort']} reasoning)"
                    if detail == "copilot":
                        detail += f" ({feature['copilot_model'] or 'Copilot default model'})"
                    if detail == "local":
                        detail += f" ({feature['local_model']} at " \
                            f"{feature['local_url']}, " \
                            f"{feature['local_reasoning_effort']} reasoning)"
                    print(f"{name}: {detail}")
                    for key, value in feature[
                        "environment_overrides"
                    ].items():
                        print(f"  overridden by {key}={value}")
                print(f"Config: {current['config_path']}")
            return 0
        if args.command == "set":
            set_provider(
                path, args.provider, args.feature)
            if not args.feature:
                dispatch = (load_document(path).get("dispatch") or {}).get("agent")
                if dispatch and dispatch != args.provider:
                    print(f"note: task dispatch stays on {dispatch} (dispatch.agent); "
                          f"remove it to follow {args.provider}", file=sys.stderr)
                for name, pinned in feature_pins(path, args.provider):
                    print(f"note: {name} stays on {pinned} (llm.features.{name}); "
                          f"run `assistant-llm.py inherit --feature {name}` to follow "
                          f"{args.provider}", file=sys.stderr)
            if args.provider == llm_runner.agent_session.COPILOT:
                failed = llm_runner.agent_session.install_copilot_hooks()
                if failed:
                    print(f"warning: cmux Copilot hooks not installed "
                          f"({failed}); Copilot sessions won't show on the "
                          f"dashboard", file=sys.stderr)
        else:
            inherit_provider(path, args.feature)
        return main(["status"])
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
