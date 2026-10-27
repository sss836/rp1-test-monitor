"""Exercise real-robot empty states without a robot or a writable backend."""

import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(not shutil.which("node"), reason="Node exercises the shipped web renderer")
def test_real_robot_roster_transport_age_and_missing_fields():
    root = Path(__file__).parents[1]
    subprocess.run(
        ["node", str(root / "reviews/real-robot-web-check.js")],
        check=True,
        capture_output=True,
        text=True,
    )
