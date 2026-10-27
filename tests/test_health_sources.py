import json
import sys
import threading
from datetime import datetime, timedelta, timezone

import pytest

from rp1_monitor.health import HealthSource, parse_bms_message, parse_journal_entry


class Recorder:
    def __init__(self):
        self.observations = []
        self.states = {}
        self.sensors = {}
        self.observed = threading.Event()

    def observe(self, topic, values, publisher_stamp=None, *, event_age_ms=0):
        self.observations.append((topic, values, publisher_stamp))

    def set_health_source(self, name, state, detail):
        self.states[name] = (state, detail)
        if name == "bms" and state == "live":
            self.observed.set()

    def set_health_sensors(self, sensors):
        self.sensors = sensors


def test_bms_signed_current_decimal_soc_and_power_is_a_switch():
    parsed = parse_bms_message(
        "[BMS Data] Voltage: +48.6V | Current: -2.35A | SoC: 86.5% | Power: ON"
    )
    assert parsed == {
        "battery.voltage": 48.6,
        "battery.current": -2.35,
        "battery.soc": 86.5,
        "battery.power_enabled": 1.0,
    }
    assert "battery.power" not in parsed
    assert parse_bms_message("[BMS Data] Voltage: 48V | Current: .5A | SoC: 0%") == {
        "battery.voltage": 48.0,
        "battery.current": 0.5,
        "battery.soc": 0.0,
    }
    assert (
        parse_bms_message("[BMS Data] Voltage: 48V | Current: 0A | SoC: 100% | Power: OFF")[
            "battery.power_enabled"
        ]
        == 0
    )


def test_nrf_pmic_uses_bms_voltage_not_power_rail_name():
    assert parse_bms_message("[NRF PMIC] NRF v2 | 48V=OFF 5V=ON | BMS 51.7V -1.25A 84.5%") == {
        "battery.voltage": 51.7,
        "battery.current": -1.25,
        "battery.soc": 84.5,
        "battery.rail48_enabled": 0.0,
        "battery.rail5_enabled": 1.0,
    }
    other = parse_bms_message("[NRF PMIC] NRF v2 | 48V=ON 5V=OFF | BMS 51.7V -1.25A 84.5%")
    assert other["battery.rail48_enabled"] == 1 and other["battery.rail5_enabled"] == 0
    assert "battery.power_enabled" not in other


@pytest.mark.parametrize("soc", [0.5, 86.5, 100.0])
def test_gfcan_journal_format_is_recognised_and_soc_is_already_percentage(soc):
    message = f"[CAN BMS Data] Voltage: 51.7V | Current: -1.25A | SoC: {soc}%"
    expected = {"battery.voltage": 51.7, "battery.current": -1.25, "battery.soc": soc}
    assert parse_bms_message(message) == expected
    now = datetime.now(timezone.utc)
    values, _ = parse_journal_entry(journal_row(now, message), now=now)
    assert values == expected
    assert "battery.power_enabled" not in values


@pytest.mark.parametrize(
    "message",
    [
        "[BMS Data] Voltage: nanV | Current: 1A | SoC: 20%",
        "[BMS Data] Voltage: 48V | Current: infA | SoC: 20%",
        "[BMS Data] Voltage: -48V | Current: 1A | SoC: 20%",
        "[BMS Data] Voltage: 48V | Current: 1001A | SoC: 20%",
        "[BMS Data] Voltage: 48V | Current: 1A | SoC: 101%",
        "[BMS Data] Voltage: 48V | Current: 1A | SoC: -1%",
        "[BMS Data] Voltage: 48V | Current: 1A",
        "[BMS Data] Voltage: 48V | Current: 1A | SoC: 20% | Power: 300W",
        "[BMS Data] Voltage: 48V | Current: 1A | SoC: 20% garbage",
        "[CAN BMS Data] Voltage: -48V | Current: 1A | SoC: 20%",
        "[CAN BMS Data] Voltage: 48V | Current: 1A | SoC: 101%",
        None,
    ],
)
def test_bms_invalid_fields_rejected(message):
    assert parse_bms_message(message) is None


def journal_row(stamp, message=None):
    return json.dumps(
        {
            "__REALTIME_TIMESTAMP": str(int(stamp.timestamp() * 1e6)),
            "MESSAGE": message or "[BMS Data] Voltage: 48V | Current: -2A | SoC: 86%",
        }
    )


