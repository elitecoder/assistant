#!/usr/bin/env python3
"""verify-spawn-submitted.py — confirm a spawned claude session actually
submitted the prompt (vs. having it staged in the input box but never sent).

Mechanical only. The Assistant prompt decides what to do based on the
return code (log dispatch / try recovery / surface awaiting card).

Usage:
  verify-spawn-submitted.py --cwd ~/dev --prompt-file /path/to/prompt.md \
                            [--window-sec 90]

Exit code: 0 if submitted, 1 if not.
"""
import argparse
import datetime
import glob
import json
import os
import sys


def newest_jsonl(project_dir, window_sec):
    cutoff = datetime.datetime.now(datetime.UTC).timestamp() - window_sec
    candidates = []
    for f in glob.glob(os.path.join(project_dir, "*.jsonl")):
        try:
            st = os.stat(f)
        except FileNotFoundError:
            continue
        if st.st_mtime >= cutoff:
            candidates.append((st.st_mtime, f))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][1]


def has_user_submission(jsonl_path, signature):
    try:
        with open(jsonl_path) as f:
            for line in f:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                msg = d.get("message")
                if d.get("type") != "user" or not isinstance(msg, dict):
                    continue
                if msg.get("role") != "user":
                    continue
                content = msg.get("content", "")
                if isinstance(content, list):
                    content = " ".join(
                        str(x.get("text", "") if isinstance(x, dict) else x) for x in content
                    )
                if signature in str(content):
                    return True
    except FileNotFoundError:
        return False
    return False


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--cwd", required=True)
    p.add_argument("--prompt-file", required=True)
    p.add_argument("--window-sec", type=int, default=90)
    args = p.parse_args()

    cwd_real = os.path.realpath(os.path.expanduser(args.cwd))
    slug = cwd_real.replace("/", "-")
    project_dir = os.path.expanduser(f"~/.claude/projects/{slug}")

    latest = newest_jsonl(project_dir, args.window_sec)
    if not latest:
        print("no_recent_jsonl", file=sys.stderr)
        sys.exit(1)

    sig = args.prompt_file[:60]
    if has_user_submission(latest, sig):
        print(latest)
        sys.exit(0)
    print(f"prompt_signature_not_in:{latest}", file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()
