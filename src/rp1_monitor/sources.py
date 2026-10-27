"""Two concrete read-only sources. ROS is imported only for an explicit ROS run."""

import logging
import math
import re
import threading
import time
from datetime import datetime, timezone

from .process_monitor import SIMULATED_WATCHES, simulate_processes


def simulated_motor_temperature(joint, elapsed, thermal_demo=False):
    if thermal_demo and joint in {3, 9, 15}:
        return 35 + 60 * (1 - math.cos(elapsed / 12 + (joint - 3) / 6)) / 2
    return 35 + joint * 0.4 + math.sin(elapsed / 10)


class SimulatedSource:
    def __init__(self, accumulator, joints=24, processes=None, thermal_demo=False):
        self.accumulator = accumulator
        self.joints = joints
        self.thermal_demo = thermal_demo
        self.processes = processes if processes is not None else SIMULATED_WATCHES
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self.run, name="simulated-ros-source", daemon=True)

    def start(self):
        self.thread.start()

    def run(self):
        started = time.monotonic()
        for name in ("bms", "host", "motors"):
            self.accumulator.set_health_source(
                name, "live", "模拟健康数据，仅用于软件演示，未连接实机"
            )
        self.accumulator.set_health_sensors({"host.thermal.0.temperature": "模拟CPU热区"})
        last_health = -1
        last_process = -2
        while not self.stop_event.is_set():
            elapsed = time.monotonic() - started
            if elapsed - last_process >= 2:
                simulate_processes(self.accumulator, self.processes, elapsed)
                last_process = elapsed
            feedback, targets = {}, {}
            for joint in range(self.joints):
                phase = elapsed * 2.4 + joint * 0.32
                feedback[f"joint.{joint}.position"] = 0.4 * math.sin(phase)
                feedback[f"joint.{joint}.velocity"] = 0.96 * math.cos(phase)
                feedback[f"joint.{joint}.torque"] = 4.5 * math.sin(phase + 0.8)
                targets[f"joint.{joint}.policy_target"] = 0.4 * math.sin(phase + 0.08)
            self.accumulator.observe("/joint_states", feedback)
            self.accumulator.observe("/action", targets)
            self.accumulator.observe_tracking(
                {
                    joint: (
                        feedback[f"joint.{joint}.position"],
                        targets[f"joint.{joint}.policy_target"],
                    )
                    for joint in range(self.joints)
                }
            )
            angle = 0.08 * math.sin(elapsed)
            self.accumulator.observe(
                "/imu",
                {
                    "imu.quaternion.x": math.sin(angle / 2),
                    "imu.quaternion.y": 0.0,
                    "imu.quaternion.z": 0.0,
                    "imu.quaternion.w": math.cos(angle / 2),
                    "imu.angular_velocity.x": 0.08 * math.cos(elapsed),
                    "imu.angular_velocity.y": 0.03 * math.sin(elapsed),
                    "imu.angular_velocity.z": 0.02 * math.cos(elapsed * 0.5),
                },
            )
            if elapsed - last_health >= 1:
                last_health = elapsed
                self.accumulator.observe(
                    "bms.journal",
                    {
                        "battery.soc": 82.5 - min(elapsed / 600, 2),
                        "battery.voltage": 49.2 + 0.1 * math.sin(elapsed / 10),
                        "battery.current": -3.2 - 0.4 * math.sin(elapsed / 3),
                        "battery.power_enabled": 1,
                        "battery.temperature": 31.4 + 0.2 * math.sin(elapsed / 10),
                        "battery.protection": 0,
                    },
                )
                self.accumulator.observe(
                    "host.health",
                    {
                        "host.thermal.0.temperature": 48.1 + 0.6 * math.sin(elapsed / 10),
                        "host.cpu.usage": 36 + 3 * math.sin(elapsed / 2),
                        "host.memory.usage": 42.3,
                        "host.uptime.seconds": elapsed,
                    },
                )
                self.accumulator.observe(
                    "motors.health",
                    {
                        f"joint.{joint}.{quantity}": value
                        for joint in range(self.joints)
                        for quantity, value in (
                            (
                                "motor_temperature",
                                simulated_motor_temperature(joint, elapsed, self.thermal_demo),
                            ),
                            ("mos_temperature", 33 + joint * 0.3),
                            ("fault_code", 0),
                        )
                    },
                )
            self.stop_event.wait(0.02)

    def stop(self):
        self.stop_event.set()
        self.thread.join(timeout=3)


