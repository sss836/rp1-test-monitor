"""Component pages expose bounded raw-window evidence, never inferred tracking data."""

import hashlib
import json
import math
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError
from sqlalchemy import select, update

from rp1_monitor.aggregation import Accumulator
from rp1_monitor.health import HealthSource
from rp1_monitor.health_view import build_health
from rp1_monitor.models import MetricRollup, TelemetryWindow
from rp1_monitor.sources import SimulatedSource
from rp1_monitor.storage import history


def rollup(last, low=None, high=None, count=1, **kwargs):
    return MetricRollup(
        last=last,
        minimum=last if low is None else low,
        maximum=last if high is None else high,
        count=count,
        age_ms=10,
        **kwargs,
    )


def query(client, read_headers, component, start, end, **params):
    response = client.get(
        "/api/v1/robot-monitors/RP1-TEST/component-history",
        headers=read_headers,
        params={
            "component": component,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "max_points": 10,
            **params,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def ingest(client, write_headers, sample):
    response = client.post(
        "/api/v1/robot-telemetry/batches",
        headers=write_headers,
        json={"delivery": "live", "windows": [sample.model_dump(mode="json")]},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_battery_full_window_extrema_signed_current_and_nonmonotonic_soc(
    client, window, write_headers, read_headers, monkeypatch
):
    end = datetime.now(timezone.utc)
    sample = window()
    sample.metrics = {
        "battery.current": rollup(-2, -90, 7, count=20),
        "battery.voltage": rollup(48, 41, 56, count=20),
        "battery.soc": rollup(82, 79, 84, count=20),
        "battery.temperature": rollup(32, 31, 39, count=20),
    }
    ingest(client, write_headers, sample)
    result = query(
        client, read_headers, "battery", end - timedelta(minutes=1), end + timedelta(seconds=1)
    )
    assert result["source_kind"] == "simulated" and result["joint"] is None
    assert [curve["metric"] for curve in result["curves"]] == [
        "battery.soc",
        "battery.temperature",
        "battery.voltage",
        "battery.current",
    ]
    assert result["stats"] == {
        "current_peak_abs": 90,
        "current_min": -90,
        "current_max": 7,
        "voltage_min": 41,
        "voltage_max": 56,
        "soc_min": 79,
        "soc_max": 84,
        "temperature_max": 39,
    }
    # The actual SQL statistics must remain independent of any graph decimation.
    monkeypatch.setattr(
        client.app.state.store, "curves", lambda *args, **kwargs: {"points": [{"last": 1}]}
    )
    other = query(
        client, read_headers, "battery", end - timedelta(minutes=1), end + timedelta(seconds=1)
    )
    assert other["stats"] == result["stats"]


def test_rms_is_paired_sample_weighted_in_window_range_and_decimated_bucket(
    client, write_headers, read_headers
):
    accumulator = Accumulator("RP1-TEST", "simulated")
    start = datetime.now(timezone.utc) - timedelta(seconds=100)
    for sequence, errors in enumerate(([1, 3], [2, 2, 2]), 1):
        for error in errors:
            accumulator.observe_tracking({0: (error, 0)})
        sample = accumulator.flush()
        sample.window_start = start + timedelta(seconds=sequence)
        sample.window_end = start + timedelta(seconds=sequence + 1)
        ingest(client, write_headers, sample)
    result = query(client, read_headers, "joints", start, start + timedelta(seconds=100), joint=0)
    assert result["stats"]["tracking_samples"] == 5
    assert result["stats"]["tracking_rms"] == pytest.approx(math.sqrt(22 / 5))
    curve = next(curve for curve in result["curves"] if curve["metric"] == "joint.0.tracking_rms")
    assert len(curve["points"]) == 1
    assert curve["points"][0]["last"] == pytest.approx(math.sqrt(22 / 5))
    assert curve["points"][0]["sum_squares"] == 22
    assert curve["points"][0]["samples"] == 5
    assert curve["points"][0]["minimum"] is None and curve["points"][0]["maximum"] is None
    assert "不是最终电机目标" in result["meaning"]


def test_legacy_or_stale_tracking_rows_cannot_become_rms_by_using_last(
    client, window, write_headers, read_headers
):
    sample = window()
    ingest(client, write_headers, sample)
    payload = sample.model_dump(mode="json")
    metric = {"last": 99, "minimum": 99, "maximum": 99, "count": 20, "age_ms": 10}
    for altered in (
        metric,
        {**metric, "sum_squares": 196020, "pairing": "same_sample", "age_ms": 1001},
    ):
        payload["metrics"]["joint.0.tracking_rms"] = altered
        with client.app.state.store.engine.begin() as connection:
            connection.execute(update(history).values(payload=payload))
        result = query(
            client,
            read_headers,
            "joints",
            sample.window_start - timedelta(seconds=1),
            sample.window_end + timedelta(seconds=1),
        )
        assert result["stats"]["tracking_rms"] is None
        assert result["stats"]["tracking_samples"] == 0
        curve = next(
            curve for curve in result["curves"] if curve["metric"].endswith("tracking_rms")
        )
        assert all(point["last"] is None and point["samples"] == 0 for point in curve["points"])


def test_host_history_selects_any_zone_and_peak_includes_all_reported_zones(
    client, window, write_headers, read_headers
):
    sample = window()
    sample.metrics.update(
        {"host.cpu.usage": rollup(20, 10, 91), "host.memory.usage": rollup(40, 20, 70)}
    )
    for index in (0, 2, 9, 10, 11):
        sample.metrics[f"host.thermal.{index}.temperature"] = rollup(40 + index, 30, 60 + index)
    sample.metrics["host.thermal.11.temperature"] = rollup(150, 30, 150)
    ingest(client, write_headers, sample)
    result = query(
        client,
        read_headers,
        "host",
        sample.window_start - timedelta(seconds=1),
        sample.window_end + timedelta(seconds=1),
    )
    assert [curve["metric"] for curve in result["curves"]] == [
        "host.cpu.usage",
        "host.memory.usage",
        "host.thermal.0.temperature",
    ]
    assert result["thermal_metrics"] == [
        f"host.thermal.{index}.temperature" for index in (0, 2, 9, 10, 11)
    ]
    assert result["selected_thermal"] == "host.thermal.0.temperature"
    assert result["stats"] == {"cpu_peak": 91, "memory_peak": 70, "temperature_peak": 150}
    selected = query(
        client,
        read_headers,
        "host",
        sample.window_start - timedelta(seconds=1),
        sample.window_end + timedelta(seconds=1),
        thermal="host.thermal.11.temperature",
    )
    assert selected["selected_thermal"] == "host.thermal.11.temperature"
    assert selected["curves"][-1]["metric"] == "host.thermal.11.temperature"
    assert selected["stats"] == result["stats"]


def test_component_history_auth_empty_missing_robot_and_range_validation(client, read_headers):
    path = "/api/v1/robot-monitors/RP1-TEST/component-history"
    assert client.get(path, params={"component": "battery"}).status_code == 401
    for params in (
        {"component": "other"},
        {"component": "joints", "joint": 64},
        {"component": "battery", "max_points": 4001},
        {"component": "host", "start": "2026-01-01T00:00:00"},
        {"component": "host", "thermal": "battery.soc"},
        {"component": "host", "thermal": "host.thermal.31.temperature"},
        {"component": "host", "start": "2026-01-01T00:00:00Z", "end": "2026-04-01T00:00:00Z"},
    ):
        assert client.get(path, params=params, headers=read_headers).status_code == 422
    assert (
        client.get(
            path.replace("RP1-TEST", "MISSING"), params={"component": "host"}, headers=read_headers
        ).status_code
        == 404
    )
    result = client.get(path, params={"component": "battery"}, headers=read_headers).json()
    assert all(value is None for value in result["stats"].values())
    assert all(not curve["points"] for curve in result["curves"])
    for page in ("battery", "joints", "host"):
        assert client.get(f"/robots/RP1-TEST/{page}").status_code == 200


def test_new_optional_rms_fields_preserve_legacy_wire_hash_and_duplicate_retry(
    client, window, write_headers, read_headers
):
    sample = window()
    payload = sample.model_dump(mode="json")
    assert all(
        set(value) == {"last", "minimum", "maximum", "count", "age_ms"}
        for value in payload["metrics"].values()
    )
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    assert ingest(client, write_headers, sample)["inserted"] == 1
    assert ingest(client, write_headers, TelemetryWindow.model_validate(payload))["duplicates"] == 1
    query(
        client,
        read_headers,
        "joints",
        sample.window_start - timedelta(seconds=1),
        sample.window_end + timedelta(seconds=1),
    )
    with client.app.state.store.engine.connect() as connection:
        row = connection.execute(select(history.c.payload, history.c.payload_hash)).one()
    assert row.payload == payload and row.payload_hash == hashlib.sha256(canonical).hexdigest()


def test_tracking_requires_explicit_fresh_pairs_not_named_topic_last_values(window):
    accumulator = Accumulator("RP1-TEST", "ros2")
    accumulator.set_mapping(["hip"], "matched_names", "名称已匹配，未证明同步")
    accumulator.observe("/joint_states", {"joint.0.position": 5})
    accumulator.observe("/action", {"joint.0.policy_target": 1})
    assert accumulator.observe_tracking({0: (5, 1)}) is False
    assert "joint.0.tracking_rms" not in accumulator.preview().values
    for metric, source, quality in (
        (rollup(2, count=1), "simulated", {}),
        (
            rollup(2, count=1, sum_squares=4, pairing="same_sample"),
            "ros2",
            {"target_alignment": "unverified"},
        ),
    ):
        sample = window().model_dump(mode="json")
        sample["source_kind"] = source
        sample["quality"].update(quality)
        sample["metrics"]["joint.0.tracking_rms"] = metric.model_dump(mode="json")
        with pytest.raises(ValidationError, match="配对"):
            TelemetryWindow.model_validate(sample)
    with pytest.raises(ValidationError):
        MetricRollup(
            last=1e308, minimum=0, maximum=1e308, count=1, sum_squares=1, pairing="same_sample"
        )
    simulated = Accumulator("RP1-TEST", "simulated")
    assert simulated.observe_tracking({0: (5, 1)}, event_age_ms=1001) is False
    assert "joint.0.tracking_rms" not in simulated.flush().metrics


def test_robot_uptime_reads_proc_and_simulation_uses_source_start(tmp_path, monkeypatch):
    accumulator = Accumulator("RP1-TEST", "ros2")
    proc = tmp_path / "proc"
    proc.mkdir()
    (proc / "uptime").write_text("12345.67 99999.1\n")
    source = HealthSource(accumulator, proc_root=proc, sysfs_root=tmp_path / "sys")
    source._poll_host()
    preview = accumulator.preview()
    assert preview.values["host.uptime.seconds"] == 12345.67
    health = build_health(
        preview.values, preview.ages_ms, preview.health_sources, preview.health_sensors
    )
    assert health["host"]["uptime_seconds"] == 12345.67
    health = build_health(preview.values, {**preview.ages_ms, "host.uptime.seconds": 5001}, {}, {})
    assert health["host"]["uptime_seconds"] is None
    simulated = Accumulator("RP1-TEST", "simulated")
    demo = SimulatedSource(simulated, joints=1, processes=[])
    clock = iter((1000, 1007))
    monkeypatch.setattr("rp1_monitor.sources.time.monotonic", lambda: next(clock))

    class OneCycle:
        stopped = False

        def is_set(self):
            return self.stopped

        def wait(self, _duration):
            self.stopped = True

    demo.stop_event = OneCycle()
    demo.run()
    assert simulated.preview().values["host.uptime.seconds"] == 7
    assert simulated.flush().metrics["joint.0.tracking_rms"].pairing == "same_sample"
