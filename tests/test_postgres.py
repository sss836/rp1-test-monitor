import os
from concurrent.futures import ThreadPoolExecutor

import pytest
from conftest import DEVICE_TOKEN, require_disposable_postgres
from sqlalchemy import func, select

from rp1_monitor.models import TelemetryBatch
from rp1_monitor.storage import Store, history, metadata, token_digest


@pytest.mark.skipif(
    not os.environ.get("RP1_MONITOR_TEST_DATABASE_URL"),
    reason="由scripts/check_postgres.py启动独立PostgreSQL测试",
)
def test_cross_instance_concurrent_idempotency(window):
    url = require_disposable_postgres(os.environ["RP1_MONITOR_TEST_DATABASE_URL"])
    store = Store(url)
    metadata.drop_all(store.engine)
    metadata.create_all(store.engine)
    store.configure(
        [
            {
                "robot_id": "RP1-TEST",
                "name": "Concurrent simulator",
                "source_kind": "simulated",
                "token": DEVICE_TOKEN,
            }
        ]
    )
    frame = window()
    batch = TelemetryBatch(delivery="live", windows=[frame])

    def writer(_):
        independent = Store(url, initialize=False)
        try:
            actor = independent.device_for_digest(token_digest(DEVICE_TOKEN))
            return independent.ingest(batch, actor)
        finally:
            independent.engine.dispose()

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(writer, range(16)))
    assert sum(result["inserted"] for result in results) == 1
    assert sum(result["duplicates"] for result in results) == 15
    with store.engine.connect() as connection:
        assert connection.execute(select(func.count()).select_from(history)).scalar_one() == 1
    store.engine.dispose()


def test_destructive_postgres_entry_rejects_non_test_database(monkeypatch, window):
    monkeypatch.setenv(
        "RP1_MONITOR_TEST_DATABASE_URL", "postgresql+psycopg://example.invalid/production"
    )
    with pytest.raises(pytest.fail.Exception, match="专用随机测试库"):
        test_cross_instance_concurrent_idempotency(window)


@pytest.mark.skipif(
    not os.environ.get("RP1_MONITOR_TEST_DATABASE_URL"),
    reason="等待审核后在独立PostgreSQL库验证JSON证据往返",
)
def test_postgres_signed_zero_evidence_and_policy_roundtrip(window):
    import hashlib
    import json
    import math

    from rp1_monitor.central_policy import CentralPolicy
    from rp1_monitor.central_tables import policies
    from rp1_monitor.models import MetricRollup

    url = require_disposable_postgres(os.environ["RP1_MONITOR_TEST_DATABASE_URL"])
    policy = CentralPolicy(temperature_limits_c={"battery.temperature": -0.0})
    store = Store(url, policy=policy)
    key = "pg-json-software-test-" + "q" * 32
    store.configure(
        [
            dict(
                robot_id="PG-SIGNED-ZERO",
                name="Signed zero test",
                source_kind="simulated",
                token=key,
            )
        ]
    )
    value = window().model_copy(update={"robot_id": "PG-SIGNED-ZERO"})
    value.metrics["battery.temperature"] = MetricRollup(
        last=-0.0, minimum=-0.0, maximum=-0.0, count=1, age_ms=0
    )
    try:
        store.ingest(
            TelemetryBatch(delivery="live", windows=[value]),
            store.device_for_digest(token_digest(key)),
        )
        with store.engine.connect() as connection:
            row = (
                connection.execute(select(history).where(history.c.robot_id == "PG-SIGNED-ZERO"))
                .mappings()
                .one()
            )
            original_policy = connection.execute(
                select(policies.c.payload).where(policies.c.policy_sha256 == policy.digest)
            ).scalar_one()
        assert math.copysign(1, row["payload"]["metrics"]["battery.temperature"]["last"]) < 0
        assert (
            hashlib.sha256(
                json.dumps(row["payload"], sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            == row["payload_hash"]
        )
        assert (
            hashlib.sha256(
                json.dumps(original_policy, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            == policy.digest
        )
    finally:
        store.engine.dispose()
