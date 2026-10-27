"""Copy the install call into the existing backend only after review/migration.

Reference platform snapshot: sss836/rp1_test_shn
f44a50e76bc8c74c4f6c34885eb0974790cc173e. Not automatically executed.
"""

import os
from pathlib import Path

from fastapi import Depends, HTTPException

from rp1_monitor.backend import BackendSettings
from rp1_monitor.integration import mount_monitoring


def install(platform_app):
    # These imports belong to the existing platform, not this standalone project.
    from app.core.auth import get_request_actor

    def human_read(actor=Depends(get_request_actor)):
        if actor.principal_kind != "HUMAN":
            raise HTTPException(403, "整机监控只读页面需要人的平台登录")
        return actor

    return mount_monitoring(
        platform_app,
        BackendSettings(
            database_url=os.environ["RP1_MONITOR_DATABASE_URL"],
            device_config=Path(os.environ["RP1_MONITOR_DEVICES"]),
            initialize=False,
        ),
        read_dependency=human_read,
    )


# In app/main.py, after the host exception handlers are registered:
# monitor_child = install(app)
# mount_monitoring composes the host lifespan to start/stop the alert worker
# and dispose the monitoring pool; host lifespan state is preserved.
# Add a frontend link/iframe to /monitor/; no existing ingestion route is reopened.
