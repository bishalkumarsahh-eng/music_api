#!/usr/bin/env bash
set -euo pipefail

if [ -f ".bgutil-provider/server/build/main.js" ]; then
  echo "Starting bgutil PO token provider on 127.0.0.1:4416..."
  node .bgutil-provider/server/build/main.js --host 127.0.0.1 --port 4416 >/tmp/bgutil-provider.log 2>&1 &
  POT_PID=$!
  trap 'kill "$POT_PID" 2>/dev/null || true' EXIT
else
  echo "WARNING: bgutil PO token provider was not built; continuing without it."
fi

exec uvicorn app:app --host 0.0.0.0 --port "${PORT:-8000}"
