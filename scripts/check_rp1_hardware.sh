#!/usr/bin/env bash
# One transferable file. Python 3 + PyYAML; no network access.
# BMS/IMU/motors: read-only. LED: user-requested solid RGB 80 80 80.
# LRO query protocol/baselines: user-supplied deploy_motorlro_params.py.
# Version layout: LeadRobot CAN custom protocol 3 V1.00, section 4.5, PDF p.17.
# IMU protocol: roboparty_imu @ 7a6e6d2eeef8ff3513cee54a8fa833773efd42f3.
set -euo pipefail
exec python3 -u - "$@" <<'RP1_CHECK_PY'
import argparse
import datetime
import json
import math
import os
import pwd
import re
import shutil
import socket
import stat
import struct
import subprocess
import sys
import time
from pathlib import Path

EFF, RTR, ERR = 0x80000000, 0x40000000, 0x20000000
# model -> KP max, KD max, position max, speed max, torque max
MODELS = {
    0: (500, 50, 12.5, 21.0, 36.0),
    1: (500, 50, 12.5, 21.89, 35.3),
    2: (500, 50, 12.5, 16.13, 56.5),
    3: (500, 50, 12.5, 14.24, 141.7),
}
# name, query code, scale, signed, unit (KP/KD retain the source script's labels)
PARAMS = (
    ("KP", 0x17, 1, False, ""),
    ("KD", 0x18, 1, False, ""),
    ("POS", 0x19, 100, True, "rad"),
    ("SPD", 0x1A, 100, True, "rad/s"),
    ("TOR", 0x1B, 10, True, "N·m"),
    ("CAN_TIMEOUT", 0x1F, 1, False, "ms"),
)


def command(*argv):
    result = subprocess.run(argv, capture_output=True, text=True, timeout=10)
    if result.returncode:
        raise RuntimeError(
            result.stderr.strip() or result.stdout.strip() or f"{argv[0]} 返回 {result.returncode}"
        )
    return result.stdout


def load_config(path):
    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError("缺少 PyYAML；使用已安装 python3-yaml 的 Python 环境") from exc
    with path.open(encoding="utf-8") as stream:
        config = yaml.safe_load(stream)
    if not isinstance(config, dict):
        raise ValueError("robot.yaml 必须为映射")
    return config


def integer(value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"需要 {low}..{high} 的整数，实际为 {value!r}")
    return value


def interface_name(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9_.:-]{1,15}", value):
        raise ValueError(f"无效 CAN 接口名: {value!r}")
    return value


def motor_targets(config, expected_count):
    motors = config.get("motors")
    if not isinstance(motors, dict):
        raise ValueError("robot.yaml 缺少 motors")
    names = (
        "motor_interface",
        "motor_interface_type",
        "motor_num",
        "motor_type",
        "motor_id",
        "motor_model",
    )
    for name in names:
        if not isinstance(motors.get(name), list) or not motors[name]:
            raise ValueError(f"motors.{name} 必须为非空列表")
    buses = len(motors["motor_interface"])
    if any(len(motors[name]) != buses for name in names[1:4]):
        raise ValueError("电机总线配置列表长度不一致")
    counts = [integer(n, 1, 128) for n in motors["motor_num"]]
    if sum(counts) != expected_count or any(
        len(motors[name]) != expected_count for name in names[4:]
    ):
        raise ValueError(f"所选机型必须完整配置 {expected_count} 台电机；不允许漏检")
    targets, seen, offset = [], set(), 0
    for bus in range(buses):
        iface = interface_name(motors["motor_interface"][bus])
        kind = motors["motor_interface_type"][bus]
        if kind not in ("can", "canfd") or motors["motor_type"][bus] != "LRO":
            raise ValueError(f"{iface}: 仅支持 LRO + can/canfd，未覆盖的驱动不能算通过")
        for index in range(offset, offset + counts[bus]):
            motor_id = integer(motors["motor_id"][index], 1, 0x7FF)
            model = integer(motors["motor_model"][index], 0, 3)
            if (iface, motor_id) in seen:
                raise ValueError(f"重复电机地址: {iface}/{motor_id}")
            seen.add((iface, motor_id))
            targets.append((iface, kind, motor_id, model))
        offset += counts[bus]
    return targets


