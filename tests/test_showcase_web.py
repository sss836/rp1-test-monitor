"""Exercise presentation freshness and verify the offline structure asset contract."""

import hashlib
import json
import shutil
import struct
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(not shutil.which("node"), reason="Node exercises the browser state machine")
def test_showcase_never_restores_cached_live_values_after_disconnection():
    root = Path(__file__).parents[1]
    subprocess.run(
        ["node", str(root / "reviews/showcase-state-check.js")],
        check=True,
        capture_output=True,
        text=True,
    )


def test_structure_model_is_self_contained_and_not_a_recorded_robot_pose():
    root = Path(__file__).parents[1] / "src/rp1_monitor/web"
    payload = (root / "models/rp1-static-structure.glb").read_bytes()
    magic, version, length = struct.unpack_from("<4sII", payload)
    assert (magic, version, length) == (b"glTF", 2, len(payload))
    size, kind = struct.unpack_from("<I4s", payload, 12)
    assert kind == b"JSON"
    model = json.loads(payload[20 : 20 + size])
    assert not model.get("animations") and not model.get("skins")
    assert all(not item.get("uri") for item in model.get("buffers", []))
    assert all(not item.get("uri") for item in model.get("images", []))
    manifest = json.loads((root / "models/model-manifest.json").read_text())
    assert hashlib.sha256(payload).hexdigest() == manifest["sha256"]
    for name in ("THREE-LICENSE.txt", "SWIPER-LICENSE.txt", "MESHOPT-LICENSE.txt"):
        assert (root / "vendor" / name).stat().st_size > 0
