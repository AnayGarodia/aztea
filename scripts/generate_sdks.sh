#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OPENAPI_PATH="$ROOT_DIR/openapi.json"
HOST="127.0.0.1"
PORT="${AZTEA_SDK_PORT:-8000}"
BASE_URL="http://${HOST}:${PORT}"

export API_KEY="${API_KEY:-aztea-sdk-master-key}"
export SERVER_BASE_URL="${SERVER_BASE_URL:-$BASE_URL}"

cleanup() {
  if [[ -n "${SERVER_PID:-}" ]]; then
    kill "${SERVER_PID}" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

cd "$ROOT_DIR"

python -m uvicorn server:app --host "$HOST" --port "$PORT" --log-level warning >/tmp/aztea-sdk-openapi.log 2>&1 &
SERVER_PID=$!

for _ in {1..60}; do
  if curl --silent --fail "$BASE_URL/health" >/dev/null 2>&1; then
    break
  fi
  sleep 0.25
done

curl --silent --fail "$BASE_URL/openapi.json" -o "$OPENAPI_PATH"

cd "$ROOT_DIR/sdks/typescript"
npm install --silent
npx openapi-typescript "$OPENAPI_PATH" -o "src/generated/types.ts"
npm run typecheck
npm run build
npm test

cd "$ROOT_DIR"
python -m pip install --quiet mypy
python -m pip install --quiet -e "$ROOT_DIR/sdks/python"
cd "$ROOT_DIR/sdks/python"
python -m mypy --strict aztea
