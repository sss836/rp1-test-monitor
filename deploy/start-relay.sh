#!/usr/bin/env bash
set -euo pipefail
: "${RP1_RELAY_PYTHON:?set upper-computer Python executable}"
: "${RP1_ROBOT_ID:?set registered robot id}"
: "${RP1_COLLECTOR_URL:?set loopback SSH tunnel endpoint}"
: "${RP1_MONITOR_BACKEND:?set HTTPS central endpoint}"
: "${RP1_DEVICE_TOKEN_FILE:?set private token file on the upper computer}"
: "${RP1_RELAY_STATE:?set dedicated upper-computer state directory}"
args=(relay --source "${RP1_SOURCE_KIND:-ros2}" --robot-id "$RP1_ROBOT_ID" --collector "$RP1_COLLECTOR_URL" --backend "$RP1_MONITOR_BACKEND" --token-file "$RP1_DEVICE_TOKEN_FILE" --state-dir "$RP1_RELAY_STATE" --port "${RP1_RELAY_PORT:-8892}" --cache-mib "${RP1_RELAY_CACHE_MIB:-1024}" --reserve-mib "${RP1_RELAY_RESERVE_MIB:-256}")
if [[ -n "${RP1_MONITOR_CA_FILE:-}" ]]; then args+=(--ca-file "$RP1_MONITOR_CA_FILE"); fi
exec "$RP1_RELAY_PYTHON" -m rp1_monitor "${args[@]}"
