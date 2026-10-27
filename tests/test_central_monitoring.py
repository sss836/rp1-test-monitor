"""SQLite + deterministic clocks only. PostgreSQL/Docker deployment waits for review."""

import gzip
import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select, update
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from rp1_monitor.central_data import (
    archive_range,
    backfill_key_windows,
    retention_preview,
    usage_report,
    verify_archive,
)
from rp1_monitor.central_policy import CentralPolicy
from rp1_monitor.central_tables import (
    alert_heads,
    archive_manifests,
    daily_usage,
    key_windows,
    policies,
)
from rp1_monitor.models import MetricRollup, RelayStatus, TelemetryBatch
from rp1_monitor.monitor_alerts import AlertMonitor, accept_relay_status, conditions
from rp1_monitor.storage import Store, history, token_digest

TOKEN = "software-test-" + "x" * 32


@pytest.fixture
def store(tmp_path):
    value = Store(
        f"sqlite:///{tmp_path / 'monitor.sqlite'}",
        policy=CentralPolicy(key_patterns=["battery.*"], alert_hold_seconds=2),
    )
    value.configure(
        [dict(robot_id="RP1-TEST", source_kind="simulated", name="software only", token=TOKEN)]
    )
    yield value
    value.engine.dispose()


def ingest(store, value, delivery="live"):
    actor = store.device_for_digest(token_digest(TOKEN))
    return store.ingest(TelemetryBatch(delivery=delivery, windows=[value]), actor)


def battery_window(window, **kwargs):
    value = window(**kwargs)
    value.metrics["battery.temperature"] = MetricRollup(
        last=-0.0, minimum=-0.0, maximum=-0.0, count=1, age_ms=1
    )
    return value


def test_projection_whitelist_units_atomic_usage_and_idempotency(store, window):
    value = battery_window(window)
    assert ingest(store, value)["inserted"] == 1
    assert ingest(store, value)["duplicates"] == 1
    with store.engine.connect() as connection:
        row = connection.execute(select(key_windows)).mappings().one()
        assert set(row["metrics"]) == {"battery.temperature"}
        assert row["metrics"]["battery.temperature"]["unit"] == "°C"
        assert row["metrics"]["battery.temperature"]["count"] == 1
        assert row["policy_sha256"] == store.policy.digest
        assert connection.execute(select(daily_usage.c.windows)).scalar_one() == 1
    summary = usage_report(store)
    assert (
        summary["physical_bytes"] > 0
        and summary["items"][0]["key_json_bytes"] < summary["items"][0]["raw_json_bytes"]
    )
    value.metrics["battery.temperature"].last = 1
    with pytest.raises(Exception, match="内容不一致"):
        ingest(store, value)
    assert usage_report(store)["items"][0]["windows"] == 1


def test_missing_metric_stays_gap_in_key_history_and_old_policy_is_immutable(store, window):
    value = battery_window(window)
    value.metrics["battery.temperature"] = MetricRollup(last=None, count=0, age_ms=6000)
    ingest(store, value)
    start, end = value.window_end - timedelta(seconds=2), value.window_end + timedelta(seconds=2)
    curve = store.curves("RP1-TEST", "battery.temperature", start, end, 20, tier="key")
    assert curve["points"][0]["last"] is None and curve["points"][0]["missing_windows"] == 1
    assert (
        store.curves("RP1-TEST", "joint.0.position", start, end, 20, tier="key")["points"][0][
            "last"
        ]
        is None
    )
    assert (
        store.curves("RP1-TEST", "joint.0.position", start, end, 20)["points"][0]["maximum"] == 99
    )
    old_digest = store.policy.digest
    store.policy = CentralPolicy(key_patterns=["joint.*.position"])
    ingest(store, window(sequence=2))
    with store.engine.connect() as connection:
        assert connection.execute(select(func.count()).select_from(policies)).scalar_one() == 2
        assert (
            connection.execute(
                select(key_windows.c.policy_sha256).order_by(key_windows.c.window_id)
            )
            .scalars()
            .first()
            == old_digest
        )