def test_journal_requires_current_record_time_and_does_not_replay_old_values():
    now = datetime.now(timezone.utc)
    values, stamp = parse_journal_entry(journal_row(now), now=now)
    assert values["battery.current"] == -2
    assert abs((stamp - now).total_seconds()) < 0.000001
    assert parse_journal_entry(journal_row(now - timedelta(seconds=6)), now=now) is None
    assert parse_journal_entry(journal_row(now + timedelta(seconds=2)), now=now) is None
    for raw in ("permission denied", "null", "[]", "{}", '{"__REALTIME_TIMESTAMP": "oops"}'):
        assert parse_journal_entry(raw, now=now) is None


def test_host_reads_real_files_and_cpu_needs_two_samples(tmp_path):
    proc = tmp_path / "proc"
    proc.mkdir()
    (proc / "stat").write_text("cpu 100 0 50 850 0 0 0 0 0 0\n")
    (proc / "meminfo").write_text("MemTotal: 1000 kB\nMemAvailable: 250 kB\n")
    thermal = tmp_path / "sys" / "class" / "thermal"
    zone = thermal / "thermal_zone2"
    zone.mkdir(parents=True)
    (zone / "type").write_text("soc-thermal\n")
    (zone / "temp").write_text("45625\n")
    missing_zone = thermal / "thermal_zone0"
    missing_zone.mkdir()
    (missing_zone / "type").write_text("cpu\n")
    recorder = Recorder()
    source = HealthSource(recorder, sysfs_root=tmp_path / "sys", proc_root=proc)
    source._poll_host()
    first = recorder.observations[-1][1]
    assert first == {"host.thermal.2.temperature": 45.625, "host.memory.usage": 75.0}
    assert recorder.sensors == {"host.thermal.2.temperature": "soc-thermal"}
    assert "thermal_zone0" in recorder.states["host"][1]
    (proc / "stat").write_text("cpu 120 0 60 920 0 0 0 0 0 0\n")
    source._poll_host()
    assert recorder.observations[-1][1]["host.cpu.usage"] == 30
    # Guest counters are already included in user/nice and must not be counted twice.
    (proc / "stat").write_text("cpu 140 0 70 990 0 0 0 0 500 500\n")
    source._poll_host()
    assert recorder.observations[-1][1]["host.cpu.usage"] == 30


def test_missing_or_invalid_host_sources_never_emit_zero(tmp_path):
    recorder = Recorder()
    source = HealthSource(recorder, sysfs_root=tmp_path / "sys", proc_root=tmp_path / "proc")
    source._poll_host()
    assert recorder.observations == []
    assert recorder.states["host"][0] == "error"
    proc = tmp_path / "proc"
    proc.mkdir()
    (proc / "stat").write_text("cpu 100 0 50 850\n")
    (proc / "meminfo").write_text("MemTotal: 1000 kB\nMemFree: 250 kB\n")
    source._poll_host()
    assert recorder.observations == []
    (proc / "stat").write_text("cpu 10 0 5 85\n")  # Counters reset.
    source._poll_host()
    assert recorder.observations == []


def test_many_unreadable_sensors_still_produce_a_bounded_visible_error(tmp_path):
    thermal = tmp_path / "class" / "thermal"
    for index in range(64):
        (thermal / f"thermal_zone{index}").mkdir(parents=True)
    recorder = Recorder()
    HealthSource(recorder, sysfs_root=tmp_path, proc_root=tmp_path / "proc")._poll_host()
    assert recorder.states["host"][0] == "error"
    assert len(recorder.states["host"][1]) <= 512


def test_follow_stream_ignores_old_rows_and_stop_only_ends_own_process(tmp_path):
    recorder = Recorder()
    now = datetime.now(timezone.utc)
    old = journal_row(now - timedelta(seconds=60))
    current = journal_row(now)
    program = (
        f"import sys,time; print({old!r},flush=True); print({current!r},flush=True); time.sleep(30)"
    )
    source = HealthSource(
        recorder,
        sysfs_root=tmp_path / "sys",
        proc_root=tmp_path / "proc",
        journal_command=[sys.executable, "-u", "-c", program],
    )
    source.start()
    try:
        assert recorder.observed.wait(3)
        observations = [row for row in recorder.observations if row[0] == "bms.journal"]
        assert len(observations) == 1
        assert observations[0][1]["battery.soc"] == 86
        assert recorder.states["bms"][0] == "live"
        assert "不能证明" in recorder.states["bms"][1]
        process = source._process
    finally:
        source.stop()
    assert process.poll() is not None
    assert not source._bms_thread.is_alive() and not source._host_thread.is_alive()


