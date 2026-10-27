"""The desktop reads the intended robot through a verified local relay or temporary SSH."""

import os
import time

import httpx
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")
pytest.importorskip("pyqtgraph")

from PySide6.QtCore import QCoreApplication, QEvent, QObject, Signal  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from rp1_monitor import desktop, desktop_connection  # noqa: E402
from rp1_monitor.aggregation import Accumulator  # noqa: E402
from rp1_monitor.app_shell import AppMonitorWindow  # noqa: E402


@pytest.mark.parametrize(
    "url",
    [
        "http://192.168.137.250:8891",
        "http://user:secret@127.0.0.1:8892",
        "http://127.0.0.1:8892/admin",
        "http://127.0.0.1:8892?token=secret",
        "http://127.0.0.1:8892#test",
        "http://127.0.0.1:bad",
        "http://127.0.0.1:65536",
        "http://127.0.0.1:0",
        "ssh://127.0.0.1",
    ],
)
def test_desktop_rejects_nonlocal_or_nonservice_addresses(url):
    with pytest.raises(ValueError):
        desktop_connection.local_endpoint(url)


def test_endpoint_and_ssh_forward_use_distinct_ports_without_password(tmp_path):
    assert (
        desktop_connection.local_endpoint(" http://127.0.0.1:18972/ ") == "http://127.0.0.1:18972"
    )
    key = tmp_path / "test key"
    key.write_text("test fixture")
    args = desktop_connection.ssh_arguments("robo@192.168.137.250", str(key), 19888, 8891)
    assert args[-2:] == ["--", "robo@192.168.137.250"]
    assert "127.0.0.1:19888:127.0.0.1:8891" in args
    assert args[args.index("-i") + 1] == str(key)
    assert "-oStrictHostKeyChecking=yes" in args
    assert "-oBatchMode=yes" in args
    assert all("password" not in arg.lower() for arg in args)


@pytest.mark.parametrize(
    "target", ["-oProxyCommand=bad", "robo@host command", "robo@host:22", "robo@@host", "robo@-bad"]
)
def test_ssh_rejects_options_commands_and_ambiguous_targets(target):
    with pytest.raises(ValueError):
        desktop_connection.ssh_arguments(target, "", 18971, 8891)


def report(*, relay=True, robot="RP1-REAL", source="ros2"):
    return {
        "snapshot": {
            "robot_id": robot,
            "source_kind": source,
            "upload": {"stage": "upper_computer"} if relay else {},
        }
    }


def test_preflight_rejects_wrong_role_and_web_response():
    assert desktop_connection.identify_service(report(), "relay") == {
        "robot_id": "RP1-REAL",
        "source_kind": "ros2",
    }
    for body, mode in ((report(relay=False), "relay"), (report(), "collector"), ({}, "relay")):
        with pytest.raises(ValueError):
            desktop_connection.identify_service(body, mode)


def test_ssh_missing_key_and_bad_port_are_rejected_without_creating_files(tmp_path):
    key = tmp_path / "missing-key"
    with pytest.raises(ValueError, match="密钥文件不存在"):
        desktop_connection.ssh_arguments("robo@192.168.137.250", str(key), 18971, 8891)
    assert not key.exists()
    with pytest.raises(ValueError, match="端口"):
        desktop_connection.ssh_arguments("robo@192.168.137.250", "", 18971, 0)


def test_preflight_http_reads_only_doctor_and_does_not_follow_redirects(monkeypatch):
    seen = []
    original = httpx.Client

    def request(req):
        seen.append((req.method, str(req.url)))
        return httpx.Response(200, json=report())

    def client(**kwargs):
        assert kwargs["trust_env"] is False and kwargs["follow_redirects"] is False
        return original(transport=httpx.MockTransport(request), **kwargs)

    monkeypatch.setattr(desktop_connection.httpx, "Client", client)
    probe = desktop_connection.ConnectionProbe("http://127.0.0.1:18972", "relay")
    received, failed = [], []
    probe.done.connect(received.append)
    probe.failed.connect(failed.append)
    probe.run()
    assert received == [{"robot_id": "RP1-REAL", "source_kind": "ros2"}] and not failed
    assert seen == [("GET", "http://127.0.0.1:18972/api/v1/doctor")]


