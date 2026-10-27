"""A different robot clock must not alter raw evidence or revive old observations."""

import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from rp1_monitor.models import LiveObservation, TelemetryBatch
from rp1_monitor.storage import history, token_digest
from rp1_monitor.transport_clock import observation_elapsed


def observation(window, now, local_age=0.1, round_trip_ms=10):
    return LiveObservation(
        stream_id=window.stream_id,
        sequence=window.sequence,
        collector_sampled_at=window.window_end + timedelta(seconds=local_age),
        relay_observed_at=now,
        round_trip_ms=round_trip_ms,
    )


def test_robot_clock_offset_live_receipt_preserves_raw_window_and_hash(
    client, window, write_headers, read_headers
):
    value = window(offset=18)
    value.quality.health_values = {
        "battery.soc": 48,
        "battery.voltage": 50.3,
        "battery.current": -0.2,
    }
    value.quality.health_ages_ms = {key: 100 for key in value.quality.health_values}
    raw = value.model_dump(mode="json")
    batch = TelemetryBatch(
        delivery="live",
        windows=[value],
        live_observation=observation(value, datetime.now(timezone.utc)),
    )
    reply = client.post(
        "/api/v1/robot-telemetry/batches", json=batch.model_dump(mode="json"), headers=write_headers
    )
    assert reply.status_code == 200 and reply.json()["live_ignored"] == 0
    snapshot = client.get("/api/v1/robot-monitors", headers=read_headers).json()["items"][0]
    assert snapshot["clock_unverified"] is True
    assert snapshot["transport"]["freshness_verified"] is True
    assert snapshot["health"]["battery"]["soc"] == 48
    assert snapshot["health"]["battery"]["current"] == -0.2
    with client.app.state.store.engine.connect() as connection:
        row = connection.execute(select(history.c.payload, history.c.payload_hash)).one()
    assert row.payload == raw
    assert (
        row.payload_hash
        == hashlib.sha256(
            json.dumps(raw, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )
    assert "_live_transport" not in row.payload


def test_offset_without_receipt_stays_history_only(client, window, write_headers, read_headers):
    value = window(offset=18)
    reply = client.post(
        "/api/v1/robot-telemetry/batches",
        json={"delivery": "live", "windows": [value.model_dump(mode="json")]},
        headers=write_headers,
    )
    assert reply.status_code == 200 and reply.json()["live_ignored"] == 1
    assert (
        client.get("/api/v1/robot-monitors", headers=read_headers).json()["items"][0]["online"]
        is False
    )


def test_expired_transport_never_falls_back_to_utc(client, window, write_headers, monkeypatch):
    import rp1_monitor.storage as storage

    now = datetime.now(timezone.utc)
    value = window(offset=4)
    value.quality.health_values = {"battery.soc": 48}
    value.quality.health_ages_ms = {"battery.soc": 200}
    # 4s conservative latency, followed by receiver aging. UTC alone looks young.
    proof = observation(value, now, local_age=2, round_trip_ms=2000)
    monkeypatch.setattr(
        storage,
        "datetime",
        type("Clock", (datetime,), {"now": classmethod(lambda cls, tz=None: now)}),
    )
    client.app.state.store.ingest(
        TelemetryBatch(delivery="live", windows=[value], live_observation=proof),
        client.app.state.store.device_for_digest(token_digest(write_headers["Authorization"][7:])),
    )
    for elapsed in (0.9, 1.1, 6):
        instant = now + timedelta(seconds=elapsed)
        monkeypatch.setattr(
            storage,
            "datetime",
            type(
                "Clock",
                (datetime,),
                {"now": classmethod(lambda cls, tz=None, instant=instant: instant)},
            ),
        )
        snapshot = client.app.state.store.snapshots()["items"][0]
        assert snapshot["health"]["battery"]["soc"] is None
        assert snapshot["transport"]["elapsed_seconds"] >= 4 + elapsed


def test_receipt_identity_and_delivery_are_bound(window):
    value = window(offset=18)
    proof = observation(value, datetime.now(timezone.utc)).model_copy(
        update={"sequence": value.sequence + 1}
    )
    with pytest.raises(ValidationError):
        TelemetryBatch(delivery="live", windows=[value], live_observation=proof)
    with pytest.raises(ValidationError):
        TelemetryBatch(
            delivery="backfill",
            windows=[value],
            live_observation=observation(value, datetime.now(timezone.utc)),
        )


def test_stale_clock_jump_or_old_receipt_cannot_prove_freshness(window):
    now = datetime.now(timezone.utc)
    value = window(offset=18)
    proof = observation(value, now)
    assert observation_elapsed(proof, value, now) is not None
    assert observation_elapsed(proof, value, now + timedelta(seconds=6)) is None
    assert observation_elapsed(proof, value, now - timedelta(seconds=2)) is None
    value.quality.clock_status = "WALL_CLOCK_CHANGED"
    assert observation_elapsed(proof, value, now) is None
