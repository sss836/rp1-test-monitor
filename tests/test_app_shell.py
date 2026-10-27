"""Visible state must remain truthful when the app shell changes pages or disconnects."""

import os
import subprocess
import sys
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")
pytest.importorskip("pyqtgraph")

from PySide6.QtCore import QCoreApplication, QEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from rp1_monitor.aggregation import Accumulator  # noqa: E402
from rp1_monitor.app_shell import AppMonitorWindow  # noqa: E402

_qt_application = None


@pytest.fixture(scope="module")
def qt_app():
    global _qt_application
    _qt_application = QApplication.instance() or QApplication([])
    yield _qt_application


@pytest.fixture
def window(qt_app):
    widget = AppMonitorWindow("http://127.0.0.1:8891")
    widget.show()
    qt_app.processEvents()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    yield widget
    widget.close()
    widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    qt_app.processEvents()


def frame():
    accumulator = Accumulator("DEMO-RP1-01", "simulated")
    accumulator.observe(
        "bms.journal",
        {
            "battery.soc": 82.5,
            "battery.voltage": 49.2,
            "battery.current": -3.2,
            "battery.temperature": 31.4,
            "battery.protection": 0,
        },
    )
    accumulator.observe(
        "host.health",
        {
            "host.thermal.0.temperature": 48.1,
            "host.cpu.usage": 36,
            "host.memory.usage": 42,
        },
    )
    accumulator.observe(
        "motors.health",
        {
            "joint.0.motor_temperature": 54.2,
            "joint.0.mos_temperature": 42,
            "joint.0.fault_code": 0,
        },
    )
    return accumulator.preview().model_dump(mode="json")


def test_upper_computer_shows_robot_link_and_central_outage_separately(window):
    sample = frame()
    sample["upload"] = {
        "stage": "upper_computer",
        "collector_connected": True,
        "pending_windows": 7,
        "robot_pending_windows": 2,
        "robot_discarded_windows": 1,
        "error": "central unavailable",
    }
    window.on_frame(sample)
    assert "机器人网线链路已连接" in window.connection_label.text()
    assert "电脑待传 7" in window.upload_label.text()
    assert "机器人待取 2" in window.upload_label.text()
    assert window.battery_soc.text() == "82.5%"
    sample["upload"]["collector_connected"] = False
    sample["values"] = {key: None for key in sample["values"]}
    sample["health"] = {}
    window.on_frame(sample)
    assert "链路失联" in window.connection_label.text()
    assert window.battery_soc.text() == "—"


def test_app_pages_keep_live_controls_and_unknown_on_disconnect(window):
    sample = frame()
    window.on_frame(sample)
    assert "模拟数据" in window.source_label.text()
    assert window.battery_soc.text() == "82.5%"
    assert window.temperature_value.text() == "54.2°C"
    assert window.group.currentText() == "温度 / °C"
    assert any(
        "电池温度" in window.metric_list.item(i).text() for i in range(window.metric_list.count())
    )
    for index in range(5):
        window.nav.button(index).click()
        assert window.pages.currentIndex() == index
        if index == 2:
            assert window.plot.isVisible() and window.table.isVisible()
    window.open_settings()
    assert window.url.text() == "http://127.0.0.1:8891"
    window.disconnect_source()
    assert window.battery_soc.text() == "—"
    assert window.temperature_value.text() == "—"
    assert window.battery_bar.isHidden()
    assert "未知" in window.motor_summary.text()
    assert all(not buffer for buffer in window.temp_buffers)


def test_faults_and_stream_changes_do_not_make_false_history(window):
    sample = frame()
    window.on_frame(sample)
    fault = deepcopy(sample)
    fault["values"]["joint.0.fault_code"] = 513
    fault["sampled_at"] = (datetime.now(timezone.utc) + timedelta(seconds=0.1)).isoformat()
    window.on_frame(fault)
    assert "513" in window.current_alerts.text()
    assert "513" in window.event_list.item(0).text()
    window.on_frame(fault)
    assert window.event_list.count() == 2
    next_stream = deepcopy(sample)
    next_stream["stream_id"] = "00000000-0000-0000-0000-000000000001"
    next_stream["sampled_at"] = (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat()
    window.on_frame(next_stream)
    assert len(window.temp_buffers[0]) == 1
    window.render_health(None, stale=True)
    assert window.host_temperature.text() == "—"
    assert "未知" in window.current_alerts.text()


def test_desktop_command_closes_cleanly():
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "rp1_monitor",
            "desktop",
            "--url",
            "http://127.0.0.1:1",
            "--quit-after",
            "0.2",
        ],
        timeout=15,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr


