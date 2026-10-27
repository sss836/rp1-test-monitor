"""Hardware checks stay explicit, robot-scoped and readable across report updates."""

import os
from copy import deepcopy

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")
pytest.importorskip("pyqtgraph")

from PySide6.QtCore import QCoreApplication, QEvent, QObject, Signal  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from rp1_monitor import desktop_operations  # noqa: E402
from rp1_monitor.app_shell import AppMonitorWindow  # noqa: E402
from rp1_monitor.desktop_operations import BoardDiagnosticsPanel  # noqa: E402
from rp1_monitor.robopi_tui import PASSIVE_PLUGINS  # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def panel(qt_app):
    widget = BoardDiagnosticsPanel(lambda: "http://127.0.0.1:18972")
    widget.set_robot("DEMO-RP1-01", source_kind="simulated")
    yield widget
    widget.close()
    widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    qt_app.processEvents()


@pytest.fixture
def workers(monkeypatch):
    created = []

    class Worker(QObject):
        done = Signal(object)
        failed = Signal(str)
        finished = Signal()

        def __init__(self, url, action, token="", body=None):
            super().__init__()
            self.url, self.action, self.body = url, action, body
            created.append(self)

        def start(self):
            pass

        def respond(self, result):
            self.done.emit(result)
            self.finished.emit()

    monkeypatch.setattr(desktop_operations, "OperationWorker", Worker)
    yield created
    desktop_operations._WORKERS.clear()


def check(identity, *, result="pass", stamp="2026-10-26T01:00:00Z", source="simulated"):
    return {
        "id": identity,
        "label": PASSIVE_PLUGINS[identity],
        "latest": {
            "report_id": "00000000-0000-0000-0000-000000000001",
            "robot_id": "DEMO-RP1-01",
            "created_at": stamp,
            "source_kind": source,
            "validation_scope": "simulated",
            "tui_version": "1.0.0",
            "result": result,
            "message": f"{identity} 合成证据",
            "details": {"sample": identity},
        },
    }


def status(*checks):
    return {
        "robot_id": "DEMO-RP1-01",
        "enabled": True,
        "detail": "仅指定被动插件",
        "checks": list(checks),
        "job": {"state": "idle"},
    }


def test_catalog_selection_and_get_refresh_do_not_execute(panel, workers):
    assert panel.table.rowCount() == panel.select.count() == 14
    assert panel.select.findData("RP040") == -1
    assert "未检查 14" in panel.summary.text()
    assert not panel.run.isEnabled() and not workers
    index = panel.select.findData("RP022")
    panel.select.setCurrentIndex(index)
    assert panel.table.item(panel.table.currentRow(), 1).text().startswith("RP022")
    assert "192 MiB" in panel.selection_note.text()
    assert "未知" in panel.details.toPlainText()
    panel.request("board_status")
    assert len(workers) == 1 and workers[0].action == "board_status"
    assert workers[0].body is None
    workers[0].respond(status())
    assert panel.run.isEnabled()
    panel.request("board_run")
    assert workers[-1].body == {"robot_id": "DEMO-RP1-01", "plugin_id": "RP022"}
    workers[-1].respond({"state": "running"})
    assert panel.poll.isActive() and not panel.run.isEnabled()
    before = len(workers)
    panel.request("board_run")
    assert len(workers) == before


def test_each_item_retains_history_and_selected_details(panel):
    panel.apply_status(status(check("RP007", result="needs_review")))
    panel.select.setCurrentIndex(panel.select.findData("RP007"))
    assert "待人工复核" in panel.details.toPlainText()
    panel.apply_status(status(check("RP022", result="error", stamp="2026-10-26T02:00:00Z")))
    assert set(panel.reports) == {"RP007", "RP022"}
    assert "RP007 合成证据" in panel.details.toPlainText()
    assert "RP022 合成证据" not in panel.details.toPlainText()
    panel.select.setCurrentIndex(panel.select.findData("RP022"))
    assert "检查错误" in panel.details.toPlainText()
    assert "模拟数据" in panel.details.toPlainText()
    assert "2026-10-26T02:00:00Z" in panel.details.toPlainText()
    assert "错误 1" in panel.summary.text() and "未检查 12" in panel.summary.text()


def test_clock_rollback_does_not_hide_new_failure(panel):
    panel.apply_status(status(check("RP007", result="pass", stamp="2026-10-26T04:00:00Z")))
    failure = check("RP007", result="fail", stamp="2026-10-26T03:00:00Z")
    failure["latest"]["report_id"] = "00000000-0000-0000-0000-000000000002"
    panel.apply_status(status(failure))
    assert panel.reports["RP007"]["result"] == "fail"
    assert "不符合 1" in panel.summary.text()


