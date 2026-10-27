"""Synthetic TUI protocol, subprocess and two-hop delivery; no robot access."""

import hashlib
import json
import os
import sqlite3
import time
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from rp1_monitor.board_diagnostics import BoardDiagnostics, BoardJournal
from rp1_monitor.diagnostics import canonical_bytes, export_bundle
from rp1_monitor.relay import RelayRuntime, RelaySettings
from rp1_monitor.robopi_tui import BoardReport


def native_report(identity="RP007", result="needs_review"):
    stamp = datetime.now(timezone.utc).isoformat()
    plugin = {
        "id": identity,
        "version": "1.0",
        "kind": "test",
        "safety": "passive",
        "clients": ["tui", "csharp"],
        "lifecycle": "testing",
        "depends_on": [],
        "documentation": {"title": "模拟接口检查", "criterion": "合成数据仅用于软件验证"},
    }
    counts = dict.fromkeys(("pass", "fail", "needs_review", "skip", "error"), 0)
    counts[result] = 1
    return {
        "schema_version": 3,
        "run_id": str(uuid4()),
        "tui_version": "1.0.0",
        "client": "csharp",
        "selected": identity,
        "started_at": stamp,
        "finished_at": stamp,
        "outcome": "pass" if result == "pass" else "fail" if result == "fail" else "incomplete",
        "summary": counts,
        "results": [
            {
                "id": identity,
                "result": result,
                "status": result,
                "plugin": plugin,
                "message": "模拟 CAN 接口清单；没有发送 CAN 帧",
                "details": {"interfaces": []},
            }
        ],
    }


def report(raw=None, robot_id="RP1-TEST", source_kind="simulated"):
    raw = raw or native_report()
    return BoardReport(
        report_id=raw["run_id"],
        robot_id=robot_id,
        source_kind=source_kind,
        created_at=raw["finished_at"],
        validation_scope="simulated"
        if source_kind == "simulated"
        else "software_observation_not_hardware_acceptance",
        board_diagnostics=raw,
    ).model_dump(mode="json")


def executor(tmp_path, raw=None, metadata=None, tail=""):
    raw = raw or native_report()
    metadata = metadata or raw["results"][0]["plugin"]
    path = tmp_path / "synthetic_executor.py"
    log = tmp_path / "calls.jsonl"
    path.write_text(
        "import json, os, sys, time\n"
        f"with open({str(log)!r}, 'a') as out: out.write(json.dumps({{'argv':sys.argv[1:],'cpus':sorted(os.sched_getaffinity(0)),'nice':os.getpriority(os.PRIO_PROCESS,0)}})+'\\n')\n"
        f"catalog={ {'schema_version': 3, 'tests': metadata if isinstance(metadata, list) else [metadata]}!r}\n"
        f"report={raw!r}\n"
        "if '--list' in sys.argv: print(json.dumps(catalog))\n"
        "else:\n" + (" " + tail + "\n" if tail else " print(json.dumps(report))\n")
    )
    return path, log


def controller(tmp_path, path):
    return BoardDiagnostics(
        SimpleNamespace(
            robot_id="RP1-TEST",
            source_kind="simulated",
            state_dir=tmp_path / "state",
            robopi_tui_executor=path,
        )
    )


def finish(board):
    board.thread.join(timeout=5)
    assert not board.thread.is_alive()
    return board.status()


def test_native_process_is_passive_bounded_excludes_cpu0_and_marks_simulated(tmp_path):
    path, log = executor(tmp_path)
    board = controller(tmp_path, path)
    assert board.start("RP007")["state"] == "running"
    status = finish(board)
    assert status["job"]["state"] == "completed"
    saved = status["latest"]
    assert saved["source_kind"] == saved["validation_scope"] == "simulated"
    assert saved["board_diagnostics"]["outcome"] == "incomplete"
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert calls[0]["argv"] == ["--env", "csharp", "--no-save", "--list"]
    assert calls[1]["argv"] == ["--env", "csharp", "--no-save", "--run", "RP007"]
    assert all(0 not in item["cpus"] and item["nice"] >= 10 for item in calls)
    assert Path(board.journal.path).stat().st_mode & 0o077 == 0
    restarted = controller(tmp_path, path)
    assert restarted.status()["latest"] == saved
    with pytest.raises(Exception) as error:
        board.start("RP007")
    assert error.value.status_code == 429


