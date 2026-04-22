#!/usr/bin/env bash
# Reproducible test run: use repo .venv if present, same as CI (Python 3.11+).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
if [[ -d "$ROOT/.venv" ]]; then
  # shellcheck source=/dev/null
  source "$ROOT/.venv/bin/activate"
fi
export API_KEY="${API_KEY:-test-master-key}"
exec python -m pytest -q tests "$@"