def test_native_diagnostics_page_survives_shell_reparenting_and_disconnect(window):
    window.on_frame(frame())
    window.navigate(5)
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert window.operations_panel.isVisible()
    assert window.operations_panel.table.rowCount() == 6
    window.disconnect_source()
    assert window.operations_panel.table.rowCount() == 0
    assert "未知" in window.operations_panel.profile.text()


@pytest.fixture
def chat_workers(monkeypatch):
    from PySide6.QtCore import QObject, Signal

    from rp1_monitor import desktop_operations

    workers = []

    class Worker(QObject):
        done = Signal(object)
        failed = Signal(str)
        finished = Signal()

        def __init__(self, url, action, token="", body=None):
            super().__init__()
            self.url, self.action, self.body = url, action, body
            workers.append(self)

        def start(self):
            pass

        def respond(self, result):
            self.done.emit(result)
            self.finished.emit()

    monkeypatch.setattr(desktop_operations, "OperationWorker", Worker)
    yield workers
    desktop_operations._WORKERS.clear()


def test_board_check_ui_is_local_explicit_and_keeps_simulated_history(window, qt_app, chat_workers):
    window.on_frame(frame())
    panel = window.operations_panel.board_panel
    assert not chat_workers and not panel.run.isEnabled()
    panel.request("board_status")
    worker = chat_workers[-1]
    assert worker.action == "board_status" and "127.0.0.1" in worker.url
    result = {
        "robot_id": "DEMO-RP1-01",
        "enabled": True,
        "detail": "仅指定被动检查",
        "latest": {
            "robot_id": "DEMO-RP1-01",
            "source_kind": "simulated",
            "created_at": "2026-10-26T00:00:00Z",
            "board_diagnostics": {
                "tui_version": "1.0.0",
                "outcome": "incomplete",
                "results": [
                    {"id": "RP007", "result": "needs_review", "message": "<img> 合成结果，待复核"}
                ],
            },
        },
    }
    worker.respond(result)
    assert panel.run.isEnabled()
    assert "模拟数据" in panel.output.toPlainText() and "待人工复核" in panel.output.toPlainText()
    panel.request("board_run")
    worker = chat_workers[-1]
    assert worker.body == {"robot_id": "DEMO-RP1-01", "plugin_id": "RP001"}
    worker.respond({"state": "running"})
    assert panel.poll.isActive() and not panel.run.isEnabled()
    panel.poll.stop()
    panel.request("board_status")
    late_worker = chat_workers[-1]
    panel.set_robot("OTHER")
    late_worker.respond(result)
    assert panel.output.toPlainText() == "" and not panel.run.isEnabled()
    # Long catalog labels must not widen the fixed diagnostic sidebar or clip reports.
    from PySide6.QtWidgets import QScrollArea

    window.operations_panel.metric_select.addItem(
        "反馈相对策略目标误差 RMS（同采样配对）" * 3, "joint.0.position"
    )
    qt_app.processEvents()
    for area in window.operations_panel.findChildren(QScrollArea):
        if area.widget() == panel.parentWidget():
            assert area.widget().width() <= area.viewport().width()
    window.operations_panel.mark_disconnected()
    assert not panel.refresh.isEnabled()


