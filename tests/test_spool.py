import pytest

from rp1_monitor.models import Receipt
from rp1_monitor.spool import Spool


def test_persistent_queue_and_receipt_subset(tmp_path, window):
    path = tmp_path / "queue.sqlite"
    first, second = window(), window(sequence=2)
    queue = Spool(path)
    queue.append(first)
    queue.append(second)
    restarted = Spool(path)
    assert restarted.status()["pending_windows"] == 2
    with pytest.raises(ValueError, match="未发送"):
        restarted.acknowledge([first], [Receipt(stream_id=second.stream_id, sequence=2)])
    restarted.acknowledge([first], [Receipt(stream_id=first.stream_id, sequence=1)])
    assert restarted.status()["pending_windows"] == 1
    assert restarted.oldest()[0] == second


def test_bounded_cache_exposes_eviction_ranges(tmp_path, window):
    first = window()
    budget = len(first.model_dump_json().encode()) * 2 + 20
    queue = Spool(tmp_path / "queue.sqlite", max_bytes=budget)
    for sequence in range(1, 6):
        queue.append(window(sequence=sequence))
    state = queue.status()
    assert state["pending_bytes"] <= budget
    assert state["discarded_windows"] >= 3
    assert state["recent_loss_ranges"]
    assert queue.newest().sequence == 5


def test_queued_payload_cannot_be_silently_replaced(tmp_path, window):
    first = window()
    queue = Spool(tmp_path / "queue.sqlite")
    queue.append(first)
    queue.append(first)
    assert queue.status()["pending_windows"] == 1
    with pytest.raises(ValueError, match="不可改写"):
        queue.append(window())


def test_low_disk_stops_cache_writes_without_deleting_other_data(tmp_path, window, monkeypatch):
    from types import SimpleNamespace

    from rp1_monitor import spool

    queue = Spool(tmp_path / "queue.sqlite", min_free_bytes=1000)
    monkeypatch.setattr(spool.shutil, "disk_usage", lambda _: SimpleNamespace(free=500))
    with pytest.raises(OSError, match="剩余空间"):
        queue.append(window())
    assert queue.status()["pending_windows"] == 0


def test_upper_computer_never_evicts_unconfirmed_windows(tmp_path, window):
    first = window()
    queue = Spool(
        tmp_path / "relay.sqlite",
        max_bytes=len(first.model_dump_json().encode()) + 10,
        discard_oldest=False,
    )
    queue.append(first)
    with pytest.raises(OSError, match="已满"):
        queue.append(window(sequence=2))
    assert queue.oldest() == [first]
    assert queue.status()["discarded_windows"] == 0


def test_durable_queue_cannot_change_robot_or_source(tmp_path, window):
    path = tmp_path / "queue.sqlite"
    queue = Spool(path)
    queue.append(window())
    queue.bind("RP1-TEST", "simulated")
    restarted = Spool(path)
    with pytest.raises(ValueError, match="绑定"):
        restarted.bind("OTHER", "simulated")
    with pytest.raises(ValueError, match="绑定"):
        restarted.bind("RP1-TEST", "ros2")
