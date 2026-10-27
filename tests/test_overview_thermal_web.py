"""Verify that overview tabs reuse the real live heat map and preserve its home."""

import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(not shutil.which("node"), reason="Node exercises real overview tabs")
def test_overview_thermal_reuses_live_panel_and_supports_keyboard_navigation():
    root = Path(__file__).parents[1]
    subprocess.run(
        ["node", str(root / "reviews/overview-thermal-check.js")],
        check=True,
        capture_output=True,
        text=True,
    )
