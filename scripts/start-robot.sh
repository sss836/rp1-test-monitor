#!/usr/bin/env bash
# New independent installation only; never substitutes the existing 20261025-r1 launcher.
set -eo pipefail
monitor_base=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
if [[ "${1:-}" != --restricted ]]; then
  exec env -u PYTHONPATH taskset --cpu-list 1-3,7 bash "$monitor_base/start-observer.sh" --restricted
fi
cd "$monitor_base"
exec 9>"$monitor_base/observer.lock"
flock --nonblock 9 || { echo '该监听已运行，未启动第二份。' >&2; exit 1; }
# Standard-library preparation inherits the CPU mask. No ROS node starts here.
observer_settings=$(.venv/bin/python -B - <<'PY'
import json, os, shlex, socket
from pathlib import Path
assert os.sched_getaffinity(0) == {1,2,3,7}, 'CPU限制不匹配，拒绝启动'
with socket.socket() as probe:
    if probe.connect_ex(('127.0.0.1',8891)) == 0:
        raise SystemExit('8891已被占用，未操作原进程。')
config=json.loads(Path('observer-config.json').read_text())
for key,value in config.items():
    print('observer_'+key+'='+shlex.quote(value))
PY
)
eval "$observer_settings"
source "$observer_ros_setup"
export PYTHONDONTWRITEBYTECODE=1
if [[ -n "$observer_rmw_implementation" ]]; then export RMW_IMPLEMENTATION="$observer_rmw_implementation"; fi
if [[ -n "$observer_fastdds_xml" ]]; then export RMW_FASTRTPS_USE_QOS_FROM_XML="$observer_fastdds_xml"; fi
if [[ -n "$observer_ros_domain_id" ]]; then export ROS_DOMAIN_ID="$observer_ros_domain_id"; fi
if [[ -n "$observer_dds_profile" ]]; then
  export FASTRTPS_DEFAULT_PROFILES_FILE="$observer_dds_profile"
fi
if [[ -z "$observer_tui_executor" ]]; then
  for candidate in /opt/roboparty/lib/robopi-tui/loader/plugin_executor.py \
    /opt/roboparty/lib/robopi-tui-source/*/loader/plugin_executor.py; do
    if [[ -f "$candidate" ]]; then
      if [[ -n "$observer_tui_executor" ]]; then echo '发现多个TUI版本，请在observer-config.json明确指定。' >&2; exit 1; fi
      observer_tui_executor=$candidate
    fi
  done
fi
args=(agent --source ros2 --robot-id "$observer_robot_id" --profile "$monitor_base/profile.json"
  --state-dir "$monitor_base/state" --cache-mib 128 --reserve-mib 512 --port 8891)
if [[ -n "$observer_tui_executor" ]]; then args+=(--robopi-tui-executor "$observer_tui_executor"); fi
exec nice -n 10 "$monitor_base/.venv/bin/python" -B -m rp1_monitor "${args[@]}"
