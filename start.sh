#!/usr/bin/env bash
set -euo pipefail

if [ -f ".bgutil-provider/server/build/main.js" ]; then
  echo "Starting bgutil PO token provider on http://127.0.0.1:4416..."
  node .bgutil-provider/server/build/main.js --host 127.0.0.1 --port 4416 >/tmp/bgutil-provider.log 2>&1 &
  POT_PID=$!
  trap 'kill "$POT_PID" 2>/dev/null || true' EXIT

  READY=0
  for i in $(seq 1 30); do
    if ! kill -0 "$POT_PID" 2>/dev/null; then
      echo "ERROR: bgutil PO token provider exited."
      cat /tmp/bgutil-provider.log || true
      exit 1
    fi
    if (echo >/dev/tcp/127.0.0.1/4416) >/dev/null 2>&1; then
      READY=1
      break
    fi
    sleep 1
  done

  if [ "$READY" -ne 1 ]; then
    echo "ERROR: bgutil PO token provider did not become ready."
    cat /tmp/bgutil-provider.log || true
    exit 1
  fi

  echo "bgutil PO token provider is ready."
else
  echo "ERROR: bgutil PO token provider was not built."
  exit 1
fi

exec uvicorn app:app --host 0.0.0.0 --port "${PORT:-8000}"
