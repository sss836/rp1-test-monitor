import threading
from types import SimpleNamespace

import pytest

from rp1_monitor import aggregation
from rp1_monitor.aggregation import Accumulator
from rp1_monitor.sources import RosSource


def test_spike_is_preserved_between_preview_frames():
    accumulator = Accumulator("TEST", "simulated")
    for value in (1, 99, 2):
        accumulator.observe("/joint_states", {"joint.0.torque": value})
    assert accumulator.preview().values["joint.0.torque"] == 2
    result = accumulator.flush().metrics["joint.0.torque"]
    assert (result.minimum, result.maximum, result.last, result.count) == (1, 99, 2, 3)


def test_missing_values_are_not_forward_filled_and_quaternion_is_not_averaged():
    accumulator = Accumulator("TEST", "simulated")
    accumulator.observe("/imu", {"imu.quaternion.w": 0.9})
    first = accumulator.flush().metrics["imu.quaternion.w"]
    assert first.last == 0.9 and first.minimum is None and first.maximum is None
    second = accumulator.flush().metrics["imu.quaternion.w"]
    assert second.count == 0 and second.last is None


def test_nonfinite_input_is_reported_not_stored():
    accumulator = Accumulator("TEST", "simulated")
    accumulator.observe(
        "/joint_states", {"joint.0.position": float("nan"), "joint.0.velocity": float("inf")}
    )
    result = accumulator.flush()
    assert result.metrics == {} and result.quality.invalid_values == 2


def test_lock_boundary_callback_does_not_create_negative_ages(monkeypatch):
    current = [1_000_000_000]
    monkeypatch.setattr(aggregation.time, "monotonic_ns", lambda: current[0])
    accumulator = Accumulator("TEST", "simulated")
    real_lock = threading.Lock()

    class ArrivalLock:
        def __enter__(self):
            accumulator._lock = real_lock
            current[0] += 1_000_000
            accumulator.observe("/joint_states", {"joint.0.position": 1})
            accumulator._lock = self
            real_lock.acquire()

        def __exit__(self, *_):
            real_lock.release()

    accumulator._lock = ArrivalLock()
    assert accumulator.preview().ages_ms["joint.0.position"] >= 0
    assert accumulator.flush().metrics["joint.0.position"].age_ms >= 0


def test_delayed_scheduler_window_recovers():
    accumulator = Accumulator("TEST", "simulated")
    accumulator._start_ns -= 11_000_000_000
    result = accumulator.flush()
    assert (result.window_end - result.window_start).total_seconds() > 10
    assert accumulator.flush().sequence == 2


def test_stale_preview_is_null(monkeypatch):
    current = [1_000_000_000]
    monkeypatch.setattr(aggregation.time, "monotonic_ns", lambda: current[0])
    accumulator = Accumulator("TEST", "simulated")
    accumulator.observe("/joint_states", {"joint.0.position": 2})
    current[0] += 2_000_000_000
    assert accumulator.preview().values["joint.0.position"] is None


def test_ros_adapter_preserves_arrays_and_never_invents_acceleration():
    accumulator = Accumulator("TEST", "ros2")
    source = RosSource.__new__(RosSource)
    source.accumulator = accumulator
    header = SimpleNamespace(stamp=SimpleNamespace(sec=100, nanosec=10))
    source.joints(
        SimpleNamespace(
            header=header,
            name=["joint_0", "joint_1"],
            position=[0.1, 0.2],
            velocity=[0.3],
            effort=[],
        )
    )
    source.targets(SimpleNamespace(header=header, name=["action_0"], position=[0.4]))
    source.imu(
        SimpleNamespace(
            header=header,
            orientation=SimpleNamespace(x=0.0, y=0.0, z=0.0, w=1.0),
            angular_velocity=SimpleNamespace(x=0.1, y=0.2, z=0.3),
        )
    )
    values = accumulator.preview().values
    assert values["joint.1.position"] == 0.2
    assert "joint.1.velocity" not in values and "joint.0.torque" not in values
    assert values["joint.0.policy_target"] == 0.4
    assert not any("acceleration" in key for key in values)