def test_global_raw_report_does_not_overwrite_authoritative_check_head(panel):
    incoming = status(check("RP007", result="fail", stamp="2026-10-26T03:00:00Z"))
    incoming["latest"] = {
        "robot_id": "DEMO-RP1-01",
        "source_kind": "simulated",
        "created_at": "2026-10-26T04:00:00Z",
        "board_diagnostics": {
            "tui_version": "1.0.0",
            "results": [{"id": "RP007", "result": "pass", "message": "较早保存但墙钟较大的报告"}],
        },
    }
    panel.apply_status(incoming)
    assert panel.reports["RP007"]["result"] == "fail"


def test_nested_foreign_robot_report_is_rejected(panel):
    incoming = status(check("RP007"))
    incoming["checks"][0]["latest"]["robot_id"] = "OTHER"
    with pytest.raises(ValueError, match="来源无效"):
        panel.apply_status(incoming)
    assert panel.reports == {}
    incoming = status()
    incoming["latest"] = {"robot_id": "OTHER"}
    with pytest.raises(ValueError, match="身份"):
        panel.apply_status(incoming)


def test_old_latest_response_keeps_other_project_results(panel):
    panel.apply_status(status(check("RP007", result="needs_review")))
    old = status()
    old.pop("checks")
    old["latest"] = {
        "robot_id": "DEMO-RP1-01",
        "source_kind": "simulated",
        "created_at": "2026-10-26T03:00:00Z",
        "board_diagnostics": {
            "tui_version": "1.0.0",
            "outcome": "incomplete",
            "results": [{"id": "RP021", "result": "skip", "message": "前置未通过"}],
        },
    }
    panel.apply_status(old)
    assert set(panel.reports) == {"RP007", "RP021"}
    panel.select.setCurrentIndex(panel.select.findData("RP021"))
    assert "已跳过" in panel.details.toPlainText()
    assert "前置未通过" in panel.details.toPlainText()
    assert "模拟数据" in panel.output.toPlainText()


def test_request_failure_and_disconnect_keep_historical_evidence(panel, workers):
    panel.apply_status(status(check("RP007")))
    panel.select.setCurrentIndex(panel.select.findData("RP007"))
    evidence = panel.details.toPlainText()
    panel.request("board_status")
    workers[-1].failed.emit("无法连接")
    workers[-1].finished.emit()
    assert panel.details.toPlainText() == evidence
    assert not panel.run.isEnabled() and panel.refresh.isEnabled()
    assert "历史结果保留" in panel.status.text()
    panel.set_robot(None)
    assert panel.details.toPlainText() == evidence
    assert not panel.refresh.isEnabled() and not panel.run.isEnabled()
    before = len(workers)
    panel.request("board_status")
    panel.request("board_run")
    assert len(workers) == before


def test_switching_robot_ignores_late_worker_and_mismatched_sources(panel, workers):
    panel.apply_status(status(check("RP007")))
    panel.request("board_status")
    worker = workers[-1]
    panel.set_robot("RP1-REAL", source_kind="ros2")
    worker.respond(status(check("RP022")))
    assert not panel.reports and panel.output.toPlainText() == ""
    wrong_source = status(check("RP007"))
    wrong_source["robot_id"] = "RP1-REAL"
    with pytest.raises(ValueError, match="来源"):
        panel.apply_status(wrong_source)
    assert not panel.reports


def test_cooldown_reading_does_not_restart_plugin(panel, workers):
    snapshot = status(check("RP020"))
    snapshot["cooldown_remaining_seconds"] = 3
    panel.apply_status(snapshot)
    assert not panel.run.isEnabled() and "冷却" in panel.run.text()
    panel.request("board_status")
    assert len(workers) == 1 and workers[-1].action == "board_status"
    workers[-1].respond(snapshot)
    panel._cooldown_until = 0
    panel._update_controls()
    assert panel.run.isEnabled()


def test_invalid_new_report_cannot_erase_old_results(panel):
    panel.apply_status(status(check("RP007")))
    before = deepcopy(panel.reports)
    invalid = check("RP022")
    invalid["latest"]["source_kind"] = "invented"
    with pytest.raises(ValueError):
        panel.apply_status(status(invalid))
    assert panel.reports == before


def test_hardware_page_navigation_keeps_assistant_at_index_five(qt_app):
    window = AppMonitorWindow("http://127.0.0.1:18972")
    window.show()
    qt_app.processEvents()
    try:
        assert window.pages.count() == 7
        assert window.nav.button(5).text() == "诊断助手"
        assert window.nav.button(6).text() == "硬件自检"
        window.navigate(6)
        assert window.pages.currentWidget() is window.operations_panel.board_panel
        window.navigate(5)
        assert window.operations_panel.isVisible()
        assert not window.operations_panel.board_panel.isVisible()
        window.operations_panel.board_checks.click()
        assert window.pages.currentIndex() == 6
    finally:
        window.close()
        window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        qt_app.processEvents()