def test_chat_keyboard_send_followup_and_folded_evidence(window, qt_app, chat_workers):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    from rp1_monitor.desktop_chat import ChatMessage

    window.on_frame(frame())
    window.navigate(5)
    panel = window.operations_panel
    panel.question.setPlainText("温度来自哪里？")
    panel.question.setFocus()
    QTest.keyClick(panel.question, Qt.Key.Key_Return, Qt.KeyboardModifier.ShiftModifier)
    assert panel.question.toPlainText().count("\n") == 1
    assert not chat_workers
    QTest.keyClick(panel.question, Qt.Key.Key_Return)
    assert panel.busy and not panel.ask.isEnabled()
    assert panel.question.toPlainText() == ""
    worker = chat_workers[-1]
    assert worker.body[1]["question"] == "温度来自哪里？"
    assert worker.body[1]["history"] == []
    assert worker.body[1]["use_model"] is False
    answer = {
        "mode": "local_rules",
        "answer": "当前温度来自模拟观测。[E1]",
        "evidence": {"E1": {"source_kind": "simulated"}},
    }
    worker.respond(answer)
    qt_app.processEvents()
    bubbles = [row.findChild(ChatMessage) for row in panel.messages]
    assert [bubble.body.text() for bubble in bubbles] == ["温度来自哪里？", answer["answer"]]
    assert "本地规则" in bubbles[-1].meta.text()
    assert not bubbles[-1].evidence.isVisible()
    bubbles[-1].evidence_toggle.click()
    assert bubbles[-1].evidence.isVisible()
    assert "simulated" in bubbles[-1].evidence.toPlainText()
    panel.question.setPlainText("它能代表实机吗？")
    panel.start("ask")
    followup = chat_workers[-1]
    assert followup.body[0] == "DEMO-RP1-01"
    assert followup.body[1]["history"] == [
        {"role": "user", "content": "温度来自哪里？"},
        {"role": "assistant", "content": answer["answer"]},
    ]
    followup.respond(answer)


def test_chat_robot_switch_and_new_conversation_discard_inflight_answer(window, chat_workers):
    from rp1_monitor.desktop_chat import ChatMessage

    window.on_frame(frame())
    panel = window.operations_panel
    panel.question.setPlainText("检查一号机")
    panel.start("ask")
    old = chat_workers[-1]
    other = frame()
    other["robot_id"] = "DEMO-RP1-02"
    panel.set_frame(other)
    old.respond({"mode": "local_rules", "answer": "一号机的旧回答", "evidence": {}})
    assert not panel.messages and not panel.history
    panel.question.setPlainText("检查二号机")
    panel.start("ask")
    assert chat_workers[-1].body[0] == "DEMO-RP1-02"
    assert chat_workers[-1].body[1]["history"] == []
    panel.new_conversation()
    chat_workers[-1].respond({"mode": "local_rules", "answer": "二号机的旧回答", "evidence": {}})
    assert not panel.messages and not panel.history
    assert not any(b.body.text().endswith("旧回答") for b in panel.findChildren(ChatMessage))


def test_chat_failure_restores_question_and_disconnect_rejects_old_reply(window, chat_workers):
    from rp1_monitor.desktop_chat import ChatMessage

    window.on_frame(frame())
    panel = window.operations_panel
    panel.question.setPlainText("   ")
    panel.start("ask")
    assert not chat_workers
    panel.question.setPlainText("请检查接入")
    panel.start("ask")
    chat_workers[-1].failed.emit("服务暂不可达")
    chat_workers[-1].finished.emit()
    assert panel.question.toPlainText() == "请检查接入"
    assert not panel.busy and panel.ask.isEnabled()
    assert panel.messages[-1].findChild(ChatMessage).body.text() == "服务暂不可达"
    assert not panel.history
    panel.start("ask")
    panel.mark_disconnected()
    chat_workers[-1].respond({"mode": "ai", "answer": "过期答案", "evidence": {}})
    assert "过期答案" not in [row.findChild(ChatMessage).body.text() for row in panel.messages]
    assert "历史查询快照" in panel.status.text()