class RosSource:
    def __init__(self, accumulator, profile=None):
        try:
            import rclpy
            from diagnostic_msgs.msg import DiagnosticArray
            from rclpy.executors import SingleThreadedExecutor
            from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy
            from sensor_msgs.msg import Imu, JointState
        except ImportError as exc:
            raise RuntimeError(
                "ROS2模式需要使用实机ROS环境的Python，并source对应setup.bash；不会自动退回模拟模式"
            ) from exc
        self.rclpy = rclpy
        self.context = rclpy.context.Context()
        rclpy.init(context=self.context)
        self.node = rclpy.create_node(
            "rp1_monitor_observer",
            context=self.context,
            enable_rosout=False,
            start_parameter_services=False,
        )
        self.executor = SingleThreadedExecutor(context=self.context)
        self.executor.add_node(self.node)
        self.error = ""
        self.accumulator = accumulator
        self.joint_names = []
        from .motor_health import MotorHealthConsumer
        from .profiles import RobotProfile

        profile = profile or RobotProfile(robot_id=accumulator.robot_id)
        self.motor_consumer = MotorHealthConsumer(accumulator, profile)
        qos = QoSProfile(
            history=HistoryPolicy.KEEP_LAST,
            depth=50,
            reliability=ReliabilityPolicy.BEST_EFFORT,
            durability=DurabilityPolicy.VOLATILE,
        )
        self.subscriptions = [
            self.node.create_subscription(JointState, "/joint_states", self.joints, qos),
            self.node.create_subscription(JointState, "/action", self.targets, qos),
            self.node.create_subscription(Imu, "/imu", self.imu, qos),
            self.node.create_subscription(
                DiagnosticArray, profile.motor_health_topic, self.motor_health, qos
            ),
        ]
        self.thread = threading.Thread(
            target=self.spin,
            name="ros2-observer",
            daemon=True,
        )

    @staticmethod
    def stamp(message):
        value = message.header.stamp.sec + message.header.stamp.nanosec / 1e9
        try:
            return datetime.fromtimestamp(value, timezone.utc) if value > 0 else None
        except (ValueError, OverflowError, OSError):
            return None

    def motor_health(self, message):
        self.motor_consumer.consume(message, self.joint_names)

    def diagnostics(self):
        topics = [
            "/joint_states",
            "/action",
            "/imu",
            self.motor_consumer.profile.motor_health_topic,
        ]
        result = []
        for topic in topics:
            publishers = self.node.get_publishers_info_by_topic(topic)
            result.append(
                {
                    "topic": topic,
                    "publisher_count": len(publishers),
                    "offered_types": sorted({p.topic_type for p in publishers}),
                    "offered_reliability": [p.qos_profile.reliability.name for p in publishers],
                    "subscriber_qos": "best_effort/volatile/keep_last(50)",
                }
            )
        return {"scope": "当前采集节点可见的ROS2图；发现发布者不证明数据正在更新", "topics": result}

    def joints(self, message):
        names = list(getattr(message, "name", []))
        canonical = getattr(self, "joint_names", [])
        valid_names = (
            bool(names)
            and len(names) <= 64
            and len(set(names)) == len(names)
            and all(isinstance(name, str) and 0 < len(name) <= 120 for name in names)
        )
        if valid_names and not canonical:
            # Stable across process restarts and publisher array reordering.
            canonical = sorted(
                names,
                key=lambda name: tuple(
                    (1, int(part)) if part.isdigit() else (0, part)
                    for part in re.split(r"(\d+)", name)
                ),
            )
            self.joint_names = canonical
            self.accumulator.set_mapping(
                canonical, "unverified", "保留ROS关节名；物理映射仍需实机验收"
            )
        if not valid_names or (canonical and set(names) != set(canonical)):
            self.feedback_mapping_valid = False
            self.accumulator.set_mapping(
                canonical,
                "unverified",
                "反馈关节名单变化，暂停更新以避免历史指标串线；请核对后重启采集实例",
            )
            self.accumulator.observe("/joint_states", {}, self.stamp(message))
            return
        self.feedback_mapping_valid = bool(canonical)
        values = {}
        for attribute, quantity in (
            ("position", "position"),
            ("velocity", "velocity"),
            ("effort", "torque"),
        ):
            samples = getattr(message, attribute)
            if len(samples) > len(names):
                continue
            for index, value in enumerate(samples):
                target = canonical.index(names[index])
                values[f"joint.{target}.{quantity}"] = value
        self.accumulator.observe("/joint_states", values, self.stamp(message))

    def targets(self, message):
        canonical = getattr(self, "joint_names", [])
        names = list(getattr(message, "name", []))
        values = {}
        alignment, note = "unverified", "目标与反馈关节名未匹配，未混入反馈索引"
        if (
            canonical
            and getattr(self, "feedback_mapping_valid", False)
            and names
            and len(names) == len(message.position)
            and len(set(names)) == len(names)
        ):
            if all(name in canonical for name in names):
                indices = [canonical.index(name) for name in names]
                alignment, note = "matched_names", "目标按ROS关节名对齐；物理映射待验收"
            elif all(re.fullmatch(r"action_\d+", name) for name in names) and all(
                re.fullmatch(r"joint_\d+", name) for name in canonical
            ):
                translated = [name.replace("action_", "joint_", 1) for name in names]
                indices = (
                    [canonical.index(name) for name in translated]
                    if all(name in canonical for name in translated)
                    else []
                )
                alignment, note = (
                    "rp1_named_index",
                    "按仓库joint_N/action_N逻辑命名协议对齐；物理映射待验收",
                )
            else:
                indices = []
            values = {
                f"joint.{index}.policy_target": value
                for index, value in zip(indices, message.position)
            }
        self.accumulator.set_mapping(canonical, alignment if values else "unverified", note)
        self.accumulator.observe(
            "/action",
            values,
            self.stamp(message),
        )

    def imu(self, message):
        values = {}
        orientation_covariance = getattr(message, "orientation_covariance", [])
        quaternion = [getattr(message.orientation, axis) for axis in "xyzw"]
        if len(orientation_covariance) == 0 or orientation_covariance[0] != -1:
            if not all(math.isfinite(value) for value in quaternion):
                values.update({f"imu.quaternion.{axis}": float("nan") for axis in "xyzw"})
            elif sum(value * value for value in quaternion) > 1e-12:
                values.update(dict(zip((f"imu.quaternion.{axis}" for axis in "xyzw"), quaternion)))
        angular_covariance = getattr(message, "angular_velocity_covariance", [])
        if len(angular_covariance) == 0 or angular_covariance[0] != -1:
            values.update(
                {
                    f"imu.angular_velocity.{axis}": getattr(message.angular_velocity, axis)
                    for axis in "xyz"
                }
            )
        self.accumulator.observe("/imu", values, self.stamp(message))

    def start(self):
        self.thread.start()

    def spin(self):
        try:
            self.executor.spin()
        except Exception:
            if self.context.ok():
                logging.getLogger(__name__).exception("ROS observation executor failed")
                self.error = "ROS观察线程异常；主机与电池采集独立运行，请检查采集日志"
                self.accumulator.set_health_source("motors", "error", self.error)

    def stop(self):
        self.executor.shutdown(timeout_sec=3)
        self.context.try_shutdown()
        self.thread.join(timeout=3)
        self.executor.remove_node(self.node)
        self.node.destroy_node()
