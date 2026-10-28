"""Read-only Linux host and BMS journal observers; never open a hardware bus."""

import json
import math
import os
import re
import selectors
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from .host_cpu import CPUObserver

_NUMBER = r"([+-]?(?:\d+(?:\.\d*)?|\.\d+))"
_BMS_MESSAGE = re.compile(
    rf"\[(?:CAN )?BMS Data\]\s*Voltage:\s*{_NUMBER}V\s*\|\s*"
    rf"Current:\s*{_NUMBER}A\s*\|\s*SoC:\s*{_NUMBER}%"
    r"(?:\s*\|\s*Power:\s*(ON|OFF))?\s*"
)
_NRF_MESSAGE = re.compile(
    r"\[NRF PMIC\]\s+.{1,120}?\s*\|\s*48V=(ON|OFF)\s+5V=(ON|OFF)\s*\|\s*"
    rf"BMS\s+{_NUMBER}V\s+{_NUMBER}A\s+{_NUMBER}%\s*"
)
_JOURNAL_COMMAND = (
    "journalctl",
    "--unit=bms.service",
    "--follow",
    "--output=json",
    "--since=now",
    "--no-pager",
)
_BMS_DETAIL = (
    "bms.service 新日志；时间为 journal 记录时间，不能证明各字段硬件采样时间；"
    "旧协议可能复用字段，未提供电池温度；Power/48V 是开关状态"
)


def parse_bms_message(message):
    """Return validated protocol values, or None for an unrecognised/invalid line."""
    if not isinstance(message, str):
        return None
    match = _BMS_MESSAGE.fullmatch(message.strip())
    nrf_match = _NRF_MESSAGE.fullmatch(message.strip()) if match is None else None
    if match is None and nrf_match is None:
        return None
    if match is not None:
        numbers, power = match.groups()[:3], match.group(4)
    else:
        numbers, power = nrf_match.groups()[2:5], nrf_match.group(1)
    voltage, current, soc = (float(value) for value in numbers)
    # Broad sanity limits, not RP1 alarm thresholds or pack specifications.
    if not (
        all(math.isfinite(value) for value in (voltage, current, soc))
        and 0 < voltage <= 1000
        and -1000 <= current <= 1000
        and 0 <= soc <= 100
    ):
        return None
    values = {"battery.voltage": voltage, "battery.current": current, "battery.soc": soc}
    if nrf_match is not None:
        values["battery.rail48_enabled"] = float(nrf_match.group(1) == "ON")
        values["battery.rail5_enabled"] = float(nrf_match.group(2) == "ON")
    elif power is not None:
        values["battery.power_enabled"] = float(power == "ON")
    return values


def parse_journal_entry(raw, now=None):
    """Accept only a current BMS journal row; its timestamp is not a device stamp."""
    try:
        row = json.loads(raw)
        if not isinstance(row, dict):
            return None
        stamp = datetime.fromtimestamp(int(row["__REALTIME_TIMESTAMP"]) / 1e6, timezone.utc)
        current = now if now is not None else datetime.now(timezone.utc)
        age = (current - stamp).total_seconds()
        if age > 5 or age < -1:
            return None
        values = parse_bms_message(row.get("MESSAGE"))
        return (values, stamp) if values is not None else None
    except (ValueError, TypeError, KeyError, OverflowError, OSError):
        return None


