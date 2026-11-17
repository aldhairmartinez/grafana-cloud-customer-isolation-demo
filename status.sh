#!/usr/bin/env bash
cd "$(dirname "$0")"
for name in generator alloy; do
  pidf="data/$name.pid"
  if [[ -f "$pidf" ]] && kill -0 "$(cat "$pidf")" 2>/dev/null; then echo "$name: running (pid $(cat "$pidf"))"; else echo "$name: stopped"; fi
done
echo "log file: $(wc -l < data/logs/publishing-api.log 2>/dev/null || echo 0) lines"
echo "recent alloy warnings/errors:"; grep -E 'level=(error|warn)' data/alloy.out 2>/dev/null | tail -5 || true
