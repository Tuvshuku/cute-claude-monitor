#!/usr/bin/env bash
# Runs the collector in a loop, guarding against a second copy starting.
set -euo pipefail

APP_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
cd "$APP_DIR"

LOCK="$APP_DIR/.collector.lock"

if command -v flock >/dev/null 2>&1; then
    exec 9>"$LOCK"
    if ! flock -n 9; then
        echo "collector is already running (lock: $LOCK)" >&2
        exit 1
    fi
fi

exec python3 collector.py --loop
