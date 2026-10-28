"""Robot-only commands; no desktop, central storage or model integration."""

import argparse
import os
from pathlib import Path


def private_write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(text)


def main():
    parser = argparse.ArgumentParser(description="RP1机器人端只读采集；不含上位机和AI")
    sub = parser.add_subparsers(dest="command", required=True)
    agent = sub.add_parser("agent", help="机器人只读采集与缓存；不连接中央服务")
    agent.add_argument("--source", choices=["simulated", "ros2"], required=True)
    agent.add_argument(
        "--simulated-dof", type=int, choices=[24, 28],
        help="仅模拟：关节数量；ROS2仍以实际关节反馈为准",
    )
    agent.add_argument(
        "--thermal-demo", action="store_true", help="仅模拟：逐关节温升、超温与降温循环"
    )
    agent.add_argument("--robot-id", required=True)
    agent.add_argument("--state-dir", type=Path, required=True)
    agent.add_argument("--cache-mib", type=int, default=1024)
    agent.add_argument("--reserve-mib", type=int, default=256)
    agent.add_argument(
        "--rp-server-url", help="仅GET读取实机已运行的rp-server；指定后替代BMS日志采集，不启动服务"
    )
    agent.add_argument(
        "--robopi-tui-executor", type=Path,
        help="可选：已安装的robopi_TUI plugin_executor.py绝对路径；仅指定被动自检，不自动安装依赖",
    )
    agent.add_argument("--port", type=int, default=8891)
    agent.add_argument(
        "--profile", type=Path, help="经过核对的设备档案JSON；电机诊断须提供身份映射"
    )
    profile = sub.add_parser("profile-init", help="生成待核对的设备档案，不探测或初始化硬件")
    profile.add_argument("--robot-id", required=True)
    profile.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "profile-init":
        from .profiles import RobotProfile, profile_json

        private_write(args.output, profile_json(RobotProfile(robot_id=args.robot_id)))
        print(f"已生成待核对档案：{args.output}")
        return

    import uvicorn

    from .agent import AgentSettings, create_agent_app

    settings = AgentSettings(
        robot_id=args.robot_id,
        source_kind=args.source,
        state_dir=args.state_dir,
        cache_bytes=args.cache_mib * 1024**2,
        reserve_bytes=args.reserve_mib * 1024**2,
        rp_server_url=args.rp_server_url,
        profile_path=args.profile,
        thermal_demo=args.thermal_demo,
        simulated_dof=args.simulated_dof,
        robopi_tui_executor=args.robopi_tui_executor,
    )
    uvicorn.run(create_agent_app(settings), host="127.0.0.1", port=args.port, access_log=False)
