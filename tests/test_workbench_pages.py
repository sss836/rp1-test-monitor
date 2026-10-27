"""Exercise the shipped router, History API, and diagnostic conversation together."""

import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(
    not shutil.which("node"), reason="Node exercises the browser page state machine"
)
def test_robot_pages_keep_navigation_device_and_conversation_context_aligned():
    script = Path(__file__).with_name("workbench_pages_check.js")
    result = subprocess.run(["node", str(script)], check=True, capture_output=True, text=True)
    assert "PASS: robot workspace navigation and conversation isolation" in result.stdout
