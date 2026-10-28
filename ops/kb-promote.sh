#!/usr/bin/env bash
# Weekly promotion of VAULT_CANDIDATES.md into a vault branch (never main).
# Usage: ops/kb-promote.sh   (the systemd unit runs it; manual runs should use systemd-run, see README)
set -euo pipefail
KB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$KB_DIR"
export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:$PATH"
exec uv run --quiet python ops/kb_promote.py "$@"
