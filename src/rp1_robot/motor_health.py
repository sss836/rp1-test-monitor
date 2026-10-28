"""Consume versioned diagnostics from the existing control process. Never send CAN."""

import math
import time
from datetime import datetime, timezone


class MotorHealthConsumer:
    def __init__(self, accumulator, profile):
        self.accumulator = accumulator
        self.profile = profile
        self._last = {}
        self.rejected = 0

    def consume(self, message, canonical_names):
        stamp = message.header.stamp.sec + message.header.stamp.nanosec / 1e9
        transport_age = (datetime.now(timezone.utc).timestamp() - stamp) * 1000
        accepted = 0
        for status in message.status[:64]:
            if not status.name.startswith("rp1/motor/"):
                continue
            try:
                pairs = [(item.key, item.value) for item in status.values]
                fields = dict(pairs)
                if len(fields) != len(pairs) or fields["schema_version"] != "1":
                    raise ValueError("invalid schema")
                name, bus = fields["joint_name"], fields["bus"]
                motor_id, sequence = int(fields["motor_id"]), int(fields["feedback_sequence"])
                epoch, protocol = fields["producer_epoch"], fields["protocol"]
                age = float(fields["feedback_age_ms"])
                if not (
                    stamp > 0
                    and -1000 <= transport_age <= 5000
                    and math.isfinite(age)
                    and 0 <= age <= 5000
                ):
                    raise ValueError("stale feedback")
                if (
                    fields["valid"] != "true"
                    or name not in canonical_names
                    or not 0 <= motor_id <= 65535
                    or sequence < 1
                ):
                    raise ValueError("unknown or invalid motor")
                if not epoch or len(epoch) > 128 or len(bus) > 64 or protocol not in {"lro", "rp"}:
                    raise ValueError("invalid identity")
                bindings = self.profile.motors
                if not bindings or not any(
                    m.joint_name == name
                    and m.bus == bus
                    and m.motor_id == motor_id
                    and m.protocol == protocol
                    for m in bindings
                ):
                    raise ValueError("motor identity is not declared in profile")
                identity = (bus, motor_id)
                previous = self._last.get(identity)
                if previous and (
                    previous[0] == epoch and sequence <= previous[1] or stamp < previous[2]
                ):
                    raise ValueError("duplicate or reordered feedback")
                total_age = age + max(0, transport_age)
                if total_age > 5000:
                    raise ValueError("expired sample")
                index = canonical_names.index(name)
                values = {}
                for field in ("motor_temperature", "mos_temperature", "fault_code"):
                    if fields.get(field + "_valid") != "true":
                        continue
                    value = int(fields[field]) if field == "fault_code" else float(fields[field])
                    if (
                        not math.isfinite(value)
                        or field == "fault_code"
                        and not 0 <= value <= 2**32 - 1
                    ):
                        raise ValueError("invalid value")
                    values[f"joint.{index}.{field}"] = value
                if not values:
                    raise ValueError("no valid fields")
                self.accumulator.observe(
                    self.profile.motor_health_topic,
                    values,
                    datetime.fromtimestamp(stamp, timezone.utc),
                    event_age_ms=total_age,
                )
                self._last[identity] = (epoch, sequence, stamp, time.monotonic())
                accepted += 1
            except (KeyError, ValueError, TypeError, OverflowError):
                self.rejected += 1
        if accepted:
            self.accumulator.set_health_source(
                "motors",
                "live",
                f"已订阅运控同帧反馈；本次{accepted}台，累计拒绝{self.rejected}条；物理映射按设备档案核对",
            )
        return accepted