def test_archive_copy_checksum_hour_segments_late_backfill_and_no_deletion(store, window, tmp_path):
    value = battery_window(window, offset=-31 * 86400)
    ingest(store, value, "backfill")
    now = datetime.now(timezone.utc)
    before = retention_preview(store, now)["items"][0]
    assert before["windows"] == 1 and before["archive_manifest_covered_windows"] == 0
    result = archive_range(
        store,
        "RP1-TEST",
        value.window_end - timedelta(seconds=1),
        value.window_end + timedelta(seconds=1),
        tmp_path / "archive",
    )
    manifest = result["items"][0]
    path = tmp_path / "archive" / manifest["file_name"]
    assert verify_archive(path, manifest)["verified"]
    with gzip.open(path, "rt") as handle:
        header, record = [json.loads(line) for line in handle]
    assert header["policies"][store.policy.digest] == store.policy.model_dump()
    assert "-0.0" in json.dumps(record["payload"])
    assert not result["source_deleted"]
    ingest(store, battery_window(window, sequence=2, offset=-31 * 86400), "backfill")
    after = retention_preview(store, now)
    assert after["dry_run"] and not after["deletion_enabled"]
    assert (
        after["items"][0]["windows"] == 2
        and after["items"][0]["archive_manifest_covered_windows"] == 1
    )
    path.write_bytes(path.read_bytes() + b"corruption")
    with pytest.raises(ValueError, match="校验值"):
        verify_archive(path, manifest)
    with store.engine.connect() as connection:
        assert connection.execute(select(func.count()).select_from(history)).scalar_one() == 2


def test_archive_rejects_corrupt_evidence_without_certifying_or_deleting(store, window, tmp_path):
    value = battery_window(window)
    ingest(store, value)
    with store.engine.begin() as connection:
        connection.execute(update(history).values(payload_hash="0" * 64))
    with pytest.raises(ValueError, match="原始摘要哈希"):
        archive_range(
            store,
            "RP1-TEST",
            value.window_start,
            value.window_end + timedelta(seconds=1),
            tmp_path / "archive",
        )
    with store.engine.connect() as connection:
        assert (
            connection.execute(select(func.count()).select_from(archive_manifests)).scalar_one()
            == 0
        )
        assert connection.execute(select(func.count()).select_from(history)).scalar_one() == 1


def snapshot():
    return dict(
        robot_id="RP1-TEST",
        source_kind="simulated",
        enabled=True,
        online=True,
        clock_unverified=False,
        stream_id="simulated-stream",
        sequence=1,
        data_fresh=True,
        age_seconds=0.2,
        captured_at=datetime.now(timezone.utc).isoformat(),
        quality={},
        health={
            "processes": {
                "items": [
                    dict(
                        key="motion",
                        label="模拟运控",
                        state="running",
                        label_state="存活",
                        detail="software fixture",
                        kernel_state="S",
                    )
                ]
            }
        },
    )


def process_head(store):
    with store.engine.connect() as connection:
        return (
            connection.execute(
                select(alert_heads).where(alert_heads.c.rule_key == "process.motion.unavailable")
            )
            .mappings()
            .one()
        )


def test_alert_debounce_unknown_is_not_recovery_events_survive_restart(store, monkeypatch):
    frame = snapshot()
    monkeypatch.setattr(store, "snapshots", lambda: {"items": [deepcopy(frame)]})
    monitor = AlertMonitor(store)
    t = datetime.now(timezone.utc)
    monitor.tick(t)
    frame["health"]["processes"]["items"][0].update(state="stopped", label_state="未发现")
    monitor.tick(t + timedelta(seconds=1))
    assert process_head(store)["state"] == "pending"
    monitor = AlertMonitor(store)
    monitor.tick(t + timedelta(seconds=3))
    assert process_head(store)["state"] == "active"
    monitor.tick(t + timedelta(seconds=3))
    frame["health"]["processes"]["items"][0].update(state="unknown", label_state="未知")
    monitor.tick(t + timedelta(seconds=4))
    assert process_head(store)["state"] == "unknown" and process_head(store)["is_open"]
    frame["health"]["processes"]["items"][0].update(state="running", label_state="存活")
    monitor.tick(t + timedelta(seconds=5))
    assert process_head(store)["state"] == "normal" and not process_head(store)["is_open"]
    events = [
        item
        for item in monitor.events("RP1-TEST")["items"]
        if item["rule_key"] == "process.motion.unavailable"
    ]
    assert [item["event_kind"] for item in reversed(events)] == [
        "opened",
        "observation_lost",
        "recovered",
    ]
    assert events[0]["evidence"]["episode_opened_at"]
    assert all(item["evidence"]["source_kind"] == "simulated" for item in events)