def test_recent_old_log_retains_event_age_and_duplicate_never_renews_it(tmp_path):
    from rp1_monitor.aggregation import Accumulator

    accumulator = Accumulator("TEST", "ros2")
    stamp = datetime.now(timezone.utc) - timedelta(seconds=4)
    row = journal_row(stamp)
    program = f"import time; print({row!r},flush=True); print({row!r},flush=True); time.sleep(30)"
    source = HealthSource(
        accumulator,
        sysfs_root=tmp_path,
        proc_root=tmp_path,
        journal_command=[sys.executable, "-u", "-c", program],
    )
    got_duplicate = threading.Event()
    original = accumulator.set_health_source

    def receive(name, state, detail):
        original(name, state, detail)
        if "重复" in detail:
            got_duplicate.set()

    accumulator.set_health_source = receive
    source.start()
    try:
        assert got_duplicate.wait(2)
        frame = accumulator.preview()
        assert 4000 <= frame.ages_ms["battery.soc"] <= 5000
        assert accumulator._counts["bms.journal"] == 1
        # Advance the source's monotonic age, without a slow wall-clock sleep.
        with accumulator._lock:
            value, received = accumulator._latest["battery.soc"]
            accumulator._latest["battery.soc"] = value, received - 2_000_000_000
        assert accumulator.preview().values["battery.soc"] is None
    finally:
        source.stop()


def test_missing_journalctl_is_visible_and_does_not_kill_host_thread(tmp_path):
    recorder = Recorder()
    source = HealthSource(
        recorder,
        sysfs_root=tmp_path / "sys",
        proc_root=tmp_path / "proc",
        journal_command=[str(tmp_path / "does-not-exist")],
    )
    # Exercise one failed launch without a timing-sensitive sleep.
    original = recorder.set_health_source

    def stop_on_error(name, state, detail):
        original(name, state, detail)
        if name == "bms" and state == "error":
            source.stop_event.set()

    recorder.set_health_source = stop_on_error
    source._run_bms()
    assert recorder.states["bms"][0] == "error"
    assert "无法读取" in recorder.states["bms"][1]


def test_host_only_does_not_start_bms_process_or_override_other_bms_source(tmp_path):
    recorder = Recorder()
    recorder.set_health_source("bms", "live", "现有 rp_server 的只读数据")
    source = HealthSource(
        recorder,
        sysfs_root=tmp_path / "sys",
        proc_root=tmp_path / "proc",
        journal_command=[str(tmp_path / "does-not-exist")],
        enable_bms=False,
    )
    source.start()
    source.stop()
    assert recorder.states["bms"] == ("live", "现有 rp_server 的只读数据")
    assert source._process is None
    assert source._bms_thread.ident is None


@pytest.mark.parametrize("failure", ["permission", "oversized"])
def test_journal_read_failures_are_visible_and_oversized_data_is_not_observed(tmp_path, failure):
    recorder = Recorder()
    program = (
        "print('Failed to open journal: Permission denied',flush=True)"
        if failure == "permission"
        else "import sys; sys.stdout.write('x' * 300000 + '\\n'); sys.stdout.flush()"
    )
    source = HealthSource(
        recorder,
        sysfs_root=tmp_path / "sys",
        proc_root=tmp_path / "proc",
        journal_command=[sys.executable, "-u", "-c", program],
    )
    original = recorder.set_health_source

    def stop_on_error(name, state, detail):
        original(name, state, detail)
        if state == "error":
            source.stop_event.set()

    recorder.set_health_source = stop_on_error
    source._run_bms()
    assert recorder.states["bms"][0] == "error"
    assert recorder.observations == []
    assert ("权限" if failure == "permission" else "256 KiB") in recorder.states["bms"][1]
