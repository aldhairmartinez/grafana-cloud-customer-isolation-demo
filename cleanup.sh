#!/usr/bin/env bash
# Stop the demo processes and delete LOCAL state (logs, Alloy WAL and positions).
# Does not touch Grafana Cloud; see grafana/isolation.py cleanup for that.
cd "$(dirname "$0")"
./stop.sh
rm -rf data/
echo "local demo state removed (.env kept)"