@pytest.fixture(scope="module")
def qt_app():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def window(qt_app):
    widget = AppMonitorWindow("http://127.0.0.1:18972")
    widget.show()
    qt_app.processEvents()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    yield widget
    widget.close()
    widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    qt_app.processEvents()


@pytest.fixture
def workers(monkeypatch):
    probes, readers = [], []

    class Probe(QObject):
        done = Signal(dict)
        failed = Signal(str)
        finished = Signal()

        def __init__(self, url, mode):
            super().__init__()
            self.url, self.mode, self.interrupted = url, mode, False
            probes.append(self)

        def start(self):
            pass

        def requestInterruption(self):
            self.interrupted = True

        def wait(self, _):
            return True

    class Reader(QObject):
        frame_received = Signal(dict)
        status = Signal(str)

        def __init__(self, url):
            super().__init__()
            self.url, self.stopped = url, False
            readers.append(self)

        def start(self):
            pass

        def stop(self):
            self.stopped = True

    monkeypatch.setattr(desktop, "ConnectionProbe", Probe)
    monkeypatch.setattr(desktop, "Reader", Reader)
    yield probes, readers
    desktop._PROBES.clear()


def test_relay_mode_requires_preflight_and_live_data_before_connection_success(window, workers):
    probes, readers = workers
    assert window.connection_mode.currentData() == "relay"
    assert not window.ssh_target.isEnabled() and not window.ssh_port.isEnabled()
    window.connect_source()
    assert not readers and "预检" in window.connection_label.text()
    assert probes[0].url == "http://127.0.0.1:18972" and probes[0].mode == "relay"
    probes[0].done.emit({"robot_id": "RP1-REAL", "source_kind": "ros2"})
    assert readers[0].url == "ws://127.0.0.1:18972/api/v1/telemetry"
    assert "等待" in window.connection_label.text()
    assert window.operations_panel.source_url() == "http://127.0.0.1:18972"
    sample = Accumulator("RP1-REAL", "ros2").preview().model_dump(mode="json")
    sample["upload"] = {"stage": "upper_computer", "collector_connected": True}
    readers[0].frame_received.emit(sample)
    assert "上位机已连接" in window.connection_label.text()
    assert "ROS2来源" in window.source_label.text()
    window.disconnect_source()
    assert "未知" in window.upload_label.text()
    assert window.source_label.text() == "来源待确认"


def test_preflight_failure_is_retryable_and_late_probe_and_frame_cannot_switch_robot(
    window, workers
):
    probes, readers = workers
    window.connect_source()
    first = probes[-1]
    first.failed.emit("relay未收到机器人预览")
    assert window.connect_button.isEnabled() and not readers
    window.connect_source()
    second = probes[-1]
    first.done.emit({"robot_id": "OLD", "source_kind": "simulated"})
    assert not readers
    second.done.emit({"robot_id": "RP1-REAL", "source_kind": "ros2"})
    first_reader = readers[-1]
    sample = Accumulator("OTHER", "ros2").preview().model_dump(mode="json")
    sample["upload"] = {"stage": "upper_computer"}
    first_reader.frame_received.emit(sample)
    assert first_reader.stopped and window.connect_button.isEnabled()
    assert "不一致" in window.connection_label.text() and window.last_frame is None
    first_reader.status.emit("old connection error")
    assert "不一致" in window.connection_label.text()


