"""Run the shipped component controller against delayed read-only responses."""

import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(not shutil.which("node"), reason="Node exercises real component pages")
def test_component_pages_isolate_devices_ranges_joints_and_late_queries():
    root = Path(__file__).parents[1]
    subprocess.run(
        ["node", str(root / "reviews/components-state-check.js")],
        check=True,
        capture_output=True,
        text=True,
    )
