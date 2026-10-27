"""Disposable local /proc verification and incremental polling cost, never a robot test."""

import argparse
import gc
import json
import os
import secrets
import statistics
import subprocess
import sys
import time
import tracemalloc
from datetime import datetime, timezone
from pathlib import Path

from rp1_monitor.aggregation import Accumulator
from rp1_monitor.process_monitor import ProcessProbe
from rp1_monitor.profiles import ProcessWatch


def launch(comm):
    # prctl changes only this newly-created child process's name; never a service.
    code = "import ctypes,sys,time; ctypes.CDLL(None).prctl(15,sys.argv[1].encode(),0,0,0); data=bytearray(8*1024**2); print('ready',flush=True); time.sleep(120)"
    child = subprocess.Popen([sys.executable, "-c", code, comm], stdout=subprocess.PIPE, text=True)
    assert child.stdout.readline().strip() == "ready"
    return child


def stop(child):
    if child.poll() is None:
        child.terminate()
        child.wait(timeout=5)
    child.stdout.close()


def run(iterations):
    watches = [
        ProcessWatch(key=f"p{i}", label=f"测试子进程{i}", comm=f"pmon{secrets.token_hex(3)}{i}")
        for i in range(10)
    ]
    children = []
    try:
        for watch in watches:
            children.append(launch(watch.comm))
        probe = ProcessProbe(watches)
        acc = Accumulator("PROCESS-SOFTWARE-TEST", "simulated")

        def poll():
            rows, values = probe.sample(acc.at(time.monotonic_ns()))
            acc.observe_processes(rows, values)
            return rows, values

        poll()
        rows, values = poll()
        assert all(row.state == "running" for row in rows)
        assert all(values[f"process.p{i}.rss_mib"] > 0 for i in range(10))
        assert all(values[f"process.p{i}.cpu_percent"] >= 0 for i in range(10))
        durations = []
        cpu_start = time.process_time()
        for _ in range(iterations):
            start = time.perf_counter()
            poll()
            durations.append((time.perf_counter() - start) * 1000)
        cpu_seconds = time.process_time() - cpu_start
        # Separately measure retained allocations, so tracing cannot inflate CPU results.
        tracemalloc.start()
        for _ in range(10):
            poll()
        gc.collect()
        baseline, _ = tracemalloc.get_traced_memory()
        for _ in range(iterations):
            poll()
        gc.collect()
        allocated, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        stop(children[0])
        rows, values = poll()
        assert rows[0].state == "stopped" and values["process.p0.alive"] == 0
        assert "process.p0.rss_mib" not in values
        children[0] = launch(watches[0].comm)
        rows, values = poll()
        assert rows[0].state == "running" and values["process.p0.observed_restarts"] == 1
        assert "process.p0.cpu_percent" not in values
        frame = acc.preview()
        assert frame.values["process.p0.cpu_percent"] is None
        return {
            "run_at_utc": datetime.now(timezone.utc).isoformat(),
            "scope": "local_linux_software_only_not_RP1_hardware_acceptance",
            "source_kind": "simulated",
            "controlled_test_children": 10,
            "visible_pid_count": sum(name.isdecimal() for name in os.listdir("/proc")),
            "iterations": iterations,
            "poll_seconds": 2,
            "median_poll_wall_ms": round(statistics.median(durations), 3),
            "p95_poll_wall_ms": round(sorted(durations)[int(len(durations) * 0.95)], 3),
            "cpu_ms_per_poll": round(cpu_seconds / iterations * 1000, 3),
            "estimated_incremental_single_core_cpu_percent_at_2s": round(
                cpu_seconds / iterations / 2 * 100, 4
            ),
            "retained_traced_growth_bytes": allocated - baseline,
            "peak_traced_bytes": peak,
            "previous_counter_entries": len(probe.previous),
            "identity_entries": len(probe.identities),
            "metric_entries": len(acc._latest),
            "exit_restart_and_cpu_warmup_verified": True,
            "note": "只测新增进程轮询，不含ROS/SQLite/HTTP/完整采集端RSS；RSS指标为近似值。无机器人实时性或内存占用保证。",
        }
    finally:
        for child in children:
            stop(child)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=200)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not 20 <= args.iterations <= 2000:
        parser.error("iterations must be 20..2000")
    result = json.dumps(run(args.iterations), ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(result + "\n")
    print(result)