def test_model_settings_select_clear_keys_and_test_without_robot(window, chat_workers):
    panel = window.operations_panel
    models = panel.models
    models.provider.setCurrentIndex(models.provider.findData("deepseek"))
    assert models.model.currentText() == "deepseek-flash"
    models.key.setText("fake-deepseek-key")
    models.provider.setCurrentIndex(models.provider.findData("qwen"))
    assert models.key.text() == ""
    assert models.model.currentText() == "qwen-plus"
    models.key.setText("fake-qwen-key")
    panel.start("probe")
    worker = chat_workers[-1]
    assert worker.action == "probe"
    assert worker.body == {"provider": "qwen", "model": "qwen-plus", "api_key": "fake-qwen-key"}
    worker.respond({"status": "ready", "provider": "qwen", "model": "qwen-plus"})
    assert "连接成功" in models.status.text() and "fake-qwen-key" not in models.status.text()
    assert not panel.messages


def test_process_panel_curves_and_disconnect_do_not_reuse_live_state(window):
    from rp1_monitor.process_monitor import SIMULATED_WATCHES, simulate_processes

    acc = Accumulator("DEMO-RP1-01", "simulated")
    simulate_processes(acc, SIMULATED_WATCHES, 10)
    window.on_frame(acc.preview().model_dump(mode="json"))
    assert window.process_table.rowCount() == 2
    assert window.process_table.item(0, 1).text() == "存活"
    assert window.process_table.item(0, 3).text() != "—"
    window.group.setCurrentIndex(10)
    assert window.metric_list.count() == 2
    simulate_processes(acc, SIMULATED_WATCHES, 31)
    window.on_frame(acc.preview().model_dump(mode="json"))
    assert window.process_table.item(1, 1).text() == "未发现"
    assert window.process_table.item(1, 2).text() == "—"
    window.render_health(None, stale=True)
    assert window.process_table.item(0, 1).text() == "未知"
    assert window.process_table.item(0, 3).text() == "—"


@pytest.mark.parametrize("dof", [24, 28])
def test_motor_map_heats_cools_and_clears_on_disconnect_and_robot_switch(window, dof):
    from rp1_monitor.central_policy import CentralPolicy
    from rp1_monitor.motor_map import MotorMapConfig, temperature_color

    window.motor_map_config = MotorMapConfig.model_validate(
        {
            "robots": {
                "DEMO-RP1-01": {
                    "source_kind": "simulated",
                    "dof": dof,
                    "scale_min_c": 20,
                    "scale_max_c": 100,
                    "joints": [{"index": 0, "label": "模拟左膝", "region": "left_knee"}],
                }
            }
        }
    )
    window.thermal_policy = CentralPolicy(
        temperature_limits_c={
            "joint.0.motor_temperature": 60,
            "joint.0.mos_temperature": 65,
        }
    )
    sample = frame()
    sample["values"]["joint.0.motor_temperature"] = 85
    window.on_frame(sample)
    assert window.robot_overview.regions[0]["state"] == "hot"
    assert window.robot_overview.regions[0]["color"] == temperature_color(85, 20, 100)
    assert "1 台电机超温" in window.motor_map_status.text()
    assert f"{dof} 自由度" in window.motor_map_status.text()
    assert window.motor_table.rowCount() == dof
    assert window.motor_table.item(dof - 1, 1).text() == "—"
    assert window.motor_coverage.text() == f"1 / {dof}"
    assert window.health_overall.text() == "存在电机超温"
    assert "超温" in window.motor_table.item(0, 0).text()
    assert (
        window.motor_table.item(0, 1).background().color()
        != window.motor_table.item(0, 2).background().color()
    )
    sample["values"]["joint.0.motor_temperature"] = 45
    window.on_frame(sample)
    assert window.robot_overview.regions[0]["state"] == "below"
    window.render_health(None, stale=True)
    assert window.robot_overview.regions[0]["state"] == "unknown"
    assert window.robot_overview.regions[0]["value"] is None
    assert "0 台电机超温" in window.motor_map_status.text()
    sample["robot_id"] = "OTHER"
    window.on_frame(sample)
    assert window.robot_overview.regions == []