def check_link(iface, fd=False, bitrate=None):
    links = json.loads(command("ip", "-details", "-json", "link", "show", "dev", iface))
    if len(links) != 1:
        raise RuntimeError(f"{iface}: 未获得唯一接口")
    link = links[0]
    info = link.get("linkinfo", {})
    if info.get("info_kind") != "can":
        raise RuntimeError(f"{iface}: 非真实 SocketCAN 接口（虚拟总线不作实机判定）")
    data = info.get("info_data", {})
    if "UP" not in link.get("flags", []) or data.get("state") in ("BUS-OFF", "STOPPED", "SLEEPING"):
        raise RuntimeError(f"{iface}: 接口未就绪，CAN state={data.get('state', '未知')}")
    if fd and link.get("mtu", 0) < 72:
        raise RuntimeError(f"{iface}: robot.yaml 要求 CAN FD，但接口未启用 FD")
    actual = data.get("bittiming", {}).get("bitrate")
    if bitrate is not None and actual != bitrate:
        raise RuntimeError(f"{iface}: 主机波特率 {actual!r}，配置要求 {bitrate} bit/s")
    print(
        f"  CAN {iface}: state={data.get('state', '未知')}, bitrate={actual!r}, mtu={link.get('mtu')}"
    )


def open_can(iface, filters, fd=False):
    bus = socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
    try:
        if fd:
            bus.setsockopt(socket.SOL_CAN_RAW, socket.CAN_RAW_FD_FRAMES, 1)
        bus.setsockopt(
            socket.SOL_CAN_RAW,
            socket.CAN_RAW_FILTER,
            b"".join(struct.pack("=II", can_id, mask) for can_id, mask in filters),
        )
        bus.settimeout(0.3)
        bus.bind((iface,))
        return bus
    except Exception:
        bus.close()
        raise


