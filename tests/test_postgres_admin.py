"""Deferred real PostgreSQL role checks. Never start a container or connect implicitly."""

import hashlib
import json
import os
import re
import secrets
from pathlib import Path

import pytest
from conftest import require_disposable_postgres
from sqlalchemy import create_engine

from rp1_monitor.models import MetricRollup


@pytest.mark.skipif(
    not os.environ.get("RP1_MONITOR_TEST_DATABASE_URL"),
    reason="管理员SQL审核后，在显式指定的独立PostgreSQL测试库验证",
)
def test_admin_controlled_writes_audit_correction_and_denied_bypasses(window):
    import psycopg
    from psycopg import sql

    url = require_disposable_postgres(os.environ["RP1_MONITOR_TEST_DATABASE_URL"])
    namespace = "rp1_admin_test_" + secrets.token_hex(5)
    administrator = namespace + "_person"
    engine = create_engine(url)
    pooled = engine.raw_connection()
    connection = pooled.driver_connection

    def execute(statement, parameters=None):
        return connection.execute(statement, parameters, prepare=False)

    def identity(role):
        execute("RESET SESSION AUTHORIZATION")
        execute(sql.SQL("SET SESSION AUTHORIZATION {}").format(sql.Identifier(role)))

    def rejected(statement, parameters=None, expected=psycopg.errors.InsufficientPrivilege):
        # A failed write must not abort the surrounding disposable test transaction.
        with pytest.raises(expected), connection.transaction():
            execute(statement, parameters)

    try:
        original_login = execute("SELECT session_user").fetchone()[0]
        assert execute("SELECT rolsuper FROM pg_roles WHERE rolname = session_user").fetchone()[0]
        for filename in (
            "001_schema.review.sql",
            "002_permissions.review.sql",
            "004_admin.review.sql",
        ):
            source = (Path("deploy/postgres") / filename).read_text()
            source = re.sub(r"(?m)^(?:BEGIN;|COMMIT;)\s*$", "", source)
            # Random, test-owned schema and group names; all DDL rolls back at the end.
            execute(source.replace("rp1_monitor", namespace))
        execute(
            sql.SQL("CREATE ROLE {} NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE").format(
                sql.Identifier(administrator)
            )
        )
        execute(
            sql.SQL("GRANT {} TO {}").format(
                sql.Identifier(namespace + "_admin"), sql.Identifier(administrator)
            )
        )
        prefix = sql.Identifier(namespace)
        create = sql.SQL("SELECT {}.admin_create_robot(%s,%s,%s,%s,%s)").format(prefix)
        update = sql.SQL("SELECT {}.admin_update_robot(%s,%s,%s,%s,%s,%s)").format(prefix)
        correct = sql.SQL("SELECT {}.admin_correct_metric(%s,%s,%s,%s,%s::jsonb,%s)").format(prefix)
        identity(administrator)
        assert (
            execute(create, ("ADMIN-SIM", "Before", "simulated", "a" * 64, "登记测试")).fetchone()[
                0
            ]
            == "ADMIN-SIM"
        )
        execute(update, ("ADMIN-SIM", "Before", True, "After", False, "更正名称并停用登记"))
        rejected(
            update,
            ("ADMIN-SIM", "Before", True, "Lost update", True, "旧版本"),
            psycopg.errors.SerializationFailure,
        )
        rejected(
            create,
            ("MISSING-REASON", "Device", "simulated", "b" * 64, ""),
            psycopg.errors.InvalidParameterValue,
        )
        rejected(sql.SQL("UPDATE {}.monitor_robots SET source_kind='ros2'").format(prefix))
        rejected(sql.SQL("SELECT token_digest FROM {}.monitor_robots").format(prefix))
        rejected(sql.SQL("INSERT INTO {}.monitor_admin_audit DEFAULT VALUES").format(prefix))
        rejected(sql.SQL("SET ROLE {}").format(sql.Identifier(namespace + "_admin_executor")))

        identity(original_login)
        metric = {"last": 50.0, "minimum": 49.0, "maximum": 51.0, "count": 2, "age_ms": 100.0}
        frame = window().model_copy(update={"robot_id": "ADMIN-SIM"})
        frame.metrics["battery.soc"] = MetricRollup(**metric)
        payload = frame.model_dump(mode="json")
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        window_id = execute(
            sql.SQL(
                "INSERT INTO {}.monitor_windows(robot_id,stream_id,sequence,window_start,window_end,"
                "received_at,payload_hash,payload) VALUES (%s,%s,%s,%s,%s,now(),%s,%s::json) RETURNING id"
            ).format(prefix),
            (
                frame.robot_id,
                str(frame.stream_id),
                frame.sequence,
                frame.window_start,
                frame.window_end,
                digest,
                json.dumps(payload),
            ),
        ).fetchone()[0]
        original = execute(
            sql.SQL("SELECT payload::text, payload_hash FROM {}.monitor_windows").format(prefix)
        ).fetchone()
        identity(administrator)
        corrected = {**metric, "last": 48.0, "minimum": 47.0, "maximum": 49.0}
        revision = execute(
            correct,
            (window_id, "battery.soc", None, "replace", json.dumps(corrected), "仪表核对更正"),
        ).fetchone()[0]
        rejected(
            correct,
            (window_id, "battery.soc", None, "replace", json.dumps(corrected), "过时版本"),
            psycopg.errors.SerializationFailure,
        )
        rejected(
            correct,
            (
                window_id,
                "battery.soc",
                revision,
                "replace",
                json.dumps({**corrected, "count": 3}),
                "修改样本数",
            ),
            psycopg.errors.InvalidParameterValue,
        )
        rejected(
            correct,
            (
                window_id,
                "battery.soc",
                revision,
                "replace",
                json.dumps({**corrected, "minimum": 60.0}),
                "无效极值",
            ),
            psycopg.errors.InvalidParameterValue,
        )
        revision = execute(
            correct, (window_id, "battery.soc", revision, "exclude", None, "排除错误观测")
        ).fetchone()[0]
        assert (
            execute(
                sql.SQL("SELECT effective_metric FROM {}.admin_current_metric_corrections").format(
                    prefix
                )
            ).fetchone()[0]
            is None
        )
        revision = execute(
            correct, (window_id, "battery.soc", revision, "restore", None, "撤销人工更正")
        ).fetchone()[0]
        row = execute(
            sql.SQL(
                "SELECT revision, effective_metric FROM {}.admin_current_metric_corrections"
            ).format(prefix)
        ).fetchone()
        assert row == (revision, metric)
        assert (
            execute(
                sql.SQL("SELECT payload::text, payload_hash FROM {}.monitor_windows").format(prefix)
            ).fetchone()
            == original
        )
        rejected(sql.SQL("UPDATE {}.monitor_windows SET payload='{{}}'::json").format(prefix))
        rejected(sql.SQL("DELETE FROM {}.monitor_metric_corrections").format(prefix))
        rejected(sql.SQL("UPDATE {}.monitor_admin_audit SET reason='rewrite'").format(prefix))
        rejected(sql.SQL("CREATE TABLE {}.unreviewed(id int)").format(prefix))
        audit = execute(
            sql.SQL(
                "SELECT action, actor_db_login, before_value, after_value FROM {}.monitor_admin_audit ORDER BY id"
            ).format(prefix)
        ).fetchall()
        assert [row[0] for row in audit] == [
            "robot.created",
            "robot.updated",
            "metric.replace",
            "metric.exclude",
            "metric.restore",
        ]
        assert all(row[1] == administrator for row in audit)
        assert "token_digest" not in json.dumps(audit)
        assert "a" * 64 not in json.dumps(audit)
        identity(namespace + "_reader")
        rejected(create, ("READER-DENIED", "No", "simulated", "d" * 64, "越权测试"))
        identity(namespace + "_app")
        rejected(correct, (window_id, "battery.soc", revision, "restore", None, "越权测试"))
    finally:
        connection.rollback()
        pooled.close()
        engine.dispose()
