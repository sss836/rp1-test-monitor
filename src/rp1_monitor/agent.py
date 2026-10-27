"""Robot-only observer and local buffer. No central URL, credentials or uploader."""

import asyncio
import logging
import shutil
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect

from .aggregation import Accumulator
from .board_diagnostics import BoardDiagnostics
from .health import HealthSource
from .health_view import build_health
from .models import ProcessObservation, TransferAck, TransferPage
from .process_monitor import SIMULATED_WATCHES, ProcessSource, build_process_view
from .profiles import load_profile
from .rp_server_source import RpServerSource
from .sources import RosSource, SimulatedSource
from .spool import Spool

log = logging.getLogger(__name__)


@dataclass
class AgentSettings:
    robot_id: str
    source_kind: str
    state_dir: Path = Path("runtime/agent")
    cache_bytes: int = 1024**3
    reserve_bytes: int = 256 * 1024**2
    rp_server_url: str | None = None
    profile_path: Path | None = None
    thermal_demo: bool = False
    simulated_dof: int | None = None
    robopi_tui_executor: Path | None = None


class AgentRuntime:
    def __init__(self, settings: AgentSettings):
        if settings.thermal_demo and settings.source_kind != "simulated":
            raise ValueError("温升演示仅允许模拟来源")
        if settings.simulated_dof not in (None, 24, 28):
            raise ValueError("模拟自由度只允许24或28")
        if settings.simulated_dof is not None and settings.source_kind != "simulated":
            raise ValueError("--simulated-dof仅允许模拟来源；ROS2清单来自实际关节反馈")
        self.settings = settings
        self.accumulator = Accumulator(settings.robot_id, settings.source_kind)
        self.profile = load_profile(settings.profile_path, settings.robot_id)
        if settings.source_kind == "simulated" and not self.profile.processes:
            self.profile.processes = list(SIMULATED_WATCHES)
        self.accumulator.device_profile = self.profile.model_dump()
        self.accumulator.observe_processes(
            [
                ProcessObservation(**watch.model_dump(), state="unknown", detail="等待首次进程采样")
                for watch in self.profile.processes
            ],
            {},
        )
        self.spool = Spool(
            settings.state_dir / "pending.sqlite", settings.cache_bytes, settings.reserve_bytes
        )
        self.spool.bind(settings.robot_id, settings.source_kind)
        self.stop_event = threading.Event()
        self.source = (
            SimulatedSource(
                self.accumulator,
                joints=settings.simulated_dof or 24,
                processes=self.profile.processes,
                thermal_demo=settings.thermal_demo,
            )
            if settings.source_kind == "simulated"
            else RosSource(self.accumulator, self.profile)
        )
        self.health_source = (
            HealthSource(self.accumulator, enable_bms=not settings.rp_server_url)
            if settings.source_kind == "ros2"
            else None
        )
        self.rp_server_source = (
            RpServerSource(self.accumulator, settings.rp_server_url)
            if settings.source_kind == "ros2" and settings.rp_server_url
            else None
        )
        self.process_source = (
            ProcessSource(self.accumulator, self.profile.processes)
            if settings.source_kind == "ros2"
            else None
        )
        if self.health_source:
            self.accumulator.set_health_source(
                "motors", "waiting", "等待运控诊断反馈及设备档案中的电机映射；不初始化硬件"
            )
        self.role = "robot_collector"
        self.collector_error = ""
        self.threads = []
        self.free_disk_bytes = None
        self.disk_observed_at = None
        self.board_diagnostics = BoardDiagnostics(settings)

    def start(self):
        self.source.start()
        if self.health_source:
            self.health_source.start()
        if self.rp_server_source:
            self.rp_server_source.start()
        if self.process_source:
            self.process_source.start()
        self.threads = [
            threading.Thread(target=self.collect, name="telemetry-windows", daemon=True),
        ]
        for thread in self.threads:
            thread.start()

    def collect(self):
        deadline = time.monotonic() + 1
        while not self.stop_event.wait(max(0, deadline - time.monotonic())):
            try:
                window = self.accumulator.flush()
                try:
                    self.free_disk_bytes = shutil.disk_usage(self.settings.state_dir).free
                    self.disk_observed_at = datetime.now(timezone.utc)
                except OSError:
                    self.free_disk_bytes = None
                    self.disk_observed_at = None
                window.quality.cache_discarded_windows = self.spool.status()["discarded_windows"]
                window.quality.collector_status = {**self.spool.status(), "stage": "robot_buffer"}
                self.spool.append(window)
            except Exception:
                self.collector_error = "采集汇总或缓存落盘失败；请检查本机日志与磁盘"
                log.exception("collector failed")
                return
            deadline = time.monotonic() + 1

    def snapshot(self):
        preview = self.accumulator.preview()
        preview.health = build_health(
            preview.values,
            preview.ages_ms,
            preview.health_sources,
            preview.health_sensors,
            preview.joint_names,
        )
        preview.upload = {
            **self.spool.status(),
            "stage": "robot_buffer",
            "destination": "上位机持久缓存；机器人不连接中央服务",
            "collector_error": self.collector_error or getattr(self.source, "error", ""),
            "free_disk_bytes": self.free_disk_bytes,
            "reserve_bytes": self.settings.reserve_bytes,
            "disk_observed_at": self.disk_observed_at.isoformat()
            if self.disk_observed_at
            else None,
        }
        preview.health["processes"] = build_process_view(
            preview.processes, preview.values, preview.ages_ms
        )
        return preview

    def stop(self):
        self.stop_event.set()
        self.board_diagnostics.stop()
        self.source.stop()
        if self.health_source:
            self.health_source.stop()
        if self.rp_server_source:
            self.rp_server_source.stop()
        if self.process_source:
            self.process_source.stop()
        for thread in self.threads:
            thread.join(timeout=4)
        # Preserve the final partial window without pretending a robot action stopped.
        if not self.collector_error:
            self.spool.append(self.accumulator.flush())