def test_temporary_ssh_mode_is_explicit_and_uses_active_tunnel_for_doctor(
    window, workers, monkeypatch
):
    probes, readers = workers
    processes = []

    class Process(QObject):
        started = Signal()
        errorOccurred = Signal(object)
        finished = Signal(int, object)

        def __init__(self, parent):
            super().__init__(parent)
            self.stopped = False
            processes.append(self)

        def setProgram(self, program):
            self.program = program

        def setArguments(self, args):
            self.args = args

        def start(self):
            self.started.emit()

        def terminate(self):
            self.stopped = True

        def waitForFinished(self, _):
            return True

    monkeypatch.setattr(desktop, "QProcess", Process)

    class LocalPort:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def bind(self, address):
            assert address == ("127.0.0.1", 0)

        def getsockname(self):
            return ("127.0.0.1", 19888)

    monkeypatch.setattr(desktop.socket, "socket", LocalPort)
    window.connection_mode.setCurrentIndex(1)
    assert "随窗口关闭" in window.connection_help.text()
    assert not window.url.isEnabled() and window.ssh_port.isEnabled()
    window.ssh_target.setText("robo@192.168.137.250")
    window.connect_source()
    assert len(probes) == 1 and probes[0].mode == "collector" and not readers
    assert processes[0].program == "ssh"
    assert processes[0].args[processes[0].args.index("-L") + 1].endswith(":127.0.0.1:8891")
    assert window.connect_button.isEnabled() is False
    probes[0].done.emit({"robot_id": "RP1-REAL", "source_kind": "ros2"})
    assert window.operations_panel.source_url() == probes[0].url
    assert window.operations_panel.source_url() != window.url.text()
    window.disconnect_source()
    assert processes[0].stopped and readers[0].stopped


def test_relay_clock_offset_note_keeps_live_values_visible(window):
    accumulator = Accumulator("RP1-REAL", "ros2")
    accumulator.observe("host.health", {"host.cpu.usage": 12, "host.memory.usage": 34})
    sample = accumulator.preview().model_dump(mode="json")
    sample["upload"] = {
        "stage": "upper_computer",
        "collector_connected": True,
        "clock_basis": "relay_round_trip",
        "clock_offset_seconds": 18.2,
    }
    window.on_frame(sample)
    assert "12.0%" in window.host_usage.text()
    assert "时钟偏差估计 +18.20s" in window.quality_label.text()
    assert "仍待核对" in window.quality_label.text()
    window.last_received = time.monotonic() - 3
    # Stale handling remains driven by the actual receive watchdog.
    window.reader = type("StoppedReader", (), {"stop": lambda _: None})()
    window.check_stale()
    assert "预览已过期" in window.connection_label.text()
    assert "12.0%" not in window.host_usage.text()


def test_password_connect_requires_host_confirmation_then_reads_verified_relay(
    window, workers, monkeypatch, tmp_path
):
    probes, readers = workers
    connectors = []

    class Connector(QObject):
        done = Signal(dict)
        failed = Signal(str)
        finished = Signal()

        def __init__(self, url, token_file, body):
            super().__init__()
            self.body = body
            connectors.append(self)

        def start(self):
            pass

        def requestInterruption(self):
            pass

        def wait(self, _):
            return True

    monkeypatch.setattr(desktop, "StationConnector", Connector)
    token = tmp_path / "viewer-token.txt"
    token.write_text("test-only-viewer-token")
    window.station_url, window.station_token_file = "http://127.0.0.1:18980", token
    window.connection_mode.setCurrentIndex(2)
    window.ssh_target.setText("ssh robo@192.168.137.250")
    window.ssh_password.setText("test-only-password")
    window.connect_source()
    assert not probes and not readers
    connectors[0].done.emit({"status": "host_key_required", "fingerprint": "SHA256:test"})
    assert window.connect_button.isEnabled() and window.station_fingerprint == "SHA256:test"
    assert not window.ssh_trust.isChecked()
    window.ssh_trust.setChecked(True)
    window.connect_source()
    assert connectors[1].body["confirmed_fingerprint"] == "SHA256:test"
    connectors[1].done.emit({"status": "connected", "relay_url": "http://127.0.0.1:12346",
                             "central_url": "http://127.0.0.1:18980"})
    assert not window.ssh_password.text()
    assert probes[-1].mode == "relay" and probes[-1].url == "http://127.0.0.1:12346"
    assert window.operations_panel.server.text() == "http://127.0.0.1:18980"
    assert window.operations_panel.token.text() == "test-only-viewer-token"
    window.disconnect_source()
    connectors[0].done.emit({"status": "host_key_required", "fingerprint": "SHA256:late"})
    assert window.station_fingerprint != "SHA256:late"
