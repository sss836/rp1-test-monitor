"""Fixtures are a fake /proc; no robot processes are inspected or controlled."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import update

from rp1_monitor import aggregation, process_monitor
from rp1_monitor.aggregation import Accumulator
from rp1_monitor.models import DataQuality, ProcessObservation
from rp1_monitor.process_monitor import (
    SIMULATED_WATCHES,
    ProcessProbe,
    build_process_view,
    parse_stat,
    simulate_processes,
)
from rp1_monitor.profiles import ProcessWatch, RobotProfile
from rp1_monitor.storage import latest

WATCH = ProcessWatch(key="motion", label="运控", comm="motion ) worker")


def stat_text(pid=42, comm=WATCH.comm, ticks=100, start=50, rss=512, state="S"):
    fields = [state] + ["0"] * 21
    for index, value in {11: ticks, 12: 10, 17: 4, 19: start, 21: rss}.items():
        fields[index] = str(value)
    return f"{pid} ({comm}) " + " ".join(fields)


def write_process(root, pid=42, **kwargs):
    folder = root / str(pid)
    folder.mkdir(exist_ok=True)
    (folder / "comm").write_text(kwargs.get("comm", WATCH.comm) + "\n")
    (folder / "stat").write_text(stat_text(pid=pid, **kwargs))


def sample(probe, now=1):
    return probe.sample(datetime.now(timezone.utc), now=now)


def test_stat_parser_handles_spaces_and_closing_parentheses():
    result = parse_stat(stat_text())
    assert result == dict(
        pid=42,
        comm=WATCH.comm,
        kernel_state="S",
        cpu_ticks=110,
        threads=4,
        start_ticks=50,
        rss_pages=512,
    )


def test_cpu_warmup_single_core_convention_and_pid_reuse(tmp_path):
    write_process(tmp_path)
    probe = ProcessProbe([WATCH], tmp_path)
    probe.clock_ticks = 100
    rows, values = sample(probe)
    assert rows[0].state == "running" and rows[0].pid == 42
    assert "process.motion.cpu_percent" not in values
    assert values["process.motion.rss_mib"] == 512 * probe.page_bytes / 1024**2
    write_process(tmp_path, ticks=500)
    _, values = sample(probe, 3)
    assert values["process.motion.cpu_percent"] == 200  # Multithreaded process can exceed 100%.
    write_process(tmp_path, start=99, ticks=3)
    _, values = sample(probe, 5)
    assert values["process.motion.observed_restarts"] == 1
    assert "process.motion.cpu_percent" not in values
    assert len(probe.previous) == len(probe.identities) == 1


def test_exit_missing_resources_and_restart_counter(tmp_path):
    write_process(tmp_path)
    probe = ProcessProbe([WATCH], tmp_path)
    sample(probe)
    (tmp_path / "42/comm").unlink()
    rows, values = sample(probe, 3)
    assert rows[0].state == "stopped"
    assert values == {"process.motion.alive": 0, "process.motion.observed_restarts": 0}
    write_process(tmp_path, pid=43, start=80)
    _, values = sample(probe, 5)
    assert values["process.motion.observed_restarts"] == 1
    assert "process.motion.cpu_percent" not in values


def test_ambiguous_name_and_zombie_are_not_healthy(tmp_path):
    write_process(tmp_path)
    write_process(tmp_path, pid=43)
    probe = ProcessProbe([WATCH], tmp_path)
    rows, values = sample(probe)
    assert rows[0].state == "ambiguous" and rows[0].pid is None
    assert "process.motion.alive" not in values
    (tmp_path / "43/comm").unlink()
    write_process(tmp_path, state="Z")
    rows, values = sample(probe, 3)
    assert rows[0].state == "zombie" and values["process.motion.alive"] == 0
    assert "process.motion.rss_mib" not in values


@pytest.mark.parametrize("filename", ["comm", "stat"])
def test_permission_denied_is_unknown_not_zero(tmp_path, monkeypatch, filename):
    write_process(tmp_path)
    original = process_monitor.read_proc

    def restricted(path, *args, **kwargs):
        if Path(path).name == filename:
            raise PermissionError("fixture only")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(process_monitor, "read_proc", restricted)
    rows, values = sample(ProcessProbe([WATCH], tmp_path))
    assert rows[0].state == "unknown"
    assert "process.motion.alive" not in values


def test_disappearing_or_changed_stat_is_unknown(tmp_path):
    write_process(tmp_path)
    (tmp_path / "42/stat").write_text(stat_text(comm="different"))
    rows, values = sample(ProcessProbe([WATCH], tmp_path))
    assert rows[0].state == "unknown" and "process.motion.alive" not in values
    (tmp_path / "42/stat").unlink()
    assert sample(ProcessProbe([WATCH], tmp_path))[0][0].state == "unknown"
    assert sample(ProcessProbe([WATCH], tmp_path / "missing"))[0][0].state == "unknown"


def test_sampling_retains_catalog_and_history_facts_but_clears_current_values(monkeypatch):
    now = [1_000_000_000]
    monkeypatch.setattr(aggregation.time, "monotonic_ns", lambda: now[0])
    acc = Accumulator("TEST", "simulated")
    simulate_processes(acc, SIMULATED_WATCHES, 28)
    now[0] += 1_000_000
    acc.flush()
    now[0] += 2_000_000_000
    simulate_processes(acc, SIMULATED_WATCHES, 30)
    frame = acc.preview()
    assert frame.values["process.demo_driver.cpu_percent"] is None
    assert "process.demo_driver.cpu_percent" in {e["key"] for e in frame.catalog}
    window = acc.flush()
    assert window.metrics["process.demo_driver.cpu_percent"].count == 0
    assert "process.demo_driver.cpu_percent" not in window.quality.health_values
    assert window.metrics["process.demo_driver.alive"].minimum is None
    now[0] += 7_000_000_000
    frame = acc.preview()
    view = build_process_view(frame.processes, frame.values, frame.ages_ms)
    assert view["items"][0]["state"] == "unknown"
    assert view["items"][0]["alive"] is None
    assert view["items"][0]["observed_at"] is not None


def test_profile_rejects_unbounded_or_executable_selectors():
    with pytest.raises(ValidationError):
        ProcessWatch(key="x", label="x", comm="进程名称超限")
    with pytest.raises(ValidationError):
        ProcessWatch(key="x; touch evil", label="x", comm="python")
    with pytest.raises(ValidationError):
        ProcessWatch(key="x", label="x", comm="/usr/bin/python")
    with pytest.raises(ValidationError):
        RobotProfile(robot_id="TEST", processes=[WATCH, WATCH])
    with pytest.raises(ValidationError):
        RobotProfile(
            robot_id="TEST",
            processes=[ProcessWatch(key=f"p{i}", label="x", comm=f"p{i}") for i in range(17)],
        )
    with pytest.raises(ValidationError):
        DataQuality(health_values={"process.motion.command": 1})


def test_processes_reach_central_history_agent_and_expire(client, write_headers, read_headers):
    acc = Accumulator("RP1-TEST", "simulated")
    simulate_processes(acc, SIMULATED_WATCHES, 10)
    payload = acc.flush().model_dump(mode="json")
    response = client.post(
        "/api/v1/robot-telemetry/batches",
        headers=write_headers,
        json={"delivery": "live", "windows": [payload]},
    )
    assert response.status_code == 200, response.text
    snapshot = client.get("/api/v1/robot-monitors", headers=read_headers).json()["items"][0]
    rows = snapshot["health"]["processes"]["items"]
    assert len(rows) == 2 and rows[0]["cpu_percent"] is not None
    assert rows[0]["state"] == "running" and snapshot["source_kind"] == "simulated"
    query = {
        "metric": "process.demo_motion.rss_mib",
        "start": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
        "end": (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat(),
    }
    curve = client.get(
        "/api/v1/robot-monitors/RP1-TEST/telemetry", params=query, headers=read_headers
    )
    assert curve.status_code == 200, curve.text
    assert curve.json()["unit"] == "MiB" and curve.json()["points"][0]["samples"] == 1
    answer = client.post(
        "/api/v1/robot-monitors/RP1-TEST/assistant",
        headers=read_headers,
        json={"question": "运控进程内存是多少？"},
    )
    assert answer.status_code == 200, answer.text
    assert "RSS" in answer.json()["answer"] and answer.json()["mode"] == "local_rules"
    assert answer.json()["evidence"]["E1"]["processes"]["items"][0]["rss_mib"] is not None
    with client.app.state.store.engine.begin() as connection:
        connection.execute(
            update(latest).values(received_at=datetime.now(timezone.utc) - timedelta(seconds=8))
        )
    snapshot = client.get("/api/v1/robot-monitors", headers=read_headers).json()["items"][0]
    assert snapshot["health"]["processes"]["items"][0]["state"] == "unknown"
    assert snapshot["health"]["processes"]["items"][0]["rss_mib"] is None


def test_unknown_does_not_reuse_old_pid_in_view():
    row = ProcessObservation(
        **WATCH.model_dump(), state="running", detail="fixture", pid=42, age_ms=7000
    )
    view = build_process_view(
        [row], {"process.motion.cpu_percent": 10}, {"process.motion.cpu_percent": 1}
    )
    assert view["items"][0]["pid"] is None and view["items"][0]["cpu_percent"] is None
