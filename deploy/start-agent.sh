#!/usr/bin/env bash
set -euo pipefail
: "${RP1_ROS_SETUP:?set ROS setup.bash path}"
: "${RP1_MONITOR_PYTHON:?set robot Python executable}"
: "${RP1_ROBOT_ID:?set registered robot id}"
: "${RP1_MONITOR_STATE:?set dedicated state directory}"
# ROS setup scripts may reference unset variables.
set +u
source "$RP1_ROS_SETUP"
if [[ -n "${RP1_WORKSPACE_SETUP:-}" ]]; then source "$RP1_WORKSPACE_SETUP"; fi
set -u
args=(agent --source ros2 --robot-id "$RP1_ROBOT_ID" --state-dir "$RP1_MONITOR_STATE" --port "${RP1_MONITOR_PORT:-8891}" --cache-mib "${RP1_MONITOR_CACHE_MIB:-1024}" --reserve-mib "${RP1_MONITOR_RESERVE_MIB:-256}")
if [[ -n "${RP1_MONITOR_PROFILE:-}" ]]; then args+=(--profile "$RP1_MONITOR_PROFILE"); fi
if [[ -n "${RP1_ROBOPI_TUI_EXECUTOR:-}" ]]; then args+=(--robopi-tui-executor "$RP1_ROBOPI_TUI_EXECUTOR"); fi
exec "$RP1_MONITOR_PYTHON" -m rp1_monitor "${args[@]}"
