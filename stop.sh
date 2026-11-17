#!/usr/bin/env bash
# Stop only the demo generator and demo Alloy (identified by their pid files).
cd "$(dirname "$0")"
for name in alloy generator; do
  pidf="data/$name.pid"
  if [[ -f "$pidf" ]] && kill -0 "$(cat "$pidf")" 2>/dev/null; then
    kill "$(cat "$pidf")" && echo "stopped $name (pid $(cat "$pidf"))"
  else
    echo "$name not running"
  fi
  rm -f "$pidf"
done
