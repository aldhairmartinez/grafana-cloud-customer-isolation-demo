#!/usr/bin/env bash
# Start the generator and a dedicated Alloy in the background (they survive closing the terminal).
set -euo pipefail
cd "$(dirname "$0")"
DIR="$(pwd)"

[[ -f .env ]] || { echo "Missing .env - copy .env.example and fill it in" >&2; exit 1; }
chmod 600 .env
set -a; source ./.env; set +a
for v in GC_WRITE_TOKEN GC_METRICS_PUSH_URL GC_METRICS_USER GC_LOGS_PUSH_URL GC_LOGS_USER; do
  [[ -n "${!v:-}" && "${!v}" != *"<"* ]] || { echo "$v is not set in .env" >&2; exit 1; }
done
command -v alloy >/dev/null || { echo "alloy not found on PATH" >&2; exit 1; }

export DEMO_METRICS_PORT="${DEMO_METRICS_PORT:-9477}"
export DEMO_LOG_DIR="$DIR/data/logs"
ALLOY_UI="${DEMO_ALLOY_LISTEN:-127.0.0.1:12399}"
mkdir -p data/logs data/alloy

running() { [[ -f "$1" ]] && kill -0 "$(cat "$1")" 2>/dev/null; }

if running data/generator.pid; then echo "generator already running (pid $(cat data/generator.pid))"; else
  nohup python3 -u generator.py >> data/generator.out 2>&1 &
  echo $! > data/generator.pid; echo "generator started (pid $!)"
fi

if running data/alloy.pid; then echo "alloy already running (pid $(cat data/alloy.pid))"; else
  nohup alloy run --server.http.listen-addr="$ALLOY_UI" --storage.path="$DIR/data/alloy" \
    --disable-reporting "$DIR/config.alloy" >> data/alloy.out 2>&1 &
  echo $! > data/alloy.pid; echo "alloy started (pid $!) - UI http://$ALLOY_UI"
fi
