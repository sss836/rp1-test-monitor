"""Read the existing RP server's HTTP snapshot without starting any drivers."""

import json
import math
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

import httpx

_LIMIT = 1024 * 1024
_CACHE_DETAIL = (
    "已有 rp-server 缓存快照；缺少字段采样时间与 valid 位，不能判断硬件实时健康；"
    "接收时间仅表示本次 HTTP 快照到达"
)


def _number(value, minimum, maximum):
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and minimum <= value <= maximum
        and math.isfinite(value)
    )


class RpServerSource:
    def __init__(self, accumulator, url):
        parsed = urlsplit(url)
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in ("", "/")
        ):
            raise ValueError("rp-server URL 必须为无凭据的 HTTP(S) 服务地址；建议使用本机 SSH 转发")
        # Accessing port validates malformed or out-of-range ports before the thread starts.
        _ = parsed.port
        self.url = url.rstrip("/") + "/api/status"
        self.accumulator = accumulator
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, name="rp-server-status", daemon=True)

    def start(self):
        self.accumulator.set_health_source("rp_server", "waiting", "等待已有 rp-server 的只读快照")
        self.accumulator.set_health_source("bms", "waiting", "等待已有 rp-server 提供 BMS 快照")
        self.thread.start()

    def _run(self):
        with httpx.Client(timeout=1.0, trust_env=False, follow_redirects=False) as client:
            while not self.stop_event.is_set():
                started = time.monotonic()
                self.poll_once(client)
                self.stop_event.wait(max(0, 1.0 - (time.monotonic() - started)))

    def poll_once(self, client):
        """Perform one bounded GET; an unsuccessful poll never refreshes metric ages."""
        try:
            started = time.monotonic()
            with client.stream("GET", self.url, timeout=1.0, follow_redirects=False) as response:
                response.raise_for_status()
                if int(response.headers.get("content-length", "0")) > _LIMIT:
                    raise ValueError("rp-server 响应超过 1 MiB")
                body = bytearray()
                for chunk in response.iter_bytes():
                    if len(body) + len(chunk) > _LIMIT:
                        raise ValueError("rp-server 响应超过 1 MiB")
                    if time.monotonic() - started > 1.0:
                        raise ValueError("rp-server 读取超过 1 秒")
                    body.extend(chunk)
            data = json.loads(body)
            if not isinstance(data, dict):
                raise ValueError("rp-server 状态不是 JSON 对象")
            if data.get("mock") is not False:
                self.accumulator.set_health_source(
                    "rp_server",
                    "unsupported",
                    "rp-server 是 mock 或没有明确 mock=false，拒绝混入实机来源",
                )
                self.accumulator.set_health_source(
                    "bms", "unsupported", "mock/未知来源的 BMS 快照未采集"
                )
                return False
            if not isinstance(data.get("hardware"), dict) or not isinstance(
                data.get("hw_ready"), bool
            ):
                raise ValueError("rp-server 缺少明确 hardware/hw_ready，协议待核对")
            self._apply_snapshot(data)
            return True
        except (httpx.HTTPError, ValueError, OSError) as exc:
            detail = f"rp-server 只读快照读取失败：{str(exc)[:200]}；已有数据不续期，下一轮重试"
            self.accumulator.set_health_source("rp_server", "error", detail)
            self.accumulator.set_health_source("bms", "error", detail)
            return False

    def _apply_snapshot(self, data):
        now = datetime.now(timezone.utc)
        hardware = data["hardware"]
        errors = data.get("motor_errors")
        if isinstance(errors, list):
            if len(errors) > 64:
                raise ValueError("rp-server motor_errors 超过 64 项，未截断故障报告")
            faults = []
            for item in errors:
                if not isinstance(item, dict):
                    raise ValueError("rp-server 电机故障项格式无效")
                code, motor_id = item.get("code"), item.get("id")
                if (
                    type(code) is not int
                    or not 0 <= code <= 2**32 - 1
                    or type(motor_id) is not int
                    or not 0 <= motor_id <= 65535
                ):
                    raise ValueError("rp-server 电机原始 ID/故障码无效，未转成 ROS 关节索引")
                if code:
                    faults.append(
                        {
                            "motor_id": motor_id,
                            "code": code,
                            "name": str(item.get("name", ""))[:128],
                        }
                    )
            detail = (
                _CACHE_DETAIL
                + "；仅保留非零原始驱动故障码，不映射 ROS 关节；"
                + ("存在已报故障" if faults else "此快照没有已报故障，不代表全电机健康")
            )
            if data["hw_ready"] is not True or hardware.get("motors") is not True:
                detail += "；整机/电机硬件就绪条件未全部满足"
            self.accumulator.set_health_source(
                "rp_server", "live", detail[:512], reported_faults=faults, observed_at=now
            )
        else:
            self.accumulator.set_health_source(
                "rp_server", "waiting", _CACHE_DETAIL + "；未提供 motor_errors，故障状态未知"
            )

        battery = data.get("battery")
        if hardware.get("bms") is not True or not isinstance(battery, dict):
            self.accumulator.set_health_source(
                "bms", "waiting", _CACHE_DETAIL + "；BMS 硬件未就绪或 battery 缺失，未刷新电池值"
            )
            return
        voltage = battery.get("voltage")
        if not _number(voltage, 0, 1000) or voltage <= 0:
            self.accumulator.set_health_source(
                "bms", "waiting", _CACHE_DETAIL + "；电压未初始化/无效，整批电池值未刷新"
            )
            return
        values, missing = {"battery.voltage": float(voltage)}, []
        for name, source, minimum, maximum in (
            ("current", "current", -1000, 1000),
            ("soc", "soc", 0, 100),
            ("temperature", "temp", -100, 200),
        ):
            value = battery.get(source)
            if _number(value, minimum, maximum) and not (source == "temp" and value == 0):
                values[f"battery.{name}"] = float(value)
            else:
                missing.append(
                    "温度零值有效性待确认"
                    if source == "temp" and value == 0
                    else f"{source} 缺失/无效"
                )
        if type(battery.get("power_on")) is bool:
            values["battery.power_enabled"] = float(battery["power_on"])
        else:
            missing.append("power_on 缺失/无效")
        protection = battery.get("protect")
        if type(protection) is int and 0 <= protection <= 2**32 - 1:
            values["battery.protection"] = float(protection)
        else:
            missing.append("protect 缺失/无效")
        self.accumulator.observe("rp_server.status", values)
        detail = _CACHE_DETAIL + "；Power 为开关状态；SoH 未使用"
        if missing:
            detail += "；" + "；".join(missing)
        self.accumulator.set_health_source("bms", "live", detail[:512])

    def stop(self):
        self.stop_event.set()
        self.thread.join(timeout=3)