def receive(bus, deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return None
    bus.settimeout(remaining)
    try:
        frame = bus.recv(72)
    except socket.timeout:
        return None
    if len(frame) not in (16, 72):
        return (ERR, b"")
    can_id, length = struct.unpack_from("=IB", frame)
    if length > len(frame) - 8:
        return (ERR, b"")
    return can_id, frame[8 : 8 + length]


def query_motor(iface, kind, motor_id, code, timeout, collect=False):
    # This is the only transmit site. Exactly two bytes: E0 + whitelisted query.
    # No C0 configuration, motor enable, zeroing, reset, or MIT control frame.
    if code not in {param[1] for param in PARAMS} | {0x1E}:
        raise ValueError("拒绝非参数查询指令")
    if collect and code != 0x1E:
        raise ValueError("仅版本查询支持采集多条响应")
    fd = kind == "canfd"
    with open_can(iface, [(motor_id, 0x7FF | EFF | RTR | ERR)], fd) as bus:
        bus.settimeout(timeout)
        frame = struct.pack(
            "=IBB2x64s" if fd else "=IBB2x8s", motor_id, 2, 1 if fd else 0, bytes((0xE0, code))
        )
        bus.send(frame)
        sent_at = time.monotonic()
        deadline = sent_at + timeout
        replies = []
        while True:
            received = receive(bus, deadline)
            if received is None:
                return replies if collect else None
            can_id, payload = received
            length = {0x1E: 2, 0x1F: 4}.get(code, 6)
            if can_id != motor_id or len(payload) < length:
                continue
            if payload[0] & 0xE0 == 0xA0 and payload[1] == code:
                if collect:
                    replies.append((time.monotonic() - sent_at, payload))
                else:
                    return payload


def decode_motor_version(payload):
    if len(payload) != 12:
        raise ValueError(f"版本回复应为12字节，实际{len(payload)}字节")
    if payload[0] >> 5 != 5 or payload[1] != 0x1E:
        raise ValueError("非类型5的1E版本回复")
    if payload[0] & 0x1F:
        raise ValueError(f"电机报告错误码0x{payload[0] & 0x1F:02X}")
    # uint8 arrays, not packed integers/ASCII/BCD. Dots are display separators;
    # the protocol does not assign major/minor/patch meanings to components.
    return tuple(payload[2:5]), tuple(payload[5:8]), tuple(payload[8:12])


def describe_motor_versions(replies, verbose):
    if not replies:
        return "；版本=UNKNOWN（无匹配响应）", []
    versions, issues = set(), []
    for _age, payload in replies:
        try:
            versions.add(decode_motor_version(payload))
        except ValueError as exc:
            issues.append(str(exc))
    if len(versions) > 1:
        issues.append("同一电机收到不一致版本回复")
    if issues:
        summary = "；版本=UNKNOWN（" + "；".join(dict.fromkeys(issues)) + "）"
    else:
        components = next(iter(versions))
        summary = "；版本=READ " + " ".join(
            f"{label}={'.'.join(map(str, values))}"
            for label, values in zip(("FW", "HW", "PROTO"), components)
        )
    details = []
    if verbose or issues:
        details = [
            f"    版本回复 {len(payload)}B@+{age:.3f}s raw={payload.hex().upper()}"
            for age, payload in replies
        ]
    return summary, details


def check_motors(config, args):
    targets = motor_targets(config, 24 if args.robot == "rp1_24dof" else 28)
    # Validate the complete configuration and every interface before transmitting.
    for iface, kind in dict.fromkeys((target[0], target[1]) for target in targets):
        check_link(iface, fd=kind == "canfd")
    failures = 0
    print("  比对基准: 用户电机脚本的型号参数；CAN_TIMEOUT 期望 0 ms；逐原始整数精确比对")
    if args.versions:
        print("  版本READ仅表示按协议读取成功，未校验允许版本；点号仅分隔uint8分量。")
    for iface, kind, motor_id, model in targets:
        motor_failures, details = 0, []
        for index, (name, code, scale, signed, unit) in enumerate(PARAMS):
            maximum = MODELS[model][index] if index < 5 else 0
            expected = (
                0 if index < 2 or index == 5 else -round(maximum * scale),
                round(maximum * scale),
            )
            try:
                payload = query_motor(iface, kind, motor_id, code, args.timeout)
                if payload is None:
                    raise RuntimeError("无匹配响应")
                if payload[0] & 0x1F:
                    raise RuntimeError(
                        f"电机报告错误码0x{payload[0] & 0x1F:02X}; raw={payload.hex().upper()}"
                    )
                actual = (
                    (0, struct.unpack_from(">H", payload, 2)[0])
                    if index == 5
                    else struct.unpack_from(">hh" if signed else ">HH", payload, 2)
                )
                ok = actual == expected
                motor_failures += not ok
                if not ok or args.verbose:
                    details.append(
                        f"    {'PASS' if ok else 'FAIL'} {name}: 实际={[v / scale for v in actual]} {unit}; 期望={[v / scale for v in expected]} {unit}; raw={payload.hex().upper()}"
                    )
            except (OSError, RuntimeError) as exc:
                motor_failures += 1
                details.append(f"    FAIL {name}: {exc}")
        failures += motor_failures
        version = ""
        if args.versions:
            # The PDF defines one 12-byte reply. Do not join short frames or
            # silently choose between conflicting complete replies.
            try:
                replies = query_motor(iface, kind, motor_id, 0x1E, args.timeout, collect=True)
                version, version_details = describe_motor_versions(replies, args.verbose)
                details.extend(version_details)
            except (OSError, RuntimeError) as exc:
                version = f"；版本=UNKNOWN（{exc}）"
        print(
            f"  [{'FAIL' if motor_failures else 'PASS'}] {iface}/{motor_id} model={model}: 参数 {len(PARAMS) - motor_failures}/{len(PARAMS)}{version}"
        )
        for detail in details:
            print(detail)
    total = len(targets) * len(PARAMS)
    return (
        "FAIL" if failures else "PASS",
        f"{len(targets)} 台电机，{total - failures}/{total} 项符合基准",
    )


def check_imu(config, args):
    imu = config.get("imu", {})
    if (
        not isinstance(imu, dict)
        or imu.get("imu_type") != "HIPNUC"
        or imu.get("imu_interface_type") != "can"
    ):
        return "UNKNOWN", "仅覆盖 HIPNUC J1939 CAN；其他 IMU 未实现判定"
    iface = interface_name(imu.get("imu_interface"))
    imu_id = integer(imu.get("imu_id"), 0, 255)
    bitrate = integer(imu.get("baudrate"), 1, 10000000)
    check_link(iface, bitrate=bitrate)
    pgns = {0xFF46: "四元数", 0xFF37: "角速度"}
    filters = [(EFF | (pgn << 8) | imu_id, EFF | RTR | ERR | 0x00FFFFFF) for pgn in pgns]
    samples = {pgn: [] for pgn in pgns}
    invalid, last_values = 0, {}
    with open_can(iface, filters) as bus:
        start = time.monotonic()
        end = start + args.seconds
        print(f"  被动采样 {args.seconds:g}s，IMU ID={imu_id}，每路最低 {args.imu_min_hz:g} Hz")
        while True:
            received = receive(bus, end)
            if received is None:
                break
            can_id, payload = received
            pgn = (can_id >> 8) & 0xFFFF
            if (
                not can_id & EFF
                or can_id & (RTR | ERR)
                or can_id & 0xFF != imu_id
                or pgn not in pgns
            ):
                continue
            if len(payload) != 8:
                invalid += 1
                continue
            if pgn == 0xFF46:
                values = [v * 0.0001 for v in struct.unpack("<4h", payload)]
                norm = math.sqrt(sum(v * v for v in values))
                if not 0.95 <= norm <= 1.05:
                    invalid += 1
                    continue
            else:
                values = [v * 0.061035 * math.pi / 180 for v in struct.unpack_from("<3h", payload)]
            samples[pgn].append(time.monotonic())
            last_values[pgn] = (values, payload.hex().upper())
    failed = invalid > 0
    gap_limit = max(0.2, 3 / args.imu_min_hz)
    for pgn, name in pgns.items():
        times = samples[pgn]
        hz = len(times) / args.seconds
        boundaries = [start, *times, end]
        gap = max(b - a for a, b in zip(boundaries, boundaries[1:]))
        ok = len(times) >= 3 and hz >= args.imu_min_hz and gap <= gap_limit
        failed |= not ok
        print(
            f"  {'PASS' if ok else 'FAIL'} PGN={pgn:04X} {name}: {len(times)} 帧，{hz:.1f} Hz，最大接收间隔={gap:.3f}s（门槛 {gap_limit:.3f}s）"
        )
        if pgn in last_values:
            values, raw = last_values[pgn]
            print(
                f"    最后值={values} {'[w,x,y,z]' if pgn == 0xFF46 else 'rad/s [x,y,z]'}; raw={raw}"
            )
    print(f"  无效目标帧={invalid}；频率为本次主机接收频率，未检验安装方向或绝对姿态精度")
    return (
        "FAIL" if failed else "PASS",
        "HIPNUC 配置与两路数据检查完成；仅表示观测窗口内符合检查条件",
    )


def check_bms(args):
    state = command("systemctl", "show", "bms.service", "--property=ActiveState", "--value").strip()
    if state != "active":
        return "FAIL", f"bms.service 当前为 {state or '未知'}；未启停服务"
    output = command(
        "journalctl",
        "-b",
        "-u",
        "bms.service",
        "--since",
        f"{args.bms_max_age:g} seconds ago",
        "-n",
        "300",
        "--no-pager",
        "--quiet",
        "-o",
        "json",
    )
    number = r"([+-]?(?:\d+(?:\.\d*)?|\.\d+|nan|inf))"
    # Only battery voltage fields; never match an NRF '48V=ON' power-rail label.
    pattern = re.compile(
        r"\[(?:CAN )?BMS Data\]\s*Voltage:\s*"
        + number
        + r"V\b|\[NRF PMIC\].*?\|\s*BMS\s+"
        + number
        + r"V\b",
        re.IGNORECASE,
    )
    latest = None
    now = time.time()
    for line in output.splitlines():
        entry = json.loads(line)
        message = entry.get("MESSAGE", "")
        if not isinstance(message, str):
            continue
        match = pattern.search(message)
        if not match:
            continue
        timestamp = int(entry["__REALTIME_TIMESTAMP"]) / 1000000
        if 0 <= now - timestamp <= args.bms_max_age and (latest is None or timestamp > latest[0]):
            latest = (timestamp, float(match.group(1) or match.group(2)), message)
    if latest is None:
        return "UNKNOWN", "当前启动的近期日志没有可识别电压；检查日志权限、串口/485接线及日志格式"
    timestamp, voltage, message = latest
    print(
        f"  journal时间={datetime.datetime.fromtimestamp(timestamp).astimezone().isoformat()}, age={now - timestamp:.2f}s"
    )
    print(f"  原始日志: {message}")
    if not math.isfinite(voltage) or not 0 < voltage <= 1000:
        return "FAIL", f"电池电压无效: {voltage} V"
    return (
        "PASS",
        f"服务运行且近期日志有电池电压 {voltage:g} V；日志可能沿用缓存值，不证明全部485字段刚采样",
    )


def check_led():
    missing = []
    try:
        package = command("dpkg-query", "-W", "-f=${Status}", "robopi-addon").strip()
        if package != "install ok installed":
            missing.append("robopi-addon 未配置完成")
    except RuntimeError:
        missing.append("robopi-addon 未安装")
    if not shutil.which("robopi-ws2812"):
        missing.append("缺少 robopi-ws2812 命令")
    device = Path("/dev/robopi-ws2812")
    if not device.exists() or not stat.S_ISCHR(device.stat().st_mode):
        missing.append("缺少 /dev/robopi-ws2812 字符设备")
    if missing:
        return "FAIL", "；".join(missing)
    try:
        command("robopi-ws2812", "solid", "80", "80", "80")
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        return "FAIL", f"设置 RGB 80 80 80 命令失败或未确认: {exc}"
    print("  已执行: robopi-ws2812 solid 80 80 80（命令返回成功）")
    return "UNKNOWN", "已发送 RGB 80 80 80，保持亮灯；实际灯珠/颜色待现场确认"


def show_versions():
    print("\n=== 版本信息（信息采集，不表示符合指定版本基线） ===")
    for package in (
        "roboparty-bms",
        "roboparty-motors",
        "roboparty-imu",
        "robopi-addon",
        "robopi-tui",
    ):
        try:
            output = command("dpkg-query", "-W", "-f=${Status}\t${Version}", package).strip()
            status, version = output.split("\t", 1)
            if status != "install ok installed":
                raise ValueError(status)
            print(f"  板卡软件包 {package}: {version}（非设备固件版本）")
        except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
            print(f"  UNKNOWN 软件包 {package}: {exc}")
    try:
        invocation = command(
            "systemctl", "show", "bms.service", "--property=InvocationID", "--value"
        ).strip()
        if not re.fullmatch(r"[a-fA-F0-9]{32}", invocation) or not int(invocation, 16):
            raise ValueError("bms.service 当前无有效 InvocationID")
        output = command(
            "journalctl",
            "-b",
            "-u",
            "bms.service",
            f"_SYSTEMD_INVOCATION_ID={invocation}",
            "--grep=FW Version:",
            "-n",
            "1",
            "--no-pager",
            "--quiet",
            "-o",
            "json",
        )
        entries = [json.loads(line) for line in output.splitlines()]
        if not entries:
            raise ValueError("本次 BMS 服务运行无设备版本日志；可能不支持或日志已轮转")
        entry = entries[-1]
        match = re.search(
            r"FW Version:\s*(0x[0-9a-fA-F]+).*HW Version:\s*(0x[0-9a-fA-F]+)",
            entry.get("MESSAGE", ""),
        )
        if not match:
            raise ValueError("设备版本日志格式未识别")
        timestamp = int(entry["__REALTIME_TIMESTAMP"]) / 1000000
        print(
            f"  BMS设备版本来源: 本次服务启动日志，journal时间={datetime.datetime.fromtimestamp(timestamp).astimezone().isoformat()}"
        )
        for label, raw in zip(("FW", "HW"), match.groups()):
            if int(raw, 16) == 0:
                print(f"  UNKNOWN BMS设备{label}: {raw}，可能为不支持的占位值（SCUD485驱动即填0）")
            else:
                print(f"  BMS设备{label}原始版本码: {raw}（不推断语义版本号；未校验版本基线）")
    except (
        OSError,
        RuntimeError,
        ValueError,
        KeyError,
        TypeError,
        subprocess.TimeoutExpired,
    ) as exc:
        print(f"  UNKNOWN BMS设备FW/HW: {exc}")


def positive(value):
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise argparse.ArgumentTypeError("需要大于0的有限数值")
    return result


def main():
    home = (
        Path(pwd.getpwnam(os.environ["SUDO_USER"]).pw_dir)
        if os.environ.get("SUDO_USER")
        else Path.home()
    )
    parser = argparse.ArgumentParser(
        prog="check_rp1_hardware.sh",
        description="RP1 板卡检查：BMS、HIPNUC和LRO只读查询；WS2812主动设置RGB 80 80 80并保持亮灯；不启停服务。",
    )
    parser.add_argument("--deploy-dir", type=Path, default=home / "rp1_deploy")
    parser.add_argument(
        "--robot", choices=("rp1_24dof", "rp1"), default="rp1_24dof", help="24 DOF（默认）或 28 DOF"
    )
    parser.add_argument("--config", type=Path, help="明确指定板卡上的 robot.yaml")
    parser.add_argument("--seconds", type=positive, default=5, help="IMU 采样秒数，默认5")
    parser.add_argument(
        "--imu-min-hz", type=positive, default=50, help="每路IMU最低接收Hz，工程检查门槛，默认50"
    )
    parser.add_argument(
        "--timeout", type=positive, default=0.3, help="每项电机响应超时秒数，默认0.3"
    )
    parser.add_argument(
        "--bms-max-age", type=positive, default=30, help="BMS日志最大年龄秒数，默认30"
    )
    parser.add_argument(
        "--versions",
        action="store_true",
        help="附加软件包/BMS版本与电机FW/HW/PROTO解码；在运控停用的检修窗口读取，不判定版本合规",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="展开所有电机参数实读值；默认每台一行，仅展开异常项",
    )
    args = parser.parse_args()
    if args.seconds > 60 or args.timeout > 5:
        parser.error("IMU采样最多60秒，每项电机超时最多5秒")
    path = (
        args.config or args.deploy_dir / "src/inference/robots" / args.robot / "robot.yaml"
    ).expanduser()
    print("RP1 板卡硬件检查 · 来源=本机 journal / SocketCAN / 设备节点；真实性待现场核对")
    print("灯板将主动设置为 RGB 80 80 80 并保持亮灯；BMS/IMU/电机只读查询。")
    print(
        f"配置: {path}；机型: {args.robot}；开始: {datetime.datetime.now().astimezone().isoformat()}"
    )
    if args.versions:
        print(
            "版本查询应在运控停用的检修窗口执行：所核查LRO驱动可能误解码长查询回复；本脚本不启停运控。"
        )
        show_versions()
    config, config_error = None, None
    try:
        config = load_config(path)
    except Exception as exc:
        config_error = f"配置不可用: {exc}"
    results = []
    checks = [
        ("BMS", lambda: check_bms(args)),
        ("灯板", check_led),
        ("IMU", lambda: check_imu(config, args)),
        ("电机参数", lambda: check_motors(config, args)),
    ]
    for name, check in checks:
        print(f"\n=== {name} ===")
        try:
            status, detail = (
                ("UNKNOWN", config_error)
                if config_error and name in ("IMU", "电机参数")
                else check()
            )
        except (
            OSError,
            ValueError,
            RuntimeError,
            KeyError,
            TypeError,
            subprocess.TimeoutExpired,
        ) as exc:
            status, detail = "FAIL", str(exc)
        results.append(status)
        print(f"[{status}] {name}: {detail}")
    print("\n汇总: " + " / ".join(f"{name}={status}" for (name, _), status in zip(checks, results)))
    print(
        "PASS 仅代表所列检查条件通过；UNKNOWN 为尚未确认。灯板设置结果见上；未执行 IMU 初始化或电机写入。"
    )
    return 1 if "FAIL" in results else 2 if "UNKNOWN" in results else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n检查已中断；未修改 IMU/电机配置，已发送的灯板颜色可能保留。", file=sys.stderr)
        sys.exit(130)
RP1_CHECK_PY