@pytest.mark.parametrize(
    "change",
    [
        {"safety": "query"},
        {"safety": "active"},
        {"version": "2.0"},
        {"depends_on": ["RP025"]},
        {"depends_on": ["RP007"]},
    ],
)
def test_metadata_or_dependency_change_is_blocked_before_run(tmp_path, change):
    raw = native_report()
    metadata = {**raw["results"][0]["plugin"], **change}
    path, log = executor(tmp_path, raw, metadata)
    board = controller(tmp_path, path)
    board.start("RP007")
    state = finish(board)
    assert state["job"]["state"] == "error" and state["latest"] is None
    assert len(log.read_text().splitlines()) == 1


@pytest.mark.parametrize("identity", ["all", "RP009", "RP025", "RP040", "RP007; reboot"])
def test_no_arbitrary_command_or_active_plugin(tmp_path, identity):
    path, log = executor(tmp_path)
    board = controller(tmp_path, path)
    with pytest.raises(Exception) as error:
        board.start(identity)
    assert error.value.status_code == 422
    assert not log.exists()


@pytest.mark.parametrize(
    "tail, message", [("print('x'*300000)", "256 KiB"), ("time.sleep(20)", "超时")]
)
def test_output_limit_timeout_and_no_automatic_retry(tmp_path, tail, message):
    path, log = executor(tmp_path, tail=tail)
    board = controller(tmp_path, path)
    board.runner.timeout = 0.3
    board.start("RP007")
    status = finish(board)
    assert status["job"]["state"] == "error" and message in status["job"]["error"]
    assert len(log.read_text().splitlines()) == 2


def test_shutdown_cancels_own_check(tmp_path):
    path, log = executor(tmp_path, tail="time.sleep(20)")
    board = controller(tmp_path, path)
    board.start("RP007")
    deadline = time.monotonic() + 3
    while (
        not log.exists() or len(log.read_text().splitlines()) < 2
    ) and time.monotonic() < deadline:
        time.sleep(0.01)
    board.stop()
    assert not board.thread.is_alive()
    assert "取消" in board.status()["job"]["error"]


def test_board_disk_failure_is_visible_and_does_not_leave_running_job(tmp_path, monkeypatch):
    path, _ = executor(tmp_path)
    board = controller(tmp_path, path)

    def full(_):
        raise sqlite3.OperationalError("synthetic disk full")

    monkeypatch.setattr(board.journal, "append", full)
    board.start("RP007")
    state = finish(board)
    assert state["job"]["state"] == "error" and "disk full" in state["job"]["error"]
    assert state["latest"] is None


def test_no_cpu_besides_cpu0_means_no_subprocess(tmp_path, monkeypatch):
    path, log = executor(tmp_path)
    board = controller(tmp_path, path)
    monkeypatch.setattr(os, "sched_getaffinity", lambda _: {0})
    board.start("RP007")
    assert "没有可用 CPU" in finish(board)["job"]["error"]
    assert not log.exists()


def test_corrupt_local_report_is_not_displayed_or_forwarded(tmp_path):
    journal = BoardJournal(tmp_path / "source.sqlite")
    data = report()
    journal.append(data)
    data["board_diagnostics"]["results"][0]["message"] = "tampered"
    with journal.connection() as connection:
        connection.execute("UPDATE runs SET payload=?", (json.dumps(data),))
    for read in (journal.latest, journal.page, journal.pending):
        with pytest.raises(ValueError, match="校验失败"):
            read()


def test_local_api_requires_explicit_request_and_rejects_cross_origin(tmp_path):
    from rp1_monitor.agent import AgentSettings, create_agent_app

    path, log = executor(tmp_path)
    app = create_agent_app(
        AgentSettings("RP1-TEST", "simulated", tmp_path / "agent", robopi_tui_executor=path)
    )
    with TestClient(app) as client:
        assert client.get("/api/v1/board-diagnostics").json()["enabled"]
        assert not log.exists()  # GET/status/startup never executes a plugin.
        body = {"robot_id": "RP1-TEST", "plugin_id": "RP007"}
        assert (
            client.post(
                "/api/v1/board-diagnostics/runs",
                json=body,
                headers={"Origin": "https://untrusted.invalid"},
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/api/v1/board-diagnostics/runs", json={**body, "robot_id": "OTHER"}
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/api/v1/board-diagnostics/runs", json={**body, "command": "reboot"}
            ).status_code
            == 422
        )
        assert client.post("/api/v1/board-diagnostics/runs", json=body).status_code == 202
        assert finish(app.state.runtime.board_diagnostics)["latest"]
        assert client.get("/api/v1/state").status_code == 200


