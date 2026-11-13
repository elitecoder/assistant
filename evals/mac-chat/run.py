"""Ask Assistant for Mac the operator's real questions on several model setups and save the answers for grading.

Usage: python3 evals/mac-chat/run.py <AssistantBar binary> [config ...]
Questions come from ~/.assistant/evals/mac-chat/refs.json ({question: {"when": ISO date, "answer": reference}}),
which stays on the operator's machine. Answers go to ~/.assistant/evals/mac-chat/results-<config>.jsonl.
"""
import concurrent.futures as cf
import json
import os
import subprocess
import sys
import time

CONFIGS = {
    "haiku": {"ASSISTANT_MODEL": "haiku", "ASSISTANT_EFFORT": ""},
    "haiku-high": {"ASSISTANT_MODEL": "haiku", "ASSISTANT_EFFORT": "high"},
    "sonnet": {"ASSISTANT_MODEL": "sonnet", "ASSISTANT_EFFORT": ""},
    "opus": {"ASSISTANT_MODEL": "opus", "ASSISTANT_EFFORT": ""},
}
HOME = os.path.expanduser("~/.assistant/evals/mac-chat")


def ask(binary, question, when, config):
    started = time.time()
    run = subprocess.run([binary, "--ask", f"(Asked on {when[:10]}.) {question}"], capture_output=True, text=True,
                         env={**os.environ, **CONFIGS[config]}, timeout=900)
    lines = run.stdout.strip().splitlines()
    cost = next((l for l in reversed(lines) if l.startswith("($")), "").strip("()$")
    answer = "\n".join(l for l in lines if not l.startswith("($") and not l.startswith("  · "))
    return {"q": question, "config": config, "answer": answer, "cost": cost, "seconds": round(time.time() - started)}


def main():
    binary, configs = sys.argv[1], sys.argv[2:] or ["haiku-high"]
    refs = json.load(open(os.path.join(HOME, "refs.json")))
    for config in configs:
        with open(os.path.join(HOME, f"results-{config}.jsonl"), "w") as out, cf.ThreadPoolExecutor(6) as pool:
            for r in pool.map(lambda q: ask(binary, q, refs[q]["when"], config), refs):
                out.write(json.dumps(r) + "\n")
                print(config, r["seconds"], "s", r["cost"], "|", r["q"][:60], flush=True)


if __name__ == "__main__":
    main()
