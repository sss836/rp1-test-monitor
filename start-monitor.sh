#!/usr/bin/env bash
# Computer only. All installed files stay inside this checkout's .venv/runtime.
set -euo pipefail
project_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
cd "$project_root"
mode=${1:-desktop}
case "$mode" in desktop|web|setup) ;; *) echo '用法：./start-monitor.sh [desktop|web|setup]' >&2; exit 2;; esac
if [[ ! -x .venv/bin/python ]]; then
  python3 -m venv .venv
fi
if ! .venv/bin/python -c 'import sys, rp1_monitor, paramiko, PySide6, pyqtgraph, fastapi, uvicorn, sqlalchemy; from pathlib import Path; sys.exit(Path(rp1_monitor.__file__).resolve().parent != Path(sys.argv[1]).resolve())' "$project_root/src/rp1_monitor" >/dev/null 2>&1; then
  .venv/bin/python -m pip install -e '.[desktop,server,station]'
fi
if [[ "$mode" == setup ]]; then
  echo '电脑运行环境已准备好。运行 ./start-monitor.sh 启动上位机，或 ./start-monitor.sh web 打开网页后台。'
elif [[ "$mode" == web ]]; then
  echo '使用独立本机数据目录，不修改原PostgreSQL。'
  exec .venv/bin/python -m rp1_monitor station --state-dir "$project_root/runtime/station" --port 18980 --background --open-browser
else
  exec .venv/bin/python -m rp1_monitor desktop --quick-connect --station-dir "$project_root/runtime/station"
fi