def test_ros_source_does_not_silently_fall_back_to_demo(monkeypatch):
    import builtins

    original = builtins.__import__

    def no_ros(name, *args, **kwargs):
        if name == "rclpy":
            raise ImportError("no ROS in unit environment")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_ros)
    with pytest.raises(RuntimeError, match="不会自动退回"):
        RosSource(Accumulator("TEST", "ros2"))


def test_named_joint_order_is_aligned_and_unknown_targets_are_suppressed():
    accumulator = Accumulator("TEST", "ros2")
    source = RosSource.__new__(RosSource)
    source.accumulator = accumulator
    header = SimpleNamespace(stamp=SimpleNamespace(sec=100, nanosec=0))
    source.joints(
        SimpleNamespace(
            header=header, name=["hip", "knee"], position=[1.0, 2.0], velocity=[], effort=[]
        )
    )
    source.targets(SimpleNamespace(header=header, name=["knee", "hip"], position=[20.0, 10.0]))
    preview = accumulator.preview()
    assert preview.values["joint.0.policy_target"] == 10.0
    assert preview.values["joint.1.policy_target"] == 20.0
    assert preview.target_alignment == "matched_names"
    source.targets(SimpleNamespace(header=header, name=["unknown"], position=[100.0]))
    assert accumulator.preview().target_alignment == "unverified"


def test_feedback_named_reordering_preserves_canonical_indices():
    accumulator = Accumulator("TEST", "ros2")
    source = RosSource.__new__(RosSource)
    source.accumulator = accumulator
    header = SimpleNamespace(stamp=SimpleNamespace(sec=100, nanosec=0))
    source.joints(
        SimpleNamespace(
            header=header, name=["hip", "knee"], position=[1.0, 2.0], velocity=[], effort=[]
        )
    )
    source.joints(
        SimpleNamespace(
            header=header, name=["knee", "hip"], position=[20.0, 10.0], velocity=[], effort=[]
        )
    )
    assert accumulator.preview().values["joint.0.position"] == 10.0
    assert accumulator.preview().values["joint.1.position"] == 20.0


def test_unavailable_imu_covariance_and_zero_quaternion_are_missing():
    accumulator = Accumulator("TEST", "ros2")
    source = RosSource.__new__(RosSource)
    source.accumulator = accumulator
    message = SimpleNamespace(
        header=SimpleNamespace(stamp=SimpleNamespace(sec=100, nanosec=0)),
        orientation=SimpleNamespace(x=0.0, y=0.0, z=0.0, w=0.0),
        orientation_covariance=[0.0] * 9,
        angular_velocity=SimpleNamespace(x=0.0, y=0.0, z=0.0),
        angular_velocity_covariance=[-1.0] + [0.0] * 8,
    )
    source.imu(message)
    assert accumulator.preview().values == {}
    message.orientation.w = 1.0
    message.orientation_covariance[0] = -1.0
    source.imu(message)
    assert accumulator.preview().values == {}


def test_unnamed_joint_feedback_is_not_silently_indexed():
    accumulator = Accumulator("TEST", "ros2")
    source = RosSource.__new__(RosSource)
    source.accumulator = accumulator
    source.joints(
        SimpleNamespace(
            header=SimpleNamespace(stamp=SimpleNamespace(sec=100, nanosec=0)),
            name=[],
            position=[1.0],
            velocity=[],
            effort=[],
        )
    )
    assert accumulator.preview().values == {}


def test_joint_identity_is_stable_across_stream_restarts():
    header = SimpleNamespace(stamp=SimpleNamespace(sec=100, nanosec=0))
    previews = []
    for names, positions in [(["hip", "knee"], [1.0, 2.0]), (["knee", "hip"], [20.0, 10.0])]:
        accumulator = Accumulator("TEST", "ros2")
        source = RosSource.__new__(RosSource)
        source.accumulator = accumulator
        source.joints(
            SimpleNamespace(header=header, name=names, position=positions, velocity=[], effort=[])
        )
        previews.append(accumulator.preview())
    assert previews[0].joint_names == previews[1].joint_names == ["hip", "knee"]
    assert previews[1].values["joint.0.position"] == 10.0
