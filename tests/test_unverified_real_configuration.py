from rp1_monitor.central_policy import load_central_policy
from rp1_monitor.health_view import build_health
from rp1_monitor.monitor_config import empty_configuration, monitoring_runtime
from rp1_monitor.motor_map import MotorMapConfig, attach_motor_map, build_motor_map


def test_real_robot_draft_roster_survives_without_ros_names_or_temperatures():
    config = empty_configuration(24)
    envelope = {
        "source": "database",
        "robot_id": "REAL-TEST",
        "source_kind": "ros2",
        "configuration": config.model_dump(),
    }
    mapping, policy = monitoring_runtime(
        envelope, "REAL-TEST", "ros2", MotorMapConfig(), load_central_policy(None)
    )
    health = build_health(
        {"battery.soc": 48, "battery.voltage": 50, "battery.current": -0.2},
        {"battery.soc": 100, "battery.voltage": 100, "battery.current": 100},
        {},
        {},
    )
    motor_map = build_motor_map("REAL-TEST", "ros2", health, mapping, policy.temperature_limits_c)
    attach_motor_map(health, motor_map)
    assert len(health["motor_map"]["rows"]) == 24
    assert all(row.get("region") is None for row in health["motor_map"]["rows"])
    assert health["battery"]["soc"] == 48
    assert health["motors"]["available"] == 0