def test_rule_removed_does_not_recover_and_observer_downtime_resets_pending(store, monkeypatch):
    frame = snapshot()
    frame["health"]["processes"]["items"][0].update(state="stopped", label_state="未发现")
    monkeypatch.setattr(store, "snapshots", lambda: {"items": [deepcopy(frame)]})
    monitor = AlertMonitor(store)
    t = datetime.now(timezone.utc)
    monitor.tick(t)
    monitor.tick(t + timedelta(seconds=30))
    assert process_head(store)["state"] == "pending"
    monitor.tick(t + timedelta(seconds=32))
    frame["health"]["processes"]["items"] = []
    monitor.tick(t + timedelta(seconds=33))
    assert process_head(store)["is_open"] and process_head(store)["state"] == "unknown"
    assert "已移除" in process_head(store)["evidence"]["detail"]


def test_temperature_limits_default_off_and_expired_offset_never_recovers():
    frame = snapshot()
    assert not any(
        item["rule_key"].startswith("limit.") for item in conditions(frame, None, CentralPolicy())
    )
    frame["quality"] = {
        "health_values": {"battery.temperature": 90},
        "health_ages_ms": {"battery.temperature": 4500},
    }
    policy = CentralPolicy(temperature_limits_c={"battery.temperature": 80})
    frame["clock_offset_seconds"] = 1
    assert (
        next(i for i in conditions(frame, None, policy) if i["rule_key"].startswith("limit."))[
            "bad"
        ]
        is None
    )
    frame["clock_offset_seconds"] = 0
    assert (
        next(i for i in conditions(frame, None, policy) if i["rule_key"].startswith("limit."))[
            "bad"
        ]
        is True
    )


def message(now=None, **changes):
    fields = dict(
        robot_id="RP1-TEST",
        source_kind="simulated",
        session_id=uuid4(),
        sequence=1,
        observed_at=now or datetime.now(timezone.utc),
        collector_connected=True,
        pending_windows=400,
        pending_bytes=100,
        cache_budget_bytes=1024,
        free_disk_bytes=1,
        reserve_bytes=100,
    )
    return RelayStatus(**{**fields, **changes})


def test_relay_status_scope_old_replay_and_evidence_provenance(store):
    actor = store.device_for_digest(token_digest(TOKEN))
    now = datetime.now(timezone.utc)
    status = message(now)
    assert accept_relay_status(store, status, actor, now)["accepted"]
    assert not accept_relay_status(store, status, actor, now + timedelta(seconds=1))["accepted"]
    with pytest.raises(PermissionError):
        accept_relay_status(store, message(now, robot_id="OTHER"), actor, now)
    with pytest.raises(ValueError, match="过期"):
        accept_relay_status(store, status, actor, now + timedelta(seconds=30))
    monitor = AlertMonitor(store)
    view = monitor.statuses(now)
    rules = conditions(snapshot(), view["RP1-TEST"], store.policy, now=now)
    backlog = next(item for item in rules if item["rule_key"] == "relay.backlog")
    assert backlog["bad"] and backlog["facts"]["status_session_id"] == str(status.session_id)
    assert backlog["facts"]["status_received_at"] and backlog["facts"]["status_sequence"] == 1
    assert all(
        i["bad"] is None
        for i in conditions(
            snapshot(), monitor.statuses(now + timedelta(seconds=30))["RP1-TEST"], store.policy
        )
        if i["rule_key"].startswith("relay.")
    )


def test_new_apis_read_auth_and_no_cleanup_write_surface(
    client, read_headers, write_headers, window
):
    for path in (
        "/api/v1/monitor-storage/usage",
        "/api/v1/monitor-storage/retention-preview",
        "/api/v1/robot-monitors/RP1-TEST/alerts",
        "/api/v1/robot-monitors/RP1-TEST/alert-events",
    ):
        assert client.get(path).status_code == 401
        assert client.get(path, headers=write_headers).status_code == 401
        assert client.get(path, headers=read_headers).status_code == 200
    assert (
        client.post(
            "/api/v1/robot-telemetry/relay-status",
            headers=read_headers,
            json=message().model_dump(mode="json"),
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/api/v1/robot-telemetry/relay-status",
            headers=write_headers,
            json=message().model_dump(mode="json"),
        ).status_code
        == 200
    )
    assert (
        client.post("/api/v1/monitor-storage/retention-preview", headers=read_headers).status_code
        == 405
    )
    assert (
        client.get("/api/v1/robot-monitors/OTHER/alerts", headers=read_headers).status_code == 404
    )