def create_preview_app(runtime, title):
    @asynccontextmanager
    async def lifespan(_):
        runtime.start()
        try:
            yield
        finally:
            runtime.stop()

    app = FastAPI(title=title, lifespan=lifespan)
    app.state.runtime = runtime

    @app.get("/api/v1/state")
    def state() -> dict:
        return runtime.snapshot().model_dump(mode="json")

    @app.websocket("/api/v1/telemetry")
    async def telemetry(websocket: WebSocket):
        if websocket.headers.get("origin"):
            await websocket.close(code=1008)
            return
        await websocket.accept()
        try:
            while True:
                try:
                    frame = runtime.snapshot()
                except HTTPException:
                    await websocket.close(code=1013)
                    return
                await websocket.send_json(frame.model_dump(mode="json"))
                await asyncio.sleep(0.1)
        except WebSocketDisconnect:
            return

    from .local_operations import register_local_operations

    register_local_operations(app, runtime)
    return app


def create_agent_app(settings: AgentSettings):
    from typing import Literal

    runtime = AgentRuntime(settings)
    app = create_preview_app(runtime, "RP1 Robot-only Observer")

    @app.get("/api/v1/transfer/windows")
    def windows(delivery: Literal["live", "backfill"] = "backfill") -> TransferPage:
        if delivery == "live":
            newest = runtime.spool.newest()
            fresh = (
                newest is not None
                and newest.stream_id == runtime.accumulator.stream_id
                and newest.sequence == runtime.accumulator.sequence
                and 0 <= (datetime.now(timezone.utc) - newest.window_end).total_seconds() < 3
            )
            selected = [newest] if fresh else []
        else:
            selected = runtime.spool.oldest()
        page = TransferPage(
            robot_id=settings.robot_id,
            source_kind=settings.source_kind,
            stream_id=runtime.accumulator.stream_id,
            sequence=runtime.accumulator.sequence,
            sampled_at=runtime.accumulator.at(time.monotonic_ns()),
            delivery=delivery,
            windows=selected,
        )
        while len(page.model_dump_json().encode()) > 1024 * 1024:
            if len(page.windows) <= 1:
                raise HTTPException(413, "单条摘要超过传输限制，保留机器人缓存")
            page.windows = page.windows[: len(page.windows) // 2]
        return page

    @app.post("/api/v1/transfer/receipts")
    def received(ack: TransferAck) -> dict:
        if ack.robot_id != settings.robot_id:
            raise HTTPException(403, "确认仅适用于绑定机器人")
        try:
            runtime.spool.acknowledge_transfer(ack.receipts)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"status": "acknowledged"}

    return app
