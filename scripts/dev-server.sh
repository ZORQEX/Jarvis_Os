#!/usr/bin/env bash
# Start/stop an isolated JARVIS core for development and UI tests.
# Usage: scripts/dev-server.sh start|stop <JARVIS_HOME>
set -euo pipefail
cmd=$1; home=$2
pidf="$home/server.pid"
case $cmd in
  start)
    [[ -f $pidf ]] && kill "$(cat "$pidf")" 2>/dev/null && sleep 0.4 || true
    cd "$(dirname "$0")/.."
    JARVIS_HOME="$home" setsid uv run python -m jarvis serve >"$home/server.out" 2>&1 &
    echo $! >"$pidf"
    for _ in $(seq 50); do grep -q "JARVIS WorkOS on" "$home/server.out" 2>/dev/null && break; sleep 0.1; done
    cat "$home/server.out";;
  stop)
    [[ -f $pidf ]] && { kill -- -"$(cat "$pidf")" 2>/dev/null || kill "$(cat "$pidf")" 2>/dev/null || true; rm -f "$pidf"; };;
esac
