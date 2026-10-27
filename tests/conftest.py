import json
import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from rp1_monitor.backend import BackendSettings, create_app
from rp1_monitor.models import DataQuality, MetricRollup, TelemetryWindow
from rp1_monitor.storage import Store, metadata

DEVICE_TOKEN = "test-device-key-" + "a" * 32
VIEWER_TOKEN = "test-viewer-key-" + "b" * 32


@pytest.fixture
def window():
    stream = uuid4()

    def build(sequence=1, stream_id=None, offset=-0.1, **changes):
        end = datetime.now(timezone.utc) + timedelta(seconds=offset)
        value = TelemetryWindow(
            robot_id="RP1-TEST",
            stream_id=stream_id or stream,
            sequence=sequence,
            source_kind="simulated",
            window_start=end - timedelta(seconds=1),
            window_end=end,
            metrics={
                "joint.0.position": MetricRollup(
                    last=2, minimum=1, maximum=99, count=50, age_ms=10
                ),
                "joint.0.policy_target": MetricRollup(
                    last=2.1, minimum=1, maximum=3, count=50, age_ms=10
                ),
                "imu.quaternion.w": MetricRollup(last=1, count=50, age_ms=10),
            },
            quality=DataQuality(
                topic_counts={"/joint_states": 50, "/action": 50, "/imu": 50},
                topic_ages_ms={"/joint_states": 10, "/action": 10, "/imu": 10},
            ),
        )
        return TelemetryWindow.model_validate({**value.model_dump(mode="json"), **changes})

    return build


DATABASES = ["sqlite:///:memory:"]
if os.environ.get("RP1_MONITOR_TEST_DATABASE_URL"):
    DATABASES.append(os.environ["RP1_MONITOR_TEST_DATABASE_URL"])


def require_disposable_postgres(url):
    from sqlalchemy.engine import make_url

    parsed = make_url(url)
    if parsed.host != "127.0.0.1" or not (parsed.database or "").startswith("rp1_monitor_test_"):
        pytest.fail("PostgreSQL测试仅允许回环地址和专用随机测试库")
    return url


@pytest.fixture(params=DATABASES, ids=["sqlite"] + (["postgres"] if len(DATABASES) > 1 else []))
def client(tmp_path, request):
    url = request.param
    if not url.startswith("sqlite"):
        require_disposable_postgres(url)
        temporary = Store(url, initialize=False)
        metadata.drop_all(temporary.engine)
        temporary.engine.dispose()
    path = tmp_path / "devices.json"
    path.write_text(
        json.dumps(
            [
                {
                    "robot_id": "RP1-TEST",
                    "name": "测试设备",
                    "source_kind": "simulated",
                    "token": DEVICE_TOKEN,
                }
            ]
        )
    )
    settings = BackendSettings(url, path, VIEWER_TOKEN)
    with TestClient(create_app(settings)) as connection:
        yield connection
    connection.app.state.store.engine.dispose()


@pytest.fixture
def write_headers():
    return {"Authorization": f"Bearer {DEVICE_TOKEN}"}


@pytest.fixture
def read_headers():
    return {"Authorization": f"Bearer {VIEWER_TOKEN}"}
