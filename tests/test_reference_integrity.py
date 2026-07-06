"""Reference-integrity gate — every script path the system points at must exist.

Born from the 6bfa86c incident: bin/heartbeat-write.py and bin/spawn-assistant.sh
were deleted, but the Slack comms warm prompt + comms_lib.Paths kept referencing
them, so the "restart Assistant" recovery surface called missing files. This
test is the mechanical gate that catches the NEXT such orphaning:
  - every `script` in bin/tools-manifest.json exists,
  - every bin/*.py|*.sh path named in prompts/*.md exists,
  - every *.py|*.sh path built into comms_lib.Paths exists.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Paths that legitimately name a script in a NON-invocation context (docs of
# history, "do not run" examples). None today; add here with a reason if needed.
ALLOWED_MISSING: set[str] = set()

# Prompt files that are HISTORICAL RECORDS, not instruction sets the warm
# session executes. INCIDENTS.md documents past bugs (and names now-deleted
# scripts as part of that history) — it must not be held to the
# referenced-script-exists rule. Instruction prompts (the mutation tables the
# session actually runs) ARE checked.
NON_INSTRUCTION_PROMPTS = {"INCIDENTS.md"}


def test_tools_manifest_scripts_exist():
    manifest = json.loads((REPO / "bin" / "tools-manifest.json").read_text())
    missing = []
    for tool in manifest:
        script = tool.get("script")
        if not script:
            continue
        if not (REPO / script).is_file() and script not in ALLOWED_MISSING:
            missing.append(f"{tool.get('name')} → {script}")
    assert not missing, "tools-manifest.json references missing scripts:\n  " + "\n  ".join(missing)


def test_prompt_script_references_exist():
    """Every bin/…(.py|.sh) path mentioned in any prompt must exist. Prompts tell
    the warm session which scripts to run; a dead path = a broken affordance."""
    pat = re.compile(r'bin/[A-Za-z0-9_./-]+\.(?:py|sh)')
    missing = []
    for prompt in (REPO / "prompts").glob("*.md"):
        if prompt.name in NON_INSTRUCTION_PROMPTS:
            continue
        text = prompt.read_text()
        for m in sorted(set(pat.findall(text))):
            if not (REPO / m).is_file() and m not in ALLOWED_MISSING:
                missing.append(f"{prompt.name}: {m}")
    assert not missing, "prompts reference missing scripts:\n  " + "\n  ".join(missing)


def test_comms_lib_paths_scripts_exist():
    """Every .py/.sh path built into comms_lib.Paths (curator, etc.) must exist.
    Regression guard for the heartbeat-write/spawn-assistant deletion."""
    import sys
    sys.path.insert(0, str(REPO / "bin"))
    import comms_lib
    p = comms_lib.Paths.from_env({"HOME": "/tmp", "COMMS_BIN_DIR": str(REPO / "bin")})
    missing = []
    for field_name in p.__dataclass_fields__:
        val = getattr(p, field_name)
        if isinstance(val, Path) and val.suffix in (".py", ".sh"):
            if not val.is_file():
                missing.append(f"Paths.{field_name} → {val}")
    assert not missing, "comms_lib.Paths references missing scripts:\n  " + "\n  ".join(missing)
