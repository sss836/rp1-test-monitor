#!/usr/bin/env python3
"""One-shot host checks. No ROS node, network request, install or service mutation.

Use the intended collector Python after sourcing the existing ROS environment.
Only standard-library dependencies are required to run this preparation tool.
"""

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path

MODULES = (
    "rclpy",
    "sensor_msgs.msg",
    "diagnostic_msgs.msg",
    "rp1_monitor",
    "pydantic",
    "httpx",
    "websockets",
    "fastapi",
    "uvicorn",
)


def probe_module(name):
    """Test native-extension compatibility, without initializing ROS or a server."""
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-B",
                "-c",
                "import importlib; importlib.import_module(" + repr(name) + ")",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
        return "importable" if result.returncode == 0 else "needs_check"
    except (OSError, subprocess.TimeoutExpired):
        return "needs_check"


def os_description(path=Path("/etc/os-release")):
    result = {}
    try:
        for line in path.read_text().splitlines():
            key, separator, value = line.partition("=")
            if separator and key in {"ID", "VERSION_ID"}:
                result[key.lower()] = value.strip("\"'")[:64]
    except OSError:
        pass
    return result


def disk_status(path):
    # Inspect the nearest existing ancestor; never create a directory or test file.
    target = path.expanduser().absolute()
    while not target.exists() and target != target.parent:
        target = target.parent
    try:
        free = shutil.disk_usage(target).free
        writable = os.access(target, os.W_OK | os.X_OK)
        return {"free_bytes": free, "ancestor_writable": writable, "write_test_performed": False}
    except OSError:
        return {"free_bytes": None, "ancestor_writable": None, "write_test_performed": False}


def collect(state_dir):
    # Never dump environment variables, process arguments, credentials or logs.
    distro = os.environ.get("ROS_DISTRO", "")
    distro = distro if re.fullmatch(r"[a-zA-Z0-9_-]{1,40}", distro) else "not_detected"
    modules = {name: probe_module(name) for name in MODULES}
    python_supported = sys.version_info >= (3, 10)
    linux = sys.platform == "linux"
    ros_setups = []
    try:
        ros_setups = [path.parent.name for path in Path("/opt/ros").glob("*/setup.bash")]
    except OSError:
        pass
    host_checks = (
        linux and python_supported and all(value == "importable" for value in modules.values())
    )
    return {
        "schema_version": 1,
        "scope": "host_environment_preparation_only_not_robot_acceptance",
        "environment_status": "imports_ready" if host_checks else "needs_check",
        "system": {
            "os": os_description(),
            "linux": linux,
            "architecture": platform.machine(),
            "python": platform.python_version(),
            "python_meets_project_minimum": python_supported,
            "ros_distro_in_current_shell": distro,
            "ros_setup_candidates": sorted(ros_setups),
        },
        "modules": modules,
        "disk": disk_status(state_dir),
        "journalctl_available": shutil.which("journalctl") is not None,
        "still_requires_on_site_check": [
            "网线与既有SSH登录可用；本脚本不配置或验证网络",
            "实际ROS工作空间、Topic类型、QoS和名称对应关系",
            "BMS既有日志来源及读取权限；不为监控启动硬件服务",
            "电机诊断发布支持、实际驱动版本和物理关节映射",
            "状态目录权限与容量、采集开销及对现有运控的影响",
            "核心进程监控名单；逐进程监控尚未实现",
        ],
        "note": "needs_check可能表示缺包、未加载ROS环境或Python ABI不匹配；imports_ready仅表示本机基础导入通过。",
    }


def main():
    parser = argparse.ArgumentParser(
        description="RP1只读环境准备检查；不安装、不联网、不启动ROS节点"
    )
    parser.add_argument("--state-dir", type=Path, default=Path("/var/lib/rp1-monitor"))
    parser.add_argument("--json", action="store_true", help="输出不含凭据的JSON准备报告")
    args = parser.parse_args()
    report = collect(args.state_dir)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return
    print("RP1 采集环境准备检查（当前主机；不构成实机验收）")
    print(
        "系统: {0} / {1}；Python: {2}".format(
            report["system"]["os"], report["system"]["architecture"], report["system"]["python"]
        )
    )
    print(
        "Python要求 >=3.10: "
        + (
            "满足"
            if report["system"]["python_meets_project_minimum"]
            else "待核对，勿直接升级机器人系统"
        )
    )
    print("当前ROS环境: " + report["system"]["ros_distro_in_current_shell"])
    for name, state in report["modules"].items():
        print("  {0}: {1}".format(name, "可导入" if state == "importable" else "待核对"))
    free = report["disk"]["free_bytes"]
    print(
        "缓存目录所在分区可用空间: "
        + ("{0:.1f} GiB".format(free / 1024**3) if free is not None else "未知")
    )
    print("目录实际写入、SSH、ROS图、BMS权限、物理映射和开销仍需现场检查。")
    print(report["note"])


if __name__ == "__main__":
    main()