def test_motor_temperature_colors_follow_identity_when_fault_rows_are_sorted_first(window):
    from rp1_monitor.central_policy import CentralPolicy
    from rp1_monitor.motor_map import MotorMapConfig, temperature_color

    window.motor_map_config = MotorMapConfig.model_validate(
        {
            "robots": {
                "DEMO-RP1-01": {
                    "source_kind": "simulated",
                    "scale_min_c": 20,
                    "scale_max_c": 100,
                    "joints": [
                        {"index": 0, "label": "零号膝", "region": "left_knee"},
                        {"index": 1, "label": "一号膝", "region": "right_knee"},
                    ],
                }
            }
        }
    )
    window.thermal_policy = CentralPolicy(
        temperature_limits_c={"joint.0.motor_temperature": 60, "joint.1.motor_temperature": 60}
    )
    sample = frame()
    sample["values"].update(
        {"joint.0.motor_temperature": 85, "joint.1.motor_temperature": 40, "joint.1.fault_code": 7}
    )
    sample["ages_ms"].update({key: 1 for key in sample["values"]})
    window.on_frame(sample)
    assert window.motor_table.item(0, 0).text() == "1 · 一号膝"
    assert window.motor_table.item(1, 0).text() == "0 · 零号膝 · 超温"
    assert window.motor_table.item(0, 1).background().color().name() == temperature_color(
        40, 20, 100
    )
    assert window.motor_table.item(1, 1).background().color().name() == temperature_color(
        85, 20, 100
    )


def test_temperature_agent_context_waits_for_submit_and_uses_both_metrics(window, chat_workers):
    window.on_frame(frame())
    window.diagnose_temperature(0, 60)
    panel = window.operations_panel
    assert not chat_workers and "关节 0" in panel.question.toPlainText()
    panel.start("ask")
    assert chat_workers[-1].body[1]["metrics"] == [
        "joint.0.motor_temperature",
        "joint.0.mos_temperature",
    ]
    assert chat_workers[-1].body[1]["use_model"] is False
    chat_workers[-1].respond({"mode": "local_rules", "answer": "本地温度证据", "evidence": {}})


def test_native_uses_synced_robot_configuration_without_changing_local_files(window):
    from rp1_monitor.monitor_config import empty_configuration

    sample = frame()
    sample["values"]["joint.0.motor_temperature"] = 85
    configuration = empty_configuration(28).model_dump()
    configuration["joints"][0]["region"] = "left_knee"
    configuration["temperature_limits_c"] = {"joint.0.motor_temperature": 90}
    sample["monitoring_configuration"] = {
        "robot_id": "DEMO-RP1-01",
        "source_kind": "simulated",
        "revision": 5,
        "source": "database",
        "configuration": configuration,
    }
    sample["upload"] = {"central_url": "http://127.0.0.1:18940"}
    window.on_frame(sample)
    assert "中央配置版本 5" in window.motor_map_status.text()
    assert window._current_motor_map["rows"][0]["state"] == "below"
    assert window.motor_table.rowCount() == 28
    assert window.operations_panel.server.text() == "http://127.0.0.1:18940"
    assert window.motor_map_config.robots == {}  # Runtime override never rewrites local files.


def test_temperature_dialog_rejects_late_response_after_robot_switch(window, qt_app, monkeypatch):
    from PySide6.QtCore import QObject, Signal

    from rp1_monitor import desktop_thermal

    workers = []

    class Worker(QObject):
        done = Signal(dict)
        failed = Signal(str)
        finished = Signal()

        def __init__(self, *args):
            super().__init__()
            self.args = args

        def start(self):
            workers.append(self)

    monkeypatch.setattr(desktop_thermal, "TemperatureWorker", Worker)
    window.on_frame(frame())
    window.motor_table.cellActivated.emit(0, 0)
    dialog = window.thermal_dialog
    assert dialog.isVisible() and workers[-1].args[2:4] == ("DEMO-RP1-01", 0)
    old = workers[-1]
    other = frame()
    other["robot_id"] = "DEMO-RP1-02"
    window.on_frame(other)
    old.done.emit({"robot_id": "DEMO-RP1-01", "joint": 0})
    old.finished.emit()
    qt_app.processEvents()
    assert not dialog.isVisible() and dialog.observation.text() == ""