def test_missing_executor_keeps_existing_sampling_and_clear_failure(tmp_path):
    board = controller(tmp_path, tmp_path / "missing.py")
    board.start("RP007")
    assert "不会自动安装" in finish(board)["job"]["error"]
    disabled = controller(tmp_path / "disabled", None)
    assert disabled.status()["enabled"] is False
    with pytest.raises(Exception) as error:
        disabled.start("RP007")
    assert error.value.status_code == 503


def test_journal_replays_after_restart_and_commits_before_cursor(tmp_path):
    source = BoardJournal(tmp_path / "source.sqlite")
    records = [report() for _ in range(5)]
    for item in records:
        source.append(item)
    pc = BoardJournal(tmp_path / "pc.sqlite")
    page = source.page()
    bad = deepcopy(page)
    bad["items"][1]["report"]["robot_id"] = "OTHER"
    with pytest.raises(ValueError):
        pc.receive_page(bad, "RP1-TEST", "simulated")
    assert pc.pending() == [] and pc.cursor()["after"] == 0
    pc.receive_page(page, "RP1-TEST", "simulated")
    pc = BoardJournal(tmp_path / "pc.sqlite")
    pc.receive_page(source.page(pc.cursor()["after"]), "RP1-TEST", "simulated")
    pc.receive_page(source.page(pc.cursor()["after"]), "RP1-TEST", "simulated")
    assert pc.cursor()["after"] == 5
    with pytest.raises(ValueError):
        pc.acknowledge(records[0]["report_id"], "wrong")
    for item in records:
        pc.acknowledge(item["report_id"], hashlib.sha256(canonical_bytes(item)).hexdigest())
    assert pc.pending() == []
    changed = deepcopy(records[0])
    changed["board_diagnostics"]["results"][0]["message"] = "altered"
    with pytest.raises(ValueError):
        pc.append(changed)


def test_two_hop_report_upload_is_idempotent_private_and_survives_disconnect(
    tmp_path, client, write_headers, read_headers
):
    source = BoardJournal(tmp_path / "robot.sqlite")
    saved = report()
    source.append(saved)
    relay = RelayRuntime(
        RelaySettings(
            "RP1-TEST",
            "simulated",
            "http://127.0.0.1:8891",
            "http://127.0.0.1:8890",
            write_headers["Authorization"][7:],
            tmp_path / "pc",
        )
    )
    requests = []

    def collector(request):
        assert "authorization" not in request.headers
        requests.append(request)
        return httpx.Response(200, json=source.page(int(request.url.params.get("after", 0))))

    with httpx.Client(transport=httpx.MockTransport(collector)) as connection:
        relay.receive_board_reports(connection)
    relay = RelayRuntime(relay.settings)  # No robot connection is needed for a retry.
    assert relay.board_journal.pending() == [saved]

    def central(request):
        response = client.post(
            request.url.path,
            content=request.content,
            headers={**write_headers, "Content-Type": "application/json"},
        )
        return httpx.Response(response.status_code, content=response.content)

    with httpx.Client(transport=httpx.MockTransport(central)) as connection:
        relay.upload_board_reports(connection)
    assert relay.board_journal.pending() == []
    url = "/api/v1/robot-telemetry/board-diagnostics"
    assert client.post(url, json=saved, headers=read_headers).status_code == 401
    assert client.post(url, json=saved, headers=write_headers).status_code == 200
    assert (
        client.post(url, json={**saved, "robot_id": "OTHER"}, headers=write_headers).status_code
        == 403
    )
    modified = deepcopy(saved)
    modified["board_diagnostics"]["results"][0]["message"] = "conflicting report"
    assert client.post(url, json=modified, headers=write_headers).status_code == 409
    assert (
        client.post(
            url,
            json={**saved, "source_kind": "ros2", "validation_scope": "simulated"},
            headers=write_headers,
        ).status_code
        == 422
    )
    view_url = "/api/v1/robot-monitors/RP1-TEST/board-diagnostics"
    assert client.get(view_url).status_code == 401
    latest = client.get(view_url, headers=read_headers).json()["latest"]
    assert latest == saved
    assert client.post(
        view_url + "/runs", json={"plugin_id": "RP007"}, headers=read_headers
    ).status_code in {404, 405}
    bundle = client.get(
        f"/api/v1/robot-monitors/RP1-TEST/diagnostics/{saved['report_id']}/bundle",
        headers=read_headers,
    )
    assert bundle.status_code == 200
    assert export_bundle({"report": saved})


