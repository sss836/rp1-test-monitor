"""C++ frame decode -> real ROS diagnostic structures -> Python observer -> central DB.

No ROS node, CAN device or real robot is created. All data are labelled simulated.
"""

import argparse
import hashlib
import json
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace as NS

from rp1_monitor.aggregation import Accumulator
from rp1_monitor.models import TelemetryBatch
from rp1_monitor.monitor_service import MonitorService
from rp1_monitor.motor_health import MotorHealthConsumer
from rp1_monitor.profiles import RobotProfile
from rp1_monitor.storage import Store

parser = argparse.ArgumentParser()
parser.add_argument("--output", type=Path, default=Path("docs/health-replay-verification.json"))
args = parser.parse_args()
root = Path(__file__).resolve().parents[1]
with tempfile.TemporaryDirectory(prefix="rp1-replay-") as temp:
    program = Path(temp) / "diagnostic-replay"
    includes = ["-I" + str(p) for p in Path("/opt/ros/humble/include").iterdir() if p.is_dir()]
    subprocess.run(
        [
            "g++",
            "-std=c++17",
            "-pthread",
            "-I" + str(root / "integrations/robot-health/include"),
            *includes,
            str(root / "integrations/robot-health/tests/diagnostic_replay.cpp"),
            "-o",
            str(program),
        ],
        check=True,
    )
    rows = [
        json.loads(line) for line in subprocess.check_output([str(program)], text=True).splitlines()
    ]
    profile = RobotProfile(
        robot_id="REPLAY-01",
        motors=[
            {"joint_name": "joint_0", "bus": "can0", "motor_id": 1, "protocol": "rp"},
            {"joint_name": "joint_1", "bus": "can1", "motor_id": 1, "protocol": "lro"},
        ],
    )
    acc = Accumulator(profile.robot_id, "simulated")
    acc.device_profile = profile.model_dump()
    acc.set_mapping(["joint_0", "joint_1"], "unverified", "合成CAN帧回放，非实机映射验收")
    consumer = MotorHealthConsumer(acc, profile)
    results = []
    for row in rows:
        stamp = datetime.now(timezone.utc).timestamp()
        status = NS(name=row["name"], values=[NS(**pair) for pair in row["values"]])
        message = NS(
            header=NS(stamp=NS(sec=int(stamp), nanosec=int(stamp % 1 * 1e9))), status=[status]
        )
        results.append(consumer.consume(message, ["joint_0", "joint_1"]))
    assert results == [0, 1, 0, 1], results
    frame = acc.preview()
    assert frame.values["joint.0.fault_code"] == 0xABCD
    assert frame.values["joint.0.motor_temperature"] == -1
    assert frame.values["joint.1.motor_temperature"] == -5
    window = acc.flush()
    store = Store("sqlite:///:memory:")
    store.configure(
        [
            {
                "robot_id": profile.robot_id,
                "name": "CAN回放模拟",
                "source_kind": "simulated",
                "token": "replay-only-" + "x" * 32,
            }
        ]
    )
    from rp1_monitor.storage import token_digest

    actor = store.device_for_digest(token_digest("replay-only-" + "x" * 32))
    store.ingest(TelemetryBatch(delivery="live", windows=[window]), actor)
    service = MonitorService(store)
    snapshot = service.snapshot(profile.robot_id)
    assert snapshot["health"]["motors"]["available"] == 2
    assert snapshot["health"]["motors"]["state"] == "fault"
    saved = service.record(profile.robot_id, ["joint.0.motor_temperature"])
    assert saved["report"]["validation_scope"] == "simulated"
    from rp1_monitor.diagnostics import canonical_bytes

    assert hashlib.sha256(canonical_bytes(saved["report"])).hexdigest() == saved["sha256"]
    output = {
        "scope": "synthetic_CAN_and_ROS_diagnostic_structure_replay_not_hardware",
        "accepted_sequence": results,
        "wide_fault_code": int(frame.values["joint.0.fault_code"]),
        "rp_temperature": frame.values["joint.0.motor_temperature"],
        "lro_temperature": frame.values["joint.1.motor_temperature"],
        "central_motor_coverage": snapshot["health"]["motors"]["available"],
        "central_fault_state": snapshot["health"]["motors"]["state"],
        "report_hash_verified": True,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(output, ensure_ascii=False))
    store.engine.dispose()
