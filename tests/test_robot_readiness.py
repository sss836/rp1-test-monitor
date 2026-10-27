"""Preparation checks must be harmless and must not claim robot acceptance."""

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts/check_robot_readiness.py"
spec = importlib.util.spec_from_file_location("robot_readiness", SCRIPT)
readiness = importlib.util.module_from_spec(spec)
spec.loader.exec_module(readiness)


def test_os_release_only_includes_selected_fields(tmp_path):
    release = tmp_path / "os-release"
    release.write_text('ID=ubuntu\nVERSION_ID="22.04"\nSECRET=must-not-return\n')
    assert readiness.os_description(release) == {"id": "ubuntu", "version_id": "22.04"}


def test_failed_import_and_timeout_do_not_expose_details(monkeypatch):
    monkeypatch.setattr(readiness.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=1))
    assert readiness.probe_module("rclpy") == "needs_check"

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], 5, stderr="must-not-return")

    monkeypatch.setattr(readiness.subprocess, "run", timeout)
    assert readiness.probe_module("rclpy") == "needs_check"


def test_import_success_does_not_mean_robot_ready_or_create_state(tmp_path, monkeypatch):
    monkeypatch.setattr(readiness, "probe_module", lambda name: "importable")
    monkeypatch.setattr(readiness.sys, "version_info", (3, 10, 0))
    monkeypatch.setattr(readiness.sys, "platform", "linux")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "must-not-return")
    monkeypatch.setenv("ROS_DISTRO", "invalid\nsecret")
    state = tmp_path / "not-created" / "collector"
    report = readiness.collect(state)
    assert report["environment_status"] == "imports_ready"
    assert report["scope"] == "host_environment_preparation_only_not_robot_acceptance"
    assert report["still_requires_on_site_check"]
    assert report["system"]["ros_distro_in_current_shell"] == "not_detected"
    assert not state.parent.exists()
    assert report["disk"]["write_test_performed"] is False
    assert "must-not-return" not in json.dumps(report)


def test_old_python_is_identified_without_advising_automatic_upgrade(tmp_path, monkeypatch):
    monkeypatch.setattr(readiness, "probe_module", lambda name: "importable")
    monkeypatch.setattr(readiness.sys, "version_info", (3, 8, 0))
    report = readiness.collect(tmp_path)
    assert report["environment_status"] == "needs_check"
    assert report["system"]["python_meets_project_minimum"] is False


@pytest.mark.parametrize("machine_readable", [False, True])
def test_script_runs_with_standard_library_and_leaves_target_untouched(tmp_path, machine_readable):
    env = {**os.environ, "DEEPSEEK_API_KEY": "must-not-return"}
    state = tmp_path / "not-created"
    command = [sys.executable, "-I", "-S", "-B", str(SCRIPT), "--state-dir", str(state)]
    if machine_readable:
        command.append("--json")
    completed = subprocess.run(
        command, env=env, capture_output=True, text=True, timeout=60, check=True
    )
    assert "must-not-return" not in completed.stdout + completed.stderr
    assert not state.exists()
    if machine_readable:
        report = json.loads(completed.stdout)
        assert report["scope"] == "host_environment_preparation_only_not_robot_acceptance"
    else:
        assert "不构成实机验收" in completed.stdout
