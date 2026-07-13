#!/usr/bin/env python3
"""Configure the Assistant's headless LLM provider."""
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


def set_provider(path: Path, provider: str, feature: str | None,
                 percent: int | None) -> None:
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
        if provider == "canary":
            target["droid_canary_percent"] = percent
        else:
            target.pop("droid_canary_percent", None)
        droid = llm.setdefault("droid", {})
        if not isinstance(droid, dict):
            raise ValueError("config key 'llm.droid' must be an object")
        droid.setdefault("bin", str(Path.home() / ".local/bin/droid"))
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
        target.pop("droid_canary_percent", None)
        if not target:
            features.pop(feature, None)
        if not features:
            llm.pop("features", None)

    write_document(path, update)


def status(path: Path) -> dict:
    document = load_document(path)
    llm = document.get("llm") if isinstance(document.get("llm"), dict) else {}
    global_provider = llm.get("provider", "droid")
    features = {}
    for feature in FEATURES:
        route = llm_runner.load_route_config(path, feature)
        configured = llm_runner.load_route_config(path, feature, env={})
        prefix = feature.upper()
        environment_overrides = {
            key: value for key in (
                f"{prefix}_LLM_PROVIDER",
                f"{prefix}_DROID_CANARY_PERCENT",
                f"{prefix}_DROID_MODEL",
                f"{prefix}_DROID_REASONING_EFFORT",
                "DROID_BIN",
            ) if (value := os.environ.get(key)) is not None
        }
        features[feature] = {
            "provider": route.provider,
            "configured_provider": configured.provider,
            "droid_canary_percent": route.droid_canary_percent,
            "droid_model": route.droid_model,
            "droid_reasoning_effort": route.droid_reasoning_effort,
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
        description="Choose Claude or Factory Droid for Assistant LLM work.")
    commands = result.add_subparsers(dest="command", required=True)
    show = commands.add_parser("status")
    show.add_argument("--json", action="store_true")

    set_cmd = commands.add_parser("set")
    set_cmd.add_argument("provider", choices=("claude", "droid", "canary"))
    set_cmd.add_argument("--feature", choices=FEATURES)
    set_cmd.add_argument("--percent", type=int)

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
                    if detail == "droid":
                        detail += f" ({feature['droid_model']}, " \
                            f"{feature['droid_reasoning_effort']} reasoning)"
                    elif detail == "canary":
                        detail += \
                            f" ({feature['droid_canary_percent']}% Droid)"
                    print(f"{name}: {detail}")
                    for key, value in feature[
                        "environment_overrides"
                    ].items():
                        print(f"  overridden by {key}={value}")
                print(f"Config: {current['config_path']}")
            return 0
        if args.command == "set":
            if args.provider == "canary" and (
                args.percent is None or not 0 <= args.percent <= 100
            ):
                parser().error(
                    "set canary requires --percent between 0 and 100")
            set_provider(
                path, args.provider, args.feature, args.percent)
        else:
            inherit_provider(path, args.feature)
        return main(["status"])
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