class HealthSource:
    """Observe local /sys, /proc and journalctl in independent daemon threads."""

    def __init__(
        self,
        accumulator,
        *,
        sysfs_root=Path("/sys"),
        proc_root=Path("/proc"),
        poll_interval=1.0,
        journal_command=None,
        enable_bms=True,
    ):
        self.accumulator = accumulator
        self.sysfs_root = Path(sysfs_root)
        self.proc_root = Path(proc_root)
        self.poll_interval = poll_interval
        self.enable_bms = enable_bms
        self.journal_command = tuple(journal_command or _JOURNAL_COMMAND)
        self.stop_event = threading.Event()
        self._cpu = CPUObserver()
        self._last_journal_stamp = None
        self._process = None
        self._process_lock = threading.Lock()
        self._host_thread = threading.Thread(target=self._run_host, name="host-health", daemon=True)
        self._bms_thread = threading.Thread(target=self._run_bms, name="bms-journal", daemon=True)

    def start(self):
        self.accumulator.set_health_source("host", "waiting", "等待本机 /sys 与 /proc 采样")
        self._host_thread.start()
        if self.enable_bms:
            self.accumulator.set_health_source("bms", "waiting", "等待 bms.service 新日志")
            self._bms_thread.start()

    def _poll_host(self):
        values, sensors, missing = {}, {}, []
        thermal_root = self.sysfs_root / "class" / "thermal"
        zones = sorted(
            (
                (int(path.name.removeprefix("thermal_zone")), path)
                for path in thermal_root.glob("thermal_zone*")
                if re.fullmatch(r"thermal_zone\d+", path.name)
            ),
            key=lambda item: item[0],
        )
        for index, path in zones:
            if index >= 64:
                continue
            try:
                sensor_type = " ".join((path / "type").read_text().split())[:120]
                temperature = float((path / "temp").read_text()) / 1000
                if (
                    not sensor_type
                    or not math.isfinite(temperature)
                    or not -100 <= temperature <= 250
                ):
                    raise ValueError("无效 thermal 数值或类型")
                key = f"host.thermal.{index}.temperature"
                values[key] = temperature
                sensors[key] = sensor_type
            except (OSError, ValueError):
                missing.append(f"thermal_zone{index} 读不到有效温度")
        if not zones:
            missing.append("本机没有 thermal_zone 温度源")

        try:
            uptime = float((self.proc_root / "uptime").read_text().split()[0])
            if not math.isfinite(uptime) or uptime < 0:
                raise ValueError("无效 /proc/uptime")
            values["host.uptime.seconds"] = uptime
        except (OSError, ValueError, IndexError):
            missing.append("无法读取内核开机时长 /proc/uptime")

        cpu_values, cpu_metadata, cpu_missing = self._cpu.sample(self.proc_root, self.sysfs_root)
        values.update(cpu_values)
        sensors.update(cpu_metadata)
        missing.extend(cpu_missing)
        self.accumulator.set_health_sensors(sensors)

        try:
            memory = {}
            for row in (self.proc_root / "meminfo").read_text().splitlines():
                key, raw_value = row.split(":", 1)
                if key in ("MemTotal", "MemAvailable"):
                    memory[key] = int(raw_value.split()[0])
            total, available = memory["MemTotal"], memory["MemAvailable"]
            if not total > 0 or not 0 <= available <= total:
                raise ValueError("无效 /proc/meminfo")
            values["host.memory.usage"] = 100 * (total - available) / total
        except (OSError, ValueError, KeyError, IndexError):
            missing.append("无法读取 MemTotal/MemAvailable")

        if values:
            self.accumulator.observe("host.health", values)
        detail = "机器人运行采集进程的本机 CPU、内存和 thermal_zone；不是电池或电机温度"
        if missing:
            detail += "；" + "；".join(missing)
        self.accumulator.set_health_source("host", "live" if values else "error", detail[:512])

    def _run_host(self):
        while not self.stop_event.is_set():
            self._poll_host()
            self.stop_event.wait(self.poll_interval)

    def _run_bms(self):
        while not self.stop_event.is_set():
            process = None
            try:
                process = subprocess.Popen(
                    self.journal_command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    bufsize=0,
                )
                with self._process_lock:
                    self._process = process
                if self.stop_event.is_set():
                    return
                self._follow_journal(process)
                if not self.stop_event.is_set():
                    self.accumulator.set_health_source(
                        "bms",
                        "error",
                        "journalctl 已断开，2 秒后重试；检查 bms.service 与日志读取权限",
                    )
            except (OSError, ValueError) as exc:
                if not self.stop_event.is_set():
                    self.accumulator.set_health_source(
                        "bms",
                        "error",
                        f"无法读取 bms.service 日志：{str(exc)[:200]}；2 秒后重试",
                    )
            finally:
                if process is not None:
                    self._terminate_process(process)
                    if process.stdout is not None:
                        process.stdout.close()
                with self._process_lock:
                    if self._process is process:
                        self._process = None
            self.stop_event.wait(2)

    def _follow_journal(self, process):
        buffer = b""
        discarding = False
        last_valid = None
        stale_reported = False
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while not self.stop_event.is_set():
                if (
                    last_valid is not None
                    and time.monotonic() - last_valid > 5
                    and not stale_reported
                ):
                    self.accumulator.set_health_source(
                        "bms",
                        "waiting",
                        "超过 5 秒没有有效 BMS 新日志；已有值按采样年龄失效",
                    )
                    stale_reported = True
                if not selector.select(timeout=0.25):
                    if process.poll() is not None:
                        return
                    continue
                chunk = os.read(process.stdout.fileno(), 65536)
                if not chunk:
                    return
                if discarding:
                    if b"\n" not in chunk:
                        continue
                    chunk = chunk.split(b"\n", 1)[1]
                    discarding = False
                buffer += chunk
                while b"\n" in buffer:
                    raw, buffer = buffer.split(b"\n", 1)
                    if len(raw) > 262144:
                        self.accumulator.set_health_source(
                            "bms", "error", "journal 行超过 256 KiB，已拒绝"
                        )
                        continue
                    if any(
                        phrase in raw.lower()
                        for phrase in (
                            b"permission denied",
                            b"access denied",
                            b"not seeing messages",
                            b"no journal files",
                        )
                    ):
                        self.accumulator.set_health_source(
                            "bms",
                            "error",
                            "journalctl 无日志读取权限或无日志文件："
                            + raw.decode("utf-8", "replace")[:160],
                        )
                        continue
                    parsed = parse_journal_entry(raw)
                    if parsed is None:
                        self.accumulator.set_health_source(
                            "bms",
                            "waiting",
                            "新日志未匹配 BMS 协议或时间过期；需核对协议、服务与读取权限",
                        )
                        continue
                    values, stamp = parsed
                    if self._last_journal_stamp is not None and stamp <= self._last_journal_stamp:
                        self.accumulator.set_health_source(
                            "bms", "waiting", "重复或乱序BMS日志不刷新已有观测时间"
                        )
                        continue
                    age_ms = max(0, (datetime.now(timezone.utc) - stamp).total_seconds() * 1000)
                    self.accumulator.observe("bms.journal", values, stamp, event_age_ms=age_ms)
                    self._last_journal_stamp = stamp
                    self.accumulator.set_health_source("bms", "live", _BMS_DETAIL)
                    last_valid, stale_reported = time.monotonic() - age_ms / 1000, False
                if len(buffer) > 262144:
                    buffer = b""
                    discarding = True
                    self.accumulator.set_health_source(
                        "bms", "error", "journal 行超过 256 KiB，已拒绝"
                    )

    @staticmethod
    def _terminate_process(process):
        if process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
            except ProcessLookupError:
                pass

    def stop(self):
        self.stop_event.set()
        with self._process_lock:
            process = self._process
        if process is not None:
            self._terminate_process(process)
        self._host_thread.join(timeout=3)
        if self.enable_bms:
            self._bms_thread.join(timeout=5)
