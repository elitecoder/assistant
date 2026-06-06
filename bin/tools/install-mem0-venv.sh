#!/usr/bin/env bash
# install-mem0-venv.sh — (re)build the dedicated venv the mem0 memory tools use.
#
# mem0ai and its deps (chroma, boto3, fastembed) install cleanly only under
# Python 3.12, not the system python3 the tool dispatcher runs. So they live in
# an isolated venv at <repo>/.venv-mem0, and mem0_backend.ensure_venv() re-execs
# the mem0-add / mem0-search CLIs into it on demand. Without this venv the tools
# still work — they fall back to the dependency-free local-jsonl store — but you
# get lexical search, not Bedrock-embedded semantic search.
#
# Idempotent: re-run freely. uv creates the venv and resolves deps; if the venv
# already exists, uv pip install just reconciles it.
#
# Embeddings use AWS Bedrock Titan when the box is authorised (the usual case
# here, via AWS_BEARER_TOKEN_BEDROCK); otherwise the tools fall back to a local
# fastembed model — both are installed below so either path is ready.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
VENV="$REPO_ROOT/.venv-mem0"

if ! command -v uv >/dev/null 2>&1; then
    echo "uv not found. Install it: https://docs.astral.sh/uv/" >&2
    exit 1
fi

echo "Building mem0 venv at $VENV (Python 3.12)…"
uv venv --python 3.12 "$VENV"
# shellcheck disable=SC1091
source "$VENV/bin/activate"
uv pip install mem0ai boto3 chromadb fastembed pytest

echo "✅ mem0 venv ready. Verify:"
echo "   $VENV/bin/python -c 'import mem0; print(mem0.__version__)'"
echo "   python3 $REPO_ROOT/bin/tools/mem0-search.py --query lesson --limit 3"
