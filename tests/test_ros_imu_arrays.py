"""Real ROS fixed arrays can be ndarrays: never use their ambiguous truth value."""

import logging
from array import array
from types import SimpleNamespace as NS

import pytest

from rp1_monitor.aggregation import Accumulator
from rp1_monitor.sources import RosSource


class AmbiguousArray(list):
    def __bool__(self):
        raise ValueError("The truth value of an array with more than one element is ambiguous")


def source_and_message(factory):
    source = RosSource.__new__(RosSource)
    source.accumulator = Accumulator("SYNTHETIC-ROS", "ros2")
    message = NS(
        header=NS(stamp=NS(sec=100, nanosec=0)),
        orientation=NS(x=0.0, y=0.0, z=0.0, w=1.0),
        orientation_covariance=factory([0.0] * 9),
        angular_velocity=NS(x=0.1, y=0.2, z=0.3),
        angular_velocity_covariance=factory([0.0] * 9),
    )
    return source, message


@pytest.mark.parametrize("factory", [list, lambda values: array("d", values), AmbiguousArray])
def test_fixed_covariance_arrays_allow_later_joint_and_target_callbacks(factory):
    source, message = source_and_message(factory)
    names = [f"joint_{index}" for index in range(1, 25)]
    source.joints(NS(header=message.header, name=names, position=[0.1]*24,
                     velocity=[0.2]*24, effort=[0.3]*24))
    source.imu(message)
    source.targets(NS(header=message.header, name=[f"action_{i}" for i in range(1, 25)],
                      position=[0.4]*24))
    source.joints(NS(header=message.header, name=names, position=[0.5]*24,
                     velocity=[0.6]*24, effort=[0.7]*24))
    frame = source.accumulator.preview()
    assert frame.values["imu.quaternion.w"] == 1
    assert frame.values["imu.angular_velocity.z"] == 0.3
    assert frame.values["joint.23.position"] == 0.5
    assert frame.values["joint.23.torque"] == 0.7
    assert frame.values["joint.23.policy_target"] == 0.4
    assert frame.target_alignment == "rp1_named_index"


@pytest.mark.parametrize("factory", [AmbiguousArray, lambda values: array("d", values)])
def test_unavailable_covariance_keeps_imu_fields_missing(factory):
    source, message = source_and_message(factory)
    message.orientation_covariance[0] = -1
    message.angular_velocity_covariance[0] = -1
    source.imu(message)
    assert not source.accumulator.preview().values


def test_numpy_covariance_matches_real_ros_container():
    np = pytest.importorskip("numpy")
    source, message = source_and_message(lambda values: np.asarray(values, dtype=np.float64))
    source.imu(message)
    assert source.accumulator.preview().values["imu.quaternion.w"] == 1


def test_executor_failure_records_traceback_without_hiding_error_status(caplog):
    source, _ = source_and_message(list)

    def fail():
        raise RuntimeError("synthetic executor failure")

    source.executor = NS(spin=fail)
    source.context = NS(ok=lambda: True)
    with caplog.at_level(logging.ERROR, logger="rp1_monitor.sources"):
        source.spin()
    assert "synthetic executor failure" in caplog.text
    assert source.error
    assert source.accumulator.preview().health_sources["motors"].state == "error"
