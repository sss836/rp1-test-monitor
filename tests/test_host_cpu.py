"""Synthetic Linux files verify sampling math, topology and missing data semantics."""

import time

import pytest

from rp1_monitor.aggregation import Accumulator
from rp1_monitor.health import HealthSource
from rp1_monitor.health_view import build_health
from rp1_monitor.models import TelemetryWindow, metric_spec


def linux_files(tmp_path, capacities=(397, 397, 1024, 1024)):
    proc, sys = tmp_path / "proc", tmp_path / "sys"
    proc.mkdir()
    (proc / "meminfo").write_text("MemTotal: 1000 kB\nMemAvailable: 500 kB\n")
    for index, capacity in enumerate(capacities):
        path = sys / "devices" / "system" / "cpu" / f"cpu{index}"
        path.mkdir(parents=True)
        if capacity is not None:
            (path / "cpu_capacity").write_text(str(capacity))
    return proc, sys


def stat(proc, rows):
    (proc / "stat").write_text("\n".join(
        f"{name} {busy} 0 0 {idle} 0 0 0 0 700 800"
        for name, busy, idle in rows
    ) + "\n")


def view(accumulator, available=True):
    frame = accumulator.preview()
    return build_health(frame.values, frame.ages_ms, frame.health_sources,
                        frame.health_sensors, available=available)["host"]


def test_per_core_counts_not_divided_by_core_count_and_topology_round_trip(tmp_path):
    proc, sys = linux_files(tmp_path)
    acc = Accumulator("SYNTHETIC-CPU", "ros2")
    source = HealthSource(acc, proc_root=proc, sysfs_root=sys, enable_bms=False)
    stat(proc, [("cpu", 100, 300), *[(f"cpu{i}", 25, 75) for i in range(4)]])
    source._poll_host()
    assert [row["usage_percent"] for row in view(acc)["cores"]] == [None] * 4
    stat(proc, [("cpu", 250, 550), ("cpu0", 25, 175), ("cpu1", 50, 150),
                ("cpu2", 125, 75), ("cpu3", 50, 150)])
    source._poll_host()
    host = view(acc)
    assert host["cpu_usage"] == 37.5
    assert [row["usage_percent"] for row in host["cores"]] == [0, 25, 100, 25]
    assert [row["group"] for row in host["cores"]] == ["little", "little", "big", "big"]
    assert "cpu_capacity" in host["cpu_topology_note"]
    time.sleep(0.001)
    window = TelemetryWindow.model_validate_json(acc.flush().model_dump_json())
    assert window.metrics["host.cpu.2.usage"].maximum == 100
    assert window.quality.health_sensors["host.cpu.2.group"] == "big"
    assert metric_spec("host.cpu.2.usage")["unit"] == "%"
    assert all(row["usage_percent"] is None for row in view(acc, False)["cores"])


@pytest.mark.parametrize("capacities", [(None, None), (397, None), (1024, 1024), (200, 397, 1024)])
def test_does_not_guess_big_little_by_cpu_index_or_incomplete_capacities(tmp_path, capacities):
    proc, sys = linux_files(tmp_path, capacities)
    acc = Accumulator("SYNTHETIC-CPU", "ros2")
    source = HealthSource(acc, proc_root=proc, sysfs_root=sys, enable_bms=False)
    stat(proc, [("cpu", 10, 90)])
    source._poll_host()
    assert all(row["group"] == "unknown" for row in view(acc)["cores"])
    assert "不按编号" in view(acc)["cpu_topology_note"]


def test_reset_offline_reappearance_and_broken_stat_never_reuse_last_live_value(tmp_path):
    proc, sys = linux_files(tmp_path, (397, 1024))
    acc = Accumulator("SYNTHETIC-CPU", "ros2")
    source = HealthSource(acc, proc_root=proc, sysfs_root=sys, enable_bms=False)
    stat(proc, [("cpu0", 10, 90), ("cpu1", 10, 90)])
    source._poll_host()
    stat(proc, [("cpu0", 50, 150), ("cpu1", 50, 150)])
    source._poll_host()
    assert [row["usage_percent"] for row in view(acc)["cores"]] == [40, 40]
    stat(proc, [("cpu0", 1, 9)])  # cpu0 reset, cpu1 offline.
    source._poll_host()
    assert [row["usage_percent"] for row in view(acc)["cores"]] == [None, None]
    stat(proc, [("cpu0", 11, 99), ("cpu1", 50, 150)])
    source._poll_host()
    assert [row["usage_percent"] for row in view(acc)["cores"]] == [10, None]
    (proc / "stat").write_text("cpu0 broken\n")
    source._poll_host()
    assert [row["usage_percent"] for row in view(acc)["cores"]] == [None, None]


def test_core_view_expiry_and_older_collector():
    key = "host.cpu.0.usage"
    sensors = {key: "CPU 0", "host.cpu.0.group": "big", "host.cpu.0.state": "live"}
    for value, age in [(12, 5001), (120, 1), (12, None)]:
        host = build_health({key: value}, {key: age}, {}, sensors)["host"]
        assert host["cores"][0]["usage_percent"] is None
    assert build_health({}, {}, {}, {})["host"]["cores"] == []


def test_individual_counter_regression_is_not_hidden_by_other_busy_ticks(tmp_path):
    proc, sys = linux_files(tmp_path, (397,))
    acc = Accumulator("SYNTHETIC-CPU", "ros2")
    source = HealthSource(acc, proc_root=proc, sysfs_root=sys, enable_bms=False)
    (proc / "stat").write_text("cpu0 100 0 100 800 0 0 0 0\n")
    source._poll_host()
    (proc / "stat").write_text("cpu0 120 0 120 860 0 0 0 0\n")
    source._poll_host()
    assert view(acc)["cores"][0]["usage_percent"] == 40
    # user regresses despite a positive sum; a bogus 50% delta must be refused.
    (proc / "stat").write_text("cpu0 100 0 190 910 0 0 0 0\n")
    source._poll_host()
    assert view(acc)["cores"][0]["usage_percent"] is None
