"""Per-plugin history uses synthetic evidence; no robot or plugin is contacted."""

import hashlib
import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import update

from rp1_monitor.board_diagnostics import BoardDiagnostics, BoardJournal, summarize_board_reports
from rp1_monitor.diagnostics import canonical_bytes
from rp1_monitor.relay import RelayRuntime, RelaySettings
from rp1_monitor.report_store import records, save_report
from rp1_monitor.robopi_tui import PASSIVE_PLUGINS, BoardReport


def synthetic_report(*results, seconds=0, robot_id="RP1-TEST"):
    stamp = (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()
    rows = []
    counts = dict.fromkeys(("pass", "fail", "needs_review", "skip", "error"), 0)
    for identity, result in results:
        counts[result] += 1
        rows.append(
            {
                "id": identity,
                "result": result,
                "status": result,
                "plugin": {
                    "id": identity,
                    "version": "1.0",
                    "kind": "test",
                    "safety": "passive",
                    "clients": ["csharp"],
                    "lifecycle": "testing",
                    "depends_on": ["RP020"] if identity == "RP021" else [],
                },
                "message": f"模拟 {identity}：{result}",
                "details": {"simulated": True, "raw_value": "未转换的原始值"},
            }
        )
    raw = {
        "schema_version": 3,
        "run_id": str(uuid4()),
        "tui_version": "1.0.0",
        "client": "csharp",
        "selected": results[-1][0],
        "started_at": stamp,
        "finished_at": stamp,
        "outcome": "fail"
        if counts["fail"]
        else "incomplete"
        if counts["needs_review"] or counts["skip"] or counts["error"]
        else "pass",
        "summary": counts,
        "results": rows,
    }
    return BoardReport(
        report_id=raw["run_id"],
        robot_id=robot_id,
        source_kind="simulated",
        created_at=stamp,
        validation_scope="simulated",
        board_diagnostics=raw,
    ).model_dump(mode="json")


def by_id(checks):
    return {check["id"]: check["latest"] for check in checks}


def test_each_check_keeps_its_own_latest_including_dependency_results():
    can = synthetic_report(("RP007", "fail"))
    bms_old = synthetic_report(("RP020", "pass"))
    bms_new = synthetic_report(("RP020", "fail"), ("RP021", "skip"))
    checks = summarize_board_reports(iter([bms_new, bms_old, can]))
    assert [item["id"] for item in checks] == list(PASSIVE_PLUGINS)
    latest = by_id(checks)
    assert latest["RP007"]["result"] == "fail"
    assert latest["RP007"]["report_id"] == can["report_id"]
    assert latest["RP020"]["result"] == "fail"
    assert latest["RP020"]["report_id"] == bms_new["report_id"]
    assert latest["RP021"]["result"] == "skip"
    assert latest["RP001"] is None
    assert latest["RP020"]["details"] == bms_new["board_diagnostics"]["results"][0]["details"]
    assert latest["RP020"]["source_kind"] == latest["RP020"]["validation_scope"] == "simulated"


def test_unknown_old_and_adapter_error_cannot_become_pass_or_erase_evidence():
    previous = synthetic_report(("RP007", "fail"))
    unknown = deepcopy(previous)
    unknown["board_diagnostics"]["results"][0].update(result="unknown", status="unknown")
    old = deepcopy(previous)
    old["board_diagnostics"]["schema_version"] = 2
    unknown_id = deepcopy(previous)
    unknown_id["board_diagnostics"]["selected"] = "RP999"
    adapter_error = {"adapter_error": "executor unavailable"}
    values = [unknown, old, unknown_id, adapter_error]
    assert all(item["latest"] is None for item in summarize_board_reports(iter(values)))
    latest = by_id(summarize_board_reports(iter([*values, previous])))
    assert latest["RP007"]["report_id"] == previous["report_id"]
    assert latest["RP007"]["result"] == "fail"


def test_history_iterator_stops_once_every_check_has_a_result():
    def reports():
        for identity in PASSIVE_PLUGINS:
            results = [(identity, "pass")]
            if identity == "RP021":
                results.insert(0, ("RP020", "pass"))
            yield synthetic_report(*results)
        raise AssertionError("Older payloads should not be read after all results are found")

    assert all(item["latest"] is not None for item in summarize_board_reports(reports()))


def test_history_survives_restart_and_relay_disconnect(tmp_path, monkeypatch):
    settings = RelaySettings(
        "RP1-TEST",
        "simulated",
        "http://127.0.0.1:8891",
        "http://127.0.0.1:8890",
        "synthetic-device-token",
        tmp_path / "relay",
    )
    relay = RelayRuntime(settings)
    can = synthetic_report(("RP007", "fail"))
    relay.board_journal.append(can)
    relay.board_journal.append(synthetic_report(("RP034", "pass")))
    relay = RelayRuntime(settings)

    def offline(*args, **kwargs):
        raise OSError("synthetic disconnected collector")

    monkeypatch.setattr("rp1_monitor.relay.read_json", offline)
    status = relay.board_status()
    assert status["enabled"] is False
    assert by_id(status["checks"])["RP007"]["report_id"] == can["report_id"]
    assert by_id(status["checks"])["RP034"]["result"] == "pass"
    assert status["latest"]["board_diagnostics"]["selected"] == "RP034"


def test_online_relay_preserves_collector_checks(tmp_path, monkeypatch):
    relay = RelayRuntime(
        RelaySettings(
            "RP1-TEST",
            "simulated",
            "http://127.0.0.1:8891",
            "http://127.0.0.1:8890",
            "synthetic-device-token",
            tmp_path / "relay",
        )
    )
    checks = summarize_board_reports(iter([synthetic_report(("RP007", "needs_review"))]))
    response = {"robot_id": "RP1-TEST", "source_kind": "simulated", "checks": checks}
    monkeypatch.setattr("rp1_monitor.relay.read_json", lambda *args, **kwargs: response)
    assert relay.board_status()["checks"] == checks


def test_corrupt_journal_history_is_not_silently_skipped(tmp_path):
    journal = BoardJournal(tmp_path / "history.sqlite")
    data = synthetic_report(("RP007", "fail"))
    journal.append(data)
    journal.append(synthetic_report(("RP034", "pass")))
    data["board_diagnostics"]["results"][0]["message"] = "tampered"
    with journal.connection() as db:
        db.execute("UPDATE runs SET payload=? WHERE sequence=1", (json.dumps(data),))
    with pytest.raises(ValueError, match="校验失败"):
        journal.checks()


def test_status_reports_cooldown_without_starting_plugin(tmp_path, monkeypatch):
    board = BoardDiagnostics(
        SimpleNamespace(
            state_dir=tmp_path,
            robot_id="RP1-TEST",
            source_kind="simulated",
            robopi_tui_executor=None,
        )
    )
    monkeypatch.setattr("rp1_monitor.board_diagnostics.time.monotonic", lambda: 100)
    assert board.status()["cooldown_remaining_seconds"] == 0
    board.last_started = 95.2
    assert board.status()["cooldown_remaining_seconds"] == 6
    board.last_started = 80
    assert board.status()["cooldown_remaining_seconds"] == 0


def test_central_history_is_authenticated_robot_scoped_and_not_limited_to_30(
    client, write_headers, read_headers
):
    can = synthetic_report(("RP007", "fail"), seconds=-60)
    upload = "/api/v1/robot-telemetry/board-diagnostics"
    assert client.post(upload, json=can, headers=write_headers).status_code == 200
    for seconds in range(-40, 0):
        data = synthetic_report(("RP034", "pass"), seconds=seconds)
        assert client.post(upload, json=data, headers=write_headers).status_code == 200
    store = client.app.state.store
    store.configure(
        [
            {
                "robot_id": "OTHER",
                "name": "另一模拟机器人",
                "source_kind": "simulated",
                "token": "synthetic-other-device-token",
            }
        ],
        authoritative=False,
    )
    save_report(store, synthetic_report(("RP007", "pass"), robot_id="OTHER"))
    path = "/api/v1/robot-monitors/RP1-TEST/board-diagnostics"
    assert client.get(path).status_code == 401
    assert client.get(path, headers=write_headers).status_code == 401
    response = client.get(path, headers=read_headers)
    assert response.status_code == 200
    body = response.json()
    assert by_id(body["checks"])["RP007"]["report_id"] == can["report_id"]
    assert by_id(body["checks"])["RP007"]["result"] == "fail"
    assert body["latest"]["board_diagnostics"]["selected"] == "RP034"
    assert client.post(path + "/runs", headers=read_headers, json={}).status_code in {404, 405}
    assert (
        client.get(
            "/api/v1/robot-monitors/MISSING/board-diagnostics", headers=read_headers
        ).status_code
        == 404
    )


def test_central_rejects_corrupt_older_report_instead_of_hiding_it(client, read_headers):
    store = client.app.state.store
    data = synthetic_report(("RP007", "fail"), seconds=-20)
    save_report(store, data)
    save_report(store, synthetic_report(("RP034", "pass")))
    changed = deepcopy(data)
    changed["board_diagnostics"]["results"][0]["message"] = "tampered"
    with store.engine.begin() as connection:
        connection.execute(
            update(records).where(records.c.report_id == data["report_id"]).values(payload=changed)
        )
    response = client.get("/api/v1/robot-monitors/RP1-TEST/board-diagnostics", headers=read_headers)
    assert response.status_code == 409


def test_central_rejects_mismatched_robot_inside_hash_valid_payload(client, read_headers):
    store = client.app.state.store
    data = synthetic_report(("RP007", "pass"), robot_id="OTHER")
    with store.engine.begin() as connection:
        connection.execute(
            records.insert().values(
                report_id=data["report_id"],
                robot_id="RP1-TEST",
                created_at=datetime.fromisoformat(data["created_at"]),
                payload=data,
                sha256=hashlib.sha256(canonical_bytes(data)).hexdigest(),
            )
        )
    assert (
        client.get(
            "/api/v1/robot-monitors/RP1-TEST/board-diagnostics", headers=read_headers
        ).status_code
        == 409
    )
