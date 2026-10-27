#!/usr/bin/env bash
# Run manually on a new robot after reviewing the scope in docs/GITHUB_QUICKSTART.md.
set -euo pipefail
project_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
monitor_base=${RP1_MONITOR_INSTALL_DIR:-"$HOME/.local/share/rp1-monitor/github"}
robot_id=${RP1_ROBOT_ID:-RP1-ROBOPI2}
ros_setup=${RP1_ROS_SETUP:-/opt/ros/jazzy/setup.bash}
if [[ $EUID == 0 ]]; then echo '请以机器人普通用户安装，不要sudo。' >&2; exit 1; fi
if [[ ! -f "$ros_setup" ]]; then echo '找不到ROS环境，请通过RP1_ROS_SETUP指定现场已有setup.bash。' >&2; exit 1; fi
if [[ -e "$monitor_base" ]]; then echo '目标目录已存在，未覆盖。已有安装请使用原启动器；更新前单独审阅文件差异。' >&2; exit 1; fi
mkdir -m 700 -p "$monitor_base"
mkdir -m 700 "$monitor_base/.tmp" "$monitor_base/.pip-cache"
export TMPDIR="$monitor_base/.tmp"
export PIP_CACHE_DIR="$monitor_base/.pip-cache"
python3 -m venv --system-site-packages --without-pip "$monitor_base/.venv"
if ! "$monitor_base/.venv/bin/python" -m pip --version >/dev/null 2>&1; then
  if ! "$monitor_base/.venv/bin/python" -m ensurepip >"$monitor_base/.tmp/bootstrap.log" 2>&1; then
    echo '机器人需要现有可用的pip或ensurepip；未安装系统组件，保留新目录供检查。' >&2
    exit 1
  fi
fi
if [[ -n "${RP1_WHEELHOUSE:-}" ]]; then
  "$monitor_base/.venv/bin/python" -m pip install --no-index --find-links "$RP1_WHEELHOUSE" 'rp1-monitor[agent]==0.4.0'
else
  "$monitor_base/.venv/bin/python" -m pip install "$project_root[agent]"
fi
"$monitor_base/.venv/bin/python" -B -m rp1_monitor profile-init --robot-id "$robot_id" --output "$monitor_base/profile.json"
# Configuration contains no passwords, central address, upload token or model key.
"$monitor_base/.venv/bin/python" -B - "$monitor_base" "$robot_id" "$ros_setup" <<'PY'
import json, os, sys
from pathlib import Path
base=Path(sys.argv[1])
config={'robot_id':sys.argv[2], 'ros_setup':sys.argv[3],
        'dds_profile':os.environ.get('FASTRTPS_DEFAULT_PROFILES_FILE',''),
        'rmw_implementation':os.environ.get('RMW_IMPLEMENTATION',''),
        'fastdds_xml':os.environ.get('RMW_FASTRTPS_USE_QOS_FROM_XML',''),
        'ros_domain_id':os.environ.get('ROS_DOMAIN_ID',''),
        'tui_executor':os.environ.get('RP1_ROBOPI_TUI_EXECUTOR','')}
path=base/'observer-config.json'
with path.open('x') as stream:
    json.dump(config,stream,ensure_ascii=False,indent=2)
path.chmod(0o600)
PY
cp "$project_root/scripts/start-robot.sh" "$monitor_base/start-observer.sh"
chmod 700 "$monitor_base/start-observer.sh"
printf '已安装到：%s\n启动命令：bash %q\n' "$monitor_base" "$monitor_base/start-observer.sh"
echo '未启动监听，未安装自启，未改运控或系统包。先核对ROS/DDS和设备档案，再手动启动。'
