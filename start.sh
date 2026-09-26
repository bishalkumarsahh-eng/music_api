#!/usr/bin/env bash
set -euo pipefail

POT_HOST="${POT_HOST:-127.0.0.1}"
POT_PORT="${POT_PORT:-4416}"

if [ -f ".bgutil-provider/server/build/main.js" ]; then
  echo "Starting bgutil PO token provider on http://${POT_HOST}:${POT_PORT}..."
  node .bgutil-provider/server/build/main.js --host "$POT_HOST" --port "$POT_PORT" >/tmp/bgutil-provider.log 2>&1 &
  POT_PID=$!
  trap 'kill "$POT_PID" 2>/dev/null || true' EXIT

  READY=0
  for i in $(seq 1 30); do
    if ! kill -0 "$POT_PID" 2>/dev/null; then
      echo "ERROR: bgutil PO token provider exited."
      cat /tmp/bgutil-provider.log || true
      exit 1
    fi
    if (echo >/dev/tcp/"$POT_HOST"/"$POT_PORT") >/dev/null 2>&1; then
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

  echo "bgutil PO token provider is ready at http://${POT_HOST}:${POT_PORT}."
  echo "PO token mode: YOUTUBE_FETCH_POT=${YOUTUBE_FETCH_POT:-always}"
else
  echo "ERROR: bgutil PO token provider was not built."
  exit 1
fi

export POT_PROVIDER_BASE_URL="${POT_PROVIDER_BASE_URL:-http://${POT_HOST}:${POT_PORT}}"
export YOUTUBE_FETCH_POT="${YOUTUBE_FETCH_POT:-always}"

exec uvicorn app:app --host 0.0.0.0 --port "${PORT:-8000}"