def test_protocol_rejects_disguised_active_and_false_pass(client, write_headers):
    for alter in (
        lambda d: d["board_diagnostics"]["results"][0]["plugin"].update(safety="query"),
        lambda d: d["board_diagnostics"].update(outcome="pass"),
        lambda d: d["board_diagnostics"].update(schema_version=2),
    ):
        data = report()
        alter(data)
        assert (
            client.post(
                "/api/v1/robot-telemetry/board-diagnostics", json=data, headers=write_headers
            ).status_code
            == 422
        )


def extended_native_report(identity):
    dependencies = {"RP013": ["RP012"], "RP021": ["RP020"], "RP022": ["RP021"]}
    ids = []

    def add(plugin_id):
        for dependency in dependencies.get(plugin_id, []):
            add(dependency)
        ids.append(plugin_id)

    add(identity)
    raw = native_report(identity, "pass")
    raw["results"] = [native_report(plugin_id, "pass")["results"][0] for plugin_id in ids]
    for row in raw["results"]:
        row["plugin"]["depends_on"] = dependencies.get(row["id"], [])
    raw["summary"]["pass"] = len(ids)
    return raw


@pytest.mark.parametrize("identity", ["RP008", "RP012", "RP013", "RP022", "RP029"])
def test_added_checks_call_native_executor_and_preserve_dependency_results(tmp_path, identity):
    raw = extended_native_report(identity)
    path, calls = executor(tmp_path, raw, [row["plugin"] for row in raw["results"]])
    board = controller(tmp_path, path)
    board.start(identity)
    state = finish(board)
    assert state["job"]["state"] == "completed"
    assert state["latest"]["board_diagnostics"] == raw
    invoked = [json.loads(line)["argv"] for line in calls.read_text().splitlines()]
    assert invoked == [
        ["--env", "csharp", "--no-save", "--list"],
        ["--env", "csharp", "--no-save", "--run", identity],
    ]
    checked = {item["id"] for item in state["checks"] if item["latest"]}
    assert checked == {row["id"] for row in raw["results"]}


def test_bms_version_address_limit_is_inherited_and_does_not_change_monitor_limit(tmp_path):
    import resource

    from rp1_monitor.robopi_tui import BMS_VERSION_ADDRESS_SPACE

    raw = extended_native_report("RP022")
    path, _ = executor(tmp_path, raw, [row["plugin"] for row in raw["results"]])
    # Real subprocesses exercise kernel limit inheritance, with no hardware reads.
    content = path.read_text()
    content = content.replace(
        " print(json.dumps(report))",
        " import resource, subprocess, mmap\n"
        " limits=resource.getrlimit(resource.RLIMIT_AS)\n"
        " child=json.loads(subprocess.check_output([sys.executable,'-c',"
        "'import json,resource;print(json.dumps(resource.getrlimit(resource.RLIMIT_AS)))']))\n"
        " blocked=False\n"
        " try:\n"
        f"  allocation=mmap.mmap(-1,{BMS_VERSION_ADDRESS_SPACE * 2})\n"
        "  allocation.close()\n"
        " except (OSError,MemoryError): blocked=True\n"
        " report['results'][-1]['details']={'limits':list(limits),'child_limits':child,'large_allocation_blocked':blocked}\n"
        " print(json.dumps(report))",
    )
    path.write_text(content)
    original = resource.getrlimit(resource.RLIMIT_AS)
    board = controller(tmp_path, path)
    board.start("RP022")
    state = finish(board)
    assert state["job"]["state"] == "completed"
    details = state["latest"]["board_diagnostics"]["results"][-1]["details"]
    assert details["limits"] == details["child_limits"] == [BMS_VERSION_ADDRESS_SPACE] * 2
    assert details["large_allocation_blocked"] is True
    assert resource.getrlimit(resource.RLIMIT_AS) == original


def test_bms_version_refuses_unbounded_execution_when_prlimit_missing(tmp_path, monkeypatch):
    from rp1_monitor import robopi_tui

    raw = extended_native_report("RP022")
    path, calls = executor(tmp_path, raw, [row["plugin"] for row in raw["results"]])
    monkeypatch.setattr(robopi_tui, "PRLIMIT", tmp_path / "missing-prlimit")
    board = controller(tmp_path, path)
    board.start("RP022")
    state = finish(board)
    assert state["job"]["state"] == "error"
    assert "prlimit" in state["job"]["error"]
    assert state["latest"] is None
    assert len(calls.read_text().splitlines()) == 1  # Metadata only; no plugin executed.