def test_postgresql_types_preserve_original_hashes_and_design_matches_metadata():
    import importlib.util
    from pathlib import Path

    from rp1_monitor.report_store import records

    spec = importlib.util.spec_from_file_location(
        "pg_design", Path("scripts/generate_postgres_design.py")
    )
    generator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(generator)

    for table in (history, records, policies):
        sql = str(CreateTable(table).compile(dialect=postgresql.dialect()))
        assert "payload JSON NOT NULL" in sql
    assert "metrics JSONB NOT NULL" in str(
        CreateTable(key_windows).compile(dialect=postgresql.dialect())
    )
    assert Path("deploy/postgres/001_schema.review.sql").read_text() == generator.schema_sql()


def test_policy_rejects_invented_limits_and_unsafe_selectors():
    for fields in (
        {"key_patterns": ["*"]},
        {"temperature_limits_c": {"host.cpu.usage": 80}},
        {"temperature_limits_c": {"battery.temperature": float("inf")}},
    ):
        with pytest.raises(ValidationError):
            CentralPolicy(**fields)


def test_projection_failure_rolls_back_raw_and_usage_together(store, window, monkeypatch):
    from rp1_monitor import central_data

    def fail(*args, **kwargs):
        raise OSError("simulated projection failure")

    monkeypatch.setattr(central_data, "project_window", fail)
    with pytest.raises(OSError):
        ingest(store, battery_window(window))
    with store.engine.connect() as connection:
        for table in (history, key_windows, daily_usage):
            assert connection.execute(select(func.count()).select_from(table)).scalar_one() == 0


def test_archive_checks_projected_values_even_when_container_hash_is_valid(store, window, tmp_path):
    value = battery_window(window)
    ingest(store, value)
    manifest = archive_range(
        store,
        "RP1-TEST",
        value.window_start,
        value.window_end + timedelta(seconds=1),
        tmp_path / "archive",
    )["items"][0]
    path = tmp_path / "archive" / manifest["file_name"]
    with gzip.open(path, "rt") as handle:
        header, record = [json.loads(line) for line in handle]
    record["key_metrics"]["battery.temperature"]["last"] = 999
    with gzip.open(path, "wt") as handle:
        handle.write(json.dumps(header) + "\n" + json.dumps(record) + "\n")
    import hashlib

    manifest.update(
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(), compressed_bytes=path.stat().st_size
    )
    with pytest.raises(ValueError, match="关键指标"):
        verify_archive(path, manifest)


def test_explicit_backfill_adds_old_data_once_without_rewriting_evidence(store, window):
    value = battery_window(window, offset=-86400)
    ingest(store, value, "backfill")
    from sqlalchemy import delete

    # Deliberately emulate an old schema's raw-only history in this disposable fixture.
    with store.engine.begin() as connection:
        original = connection.execute(select(history.c.payload_hash)).scalar_one()
        connection.execute(delete(key_windows))
        connection.execute(delete(daily_usage))
    result = backfill_key_windows(store, "RP1-TEST")
    assert result["projected_windows"] == 1 and not result["source_rewritten"]
    assert backfill_key_windows(store, "RP1-TEST")["projected_windows"] == 0
    with store.engine.connect() as connection:
        assert connection.execute(select(history.c.payload_hash)).scalar_one() == original
        assert connection.execute(select(daily_usage.c.windows)).scalar_one() == 1


def test_missing_robot_disk_observation_is_unknown_not_healthy(store):
    now = datetime.now(timezone.utc)
    data = message(
        now,
        robot_free_disk_bytes=0,
        robot_reserve_bytes=100,
        robot_disk_observed_at=now - timedelta(seconds=60),
    ).model_dump(mode="json")
    status = {"fresh": True, "payload": data, "received_at": now}
    item = next(
        row
        for row in conditions(snapshot(), status, store.policy, now)
        if row["rule_key"] == "robot.disk_low"
    )
    assert item["bad"] is None and item["facts"]["robot_disk_observed_at"]


def test_old_projection_replay_rejects_mismatched_robot_envelope(store, window):
    import hashlib

    from sqlalchemy import delete

    ingest(store, battery_window(window))
    with store.engine.begin() as connection:
        value = connection.execute(select(history.c.payload)).scalar_one()
        value["robot_id"] = "OTHER-SIMULATED-ID"
        digest = hashlib.sha256(
            json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        connection.execute(update(history).values(payload=value, payload_hash=digest))
        connection.execute(delete(key_windows))
        connection.execute(delete(daily_usage))
    with pytest.raises(ValueError, match="原始信封"):
        backfill_key_windows(store, "RP1-TEST")
    with store.engine.connect() as connection:
        assert connection.execute(select(func.count()).select_from(key_windows)).scalar_one() == 0
        assert connection.execute(select(func.count()).select_from(daily_usage)).scalar_one() == 0
