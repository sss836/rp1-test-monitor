"""The Qt host cards show observed per-core usage without inventing topology or freshness."""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")
pytest.importorskip("pyqtgraph")

from PySide6.QtCore import QCoreApplication, QEvent  # noqa: E402
from PySide6.QtWidgets import QApplication, QScrollArea  # noqa: E402

from rp1_monitor import desktop  # noqa: E402
from rp1_monitor.aggregation import Accumulator  # noqa: E402
from rp1_monitor.app_shell import AppMonitorWindow  # noqa: E402
from rp1_monitor.health_view import build_health  # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def window(qt_app):
    widget = AppMonitorWindow("http://127.0.0.1:8892")
    widget.show()
    qt_app.processEvents()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    yield widget
    widget.close()
    widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    qt_app.processEvents()


def observed(cores=None, *, online=True):
    acc = Accumulator("RP1-CPU-TEST", "ros2")
    acc.observe("host.health", {"host.cpu.usage": 12, "host.memory.usage": 34})
    frame = acc.preview().model_dump(mode="json")
    frame["health"] = build_health(
        frame["values"], frame["ages_ms"], frame["health_sources"], frame["health_sensors"]
    )
    frame["health"]["host"]["cores"] = cores or []
    frame["health"]["host"]["cpu_topology_note"] = "测试拓扑：来自内核容量分组，未按编号猜测"
    frame["upload"] = {"stage": "upper_computer", "collector_connected": online}
    return frame


def core(index, usage, group="unknown", age=100):
    return {
        "index": index,
        "label": f"CPU{index}",
        "group": group,
        "usage_percent": usage,
        "age_ms": age,
    }


def test_each_big_little_core_is_visible_and_keeps_zero_and_hundred(window):
    rows = [
        core(i, (0, 10.25, 50, 100, 1, 2, 3, 4)[i], "little" if i < 4 else "big") for i in range(8)
    ]
    window.on_frame(observed(rows))
    assert window.host_core_table.rowCount() == 8
    assert window.host_core_table.item(0, 0).text() == "CPU0"
    assert window.host_core_table.item(0, 1).text() == "小核"
    assert window.host_core_table.item(0, 2).text() == "0.0%"
    assert window.host_core_table.item(3, 2).text() == "100.0%"
    assert window.host_core_table.item(4, 1).text() == "大核"
    assert "CPU1 10.2%" in window.host_core_summary.text()
    assert "大核：CPU4" in window.host_core_summary.text()
    assert "小核：CPU0" in window.host_core_summary.text()
    assert "8 个逻辑CPU" in window.host_core_note.text()
    assert "未按编号猜测" in window.host_core_note.text()


def test_core_count_and_ids_follow_snapshot_without_assuming_eight(window):
    window.on_frame(observed([core(12, 11, "big"), core(2, 22), core(4, 33, "little")]))
    assert window.host_core_table.rowCount() == 3
    assert [window.host_core_table.item(i, 0).text() for i in range(3)] == ["CPU2", "CPU4", "CPU12"]
    assert "分组未知：CPU2" in window.host_core_summary.text()
    window.on_frame(observed([core(9, 50)]))
    assert window.host_core_table.rowCount() == 1
    assert "CPU12" not in window.host_core_summary.text()


@pytest.mark.parametrize(
    "usage, age",
    [
        (None, 10),
        (50, None),
        (50, 5001),
        (50, -1),
        (-1, 10),
        (101, 10),
        (float("nan"), 10),
        (True, 10),
    ],
)
def test_missing_invalid_or_stale_core_usage_is_unknown(window, usage, age):
    window.on_frame(observed([core(0, usage, "big", age)]))
    assert window.host_core_table.item(0, 2).text() == "未知"
    assert "CPU0 未知" in window.host_core_summary.text()
    assert "CPU0 0.0%" not in window.host_core_summary.text()


def test_collector_loss_and_preview_expiry_hide_all_per_core_values(window):
    frame = observed([core(0, 25, "little"), core(1, 75, "big")])
    window.on_frame(frame)
    window.render_health(None, stale=True)
    assert window.host_core_table.rowCount() == 2
    assert all(window.host_core_table.item(i, 2).text() == "未知" for i in range(2))
    assert "25.0%" not in window.host_core_summary.text()
    window.on_frame(observed([core(0, 25)], online=False))
    assert window.host_core_table.item(0, 2).text() == "未知"


def test_legacy_collector_and_disconnect_do_not_invent_core_count(window):
    window.on_frame(observed())
    assert window.host_core_table.rowCount() == 0
    assert "尚未提供" in window.host_core_note.text()
    assert "尚未提供" in window.host_core_summary.text()
    window.on_frame(observed([core(0, 25)]))
    window.disconnect_source()
    assert window.host_core_table.rowCount() == 0
    assert "尚未提供" in window.host_core_summary.text()


def test_host_core_details_remain_scrollable_at_small_window(window, qt_app):
    window.on_frame(observed([core(i, i % 101) for i in range(32)]))
    window.resize(1000, 680)
    window.navigate(1)
    qt_app.processEvents()
    assert isinstance(window.pages.widget(1), QScrollArea)
    assert window.host_core_table.rowCount() == 32
    assert window.host_core_table.height() <= 180
    assert window.host_core_table.verticalScrollBar().maximum() > 0
    assert window.host_core_table.isVisible()


def test_per_core_age_advances_without_new_frame_and_render_cannot_renew_it(window, monkeypatch):
    now = [100.0]
    monkeypatch.setattr(desktop.time, "monotonic", lambda: now[0])
    frame = observed([core(0, 25, "little", age=4900), core(1, 75, "big", age=100)])
    window.on_frame(frame)
    window.reader = type("LocalReader", (), {"stop": lambda _: None})()
    assert window.host_core_table.item(0, 2).text() == "25.0%"
    now[0] += 0.2
    window.check_stale()
    assert not window.health_expired
    assert window.host_core_table.item(0, 2).text() == "未知"
    assert window.host_core_table.item(1, 2).text() == "75.0%"
    assert "CPU0 未知" in window.host_core_summary.text()
    assert frame["health"]["host"]["cores"][0]["age_ms"] == 4900
    assert window.last_received == 100.0
    # Re-rendering a retained frame must retain the original receive-age origin.
    now[0] += 0.1
    window.render_health(frame)
    assert window.host_core_table.item(0, 2).text() == "未知"
    assert window.last_received == 100.0
    window.check_stale()
    assert "CPU0 未知" in window.host_core_summary.text()
