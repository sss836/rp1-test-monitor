"""Synthetic protocol and command-boundary checks; no hardware acceptance claims."""

import json
import socket
import stat
import struct
import subprocess
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/check_rp1_hardware.sh"


@pytest.fixture
def checker():
    source = SCRIPT.read_text().split("<<'RP1_CHECK_PY'\n", 1)[1].rsplit("\nRP1_CHECK_PY", 1)[0]
    module = types.ModuleType("rp1_hardware_check")
    exec(compile(source, str(SCRIPT), "exec"), module.__dict__)
    return module


def robot_config(count=24):
    return {
        "motors": {
            "motor_interface": ["can0"],
            "motor_interface_type": ["canfd"],
            "motor_num": [count],
            "motor_type": ["LRO"],
            "motor_id": list(range(1, count + 1)),
            "motor_model": [1] * count,
        },
        "imu": {
            "imu_type": "HIPNUC",
            "imu_interface_type": "can",
            "imu_interface": "can_hipnuc",
            "imu_id": 8,
            "baudrate": 1000000,
        },
    }


def options(**kwargs):
    values = dict(
        robot="rp1_24dof",
        timeout=0.1,
        seconds=1,
        imu_min_hz=50,
        bms_max_age=30,
        versions=False,
        verbose=False,
    )
    values.update(kwargs)
    return SimpleNamespace(**values)


def journal(message, timestamp=999):
    return json.dumps({"MESSAGE": message, "__REALTIME_TIMESTAMP": str(int(timestamp * 1000000))})


def bms_commands(checker, monkeypatch, logs, state="active"):
    calls = []

    def run(*argv):
        calls.append(argv)
        if argv == ("systemctl", "show", "bms.service", "--property=ActiveState", "--value"):
            return state
        assert argv[:4] == ("journalctl", "-b", "-u", "bms.service")
        assert "--since" in argv and "json" in argv
        return logs

    monkeypatch.setattr(checker, "command", run)
    monkeypatch.setattr(checker, "time", SimpleNamespace(time=lambda: 1000))
    return calls


@pytest.mark.parametrize(
    "message",
    [
        "[BMS Data] Voltage: 48.6V | Current: -2.35A | SoC: 86.5%",
        "[CAN BMS Data] Voltage: +51.7V | Current: 0A | SoC: 80%",
        "[NRF PMIC] NRF v2 | 48V=ON 5V=ON | BMS 51.7V -1.25A 84.5%",
    ],
)
def test_bms_recognizes_real_formats_and_reads_only(checker, monkeypatch, message):
    calls = bms_commands(checker, monkeypatch, journal(message))
    assert checker.check_bms(options())[0] == "PASS"
    assert len(calls) == 2


@pytest.mark.parametrize(
    "message,timestamp,expected",
    [
        ("Connected to BMS", 999, "UNKNOWN"),
        ("[NRF PMIC] NRF v2 | 48V=ON 5V=ON", 999, "UNKNOWN"),
        ("[BMS Data] Voltage: 48V", 900, "UNKNOWN"),
        ("[BMS Data] Voltage: 48V", 1001, "UNKNOWN"),
        ("[BMS Data] Voltage: 0V", 999, "FAIL"),
        ("[BMS Data] Voltage: -48V", 999, "FAIL"),
    ],
)
def test_bms_does_not_pass_stale_connection_or_rail_labels(
    checker, monkeypatch, message, timestamp, expected
):
    bms_commands(checker, monkeypatch, journal(message, timestamp))
    assert checker.check_bms(options())[0] == expected


@pytest.mark.parametrize("value", ["0", "nan", "inf", "-inf"])
def test_bms_newest_invalid_sample_overrides_old_good_sample(checker, monkeypatch, value):
    logs = "\n".join(
        [journal(f"[BMS Data] Voltage: {value}V", 999), journal("[BMS Data] Voltage: 48V", 998)]
    )
    bms_commands(checker, monkeypatch, logs)
    assert checker.check_bms(options())[0] == "FAIL"


def test_bms_inactive_does_not_restart_service(checker, monkeypatch):
    calls = bms_commands(checker, monkeypatch, "", state="inactive")
    assert checker.check_bms(options())[0] == "FAIL"
    assert len(calls) == 1


def test_led_ready_cannot_prove_light_output(checker, monkeypatch):
    calls = []

    def command(*args):
        calls.append(args)
        return "install ok installed" if args[0] == "dpkg-query" else ""

    monkeypatch.setattr(checker, "command", command)
    monkeypatch.setattr(checker.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(
        checker,
        "Path",
        lambda _: SimpleNamespace(
            exists=lambda: True, stat=lambda: SimpleNamespace(st_mode=stat.S_IFCHR)
        ),
    )
    status, detail = checker.check_led()
    assert status == "UNKNOWN" and "待现场确认" in detail
    assert "RGB 80 80 80" in detail and "保持亮灯" in detail
    assert calls == [
        ("dpkg-query", "-W", "-f=${Status}", "robopi-addon"),
        ("robopi-ws2812", "solid", "80", "80", "80"),
    ]


def test_led_missing_device_fails(checker, monkeypatch):
    calls = []
    monkeypatch.setattr(
        checker, "command", lambda *args: calls.append(args) or "install ok installed"
    )
    monkeypatch.setattr(checker.shutil, "which", lambda _: "/usr/bin/robopi-ws2812")
    monkeypatch.setattr(checker, "Path", lambda _: SimpleNamespace(exists=lambda: False))
    assert checker.check_led()[0] == "FAIL"
    assert all(call[0] != "robopi-ws2812" for call in calls)


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("write failed"),
        PermissionError("denied"),
        subprocess.TimeoutExpired("robopi-ws2812", 10),
    ],
)
def test_led_command_error_is_not_reported_as_success(checker, monkeypatch, error, capsys):
    calls = []

    def command(*args):
        calls.append(args)
        if args[0] == "dpkg-query":
            return "install ok installed"
        raise error

    monkeypatch.setattr(checker, "command", command)
    monkeypatch.setattr(checker.shutil, "which", lambda _: "/usr/bin/robopi-ws2812")
    monkeypatch.setattr(
        checker,
        "Path",
        lambda _: SimpleNamespace(
            exists=lambda: True, stat=lambda: SimpleNamespace(st_mode=stat.S_IFCHR)
        ),
    )
    status, detail = checker.check_led()
    assert status == "FAIL" and "命令失败或未确认" in detail
    assert "命令返回成功" not in capsys.readouterr().out
    assert calls[-1] == ("robopi-ws2812", "solid", "80", "80", "80")
    assert len(calls) == 2


@pytest.mark.parametrize("count", [24, 28])
def test_complete_motor_configs(checker, count):
    assert len(checker.motor_targets(robot_config(count), count)) == count


@pytest.mark.parametrize(
    "field,value",
    [
        ("motor_num", [-24]),
        ("motor_num", [23]),
        ("motor_model", [9] * 24),
        ("motor_id", [1] * 24),
        ("motor_id", [0] + list(range(2, 25))),
        ("motor_id", [True] + list(range(2, 25))),
        ("motor_type", ["DM"]),
        ("motor_interface_type", []),
    ],
)
def test_invalid_motor_config_is_rejected_before_any_query(checker, monkeypatch, field, value):
    config = robot_config()
    config["motors"][field] = value
    monkeypatch.setattr(
        checker, "query_motor", lambda *args: pytest.fail("invalid config sent a query")
    )
    with pytest.raises(ValueError):
        checker.check_motors(config, options())


class Bus:
    def __init__(self):
        self.sent = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def settimeout(self, value):
        assert value > 0

    def send(self, frame):
        self.sent.append(frame)
        return len(frame)


@pytest.mark.parametrize("kind,size,flags", [("can", 16, 0), ("canfd", 72, 1)])
@pytest.mark.parametrize("code", [0x17, 0x18, 0x19, 0x1A, 0x1B, 0x1E, 0x1F])
def test_motor_wire_query_is_only_e0_and_matches_response(
    checker, monkeypatch, kind, size, flags, code
):
    bus = Bus()
    filters = []
    monkeypatch.setattr(
        checker, "open_can", lambda iface, rules, fd=False: filters.extend(rules) or bus
    )
    good = bytes([0xA0, code, 0, 0, 1, 244])
    frames = iter(
        [
            (2, good),
            (1 | checker.EFF, good),
            (1, bytes([0, code, 0, 0, 1, 244])),
            (1, bytes([0xA0, 0, 0, 0, 1, 244])),
            (1, good),
        ]
    )
    monkeypatch.setattr(checker, "receive", lambda *args: next(frames))
    assert checker.query_motor("can0", kind, 1, code, 0.1) == good
    frame = bus.sent[0]
    assert len(bus.sent) == 1 and len(frame) == size
    assert struct.unpack_from("=IBB", frame) == (1, 2, flags)
    assert frame[8:10] == bytes([0xE0, code])
    assert not any(frame[10:])
    assert filters == [(1, 0xE00007FF)]


def test_motor_rejects_write_opcode_before_opening_socket(checker, monkeypatch):
    monkeypatch.setattr(checker, "open_can", lambda *args: pytest.fail("non-query opened bus"))
    with pytest.raises(ValueError):
        checker.query_motor("can0", "canfd", 1, 0x05, 0.1)


def test_version_capture_keeps_all_matching_frames_in_window(checker, monkeypatch):
    bus = Bus()
    monkeypatch.setattr(checker, "open_can", lambda *args, **kwargs: bus)
    first = bytes.fromhex("A01E010203040506")
    second = bytes.fromhex("A01E0708090A0B0C")
    frames = iter([(1, first), (2, first), (1, bytes.fromhex("A017000001F4")), (1, second), None])
    monkeypatch.setattr(checker, "receive", lambda *args: next(frames))
    result = checker.query_motor("can0", "canfd", 1, 0x1E, 0.1, collect=True)
    assert [payload for age, payload in result] == [first, second]
    assert all(0 <= age <= 0.1 for age, payload in result)
    assert len(bus.sent) == 1 and bus.sent[0][8:10] == bytes.fromhex("E01E")


@pytest.mark.parametrize("count", [24, 28])
@pytest.mark.parametrize("verbose", [False, True])
def test_motor_version_display_is_compact_and_decoded(checker, monkeypatch, capsys, count, verbose):
    monkeypatch.setattr(checker, "check_link", lambda *args, **kwargs: None)
    calls = []

    def query(iface, kind, motor_id, code, timeout, collect=False):
        calls.append(code)
        if code == 0x1E:
            assert collect
            return [(0.01, bytes.fromhex("A01E0017FE8002FF010003C8"))]
        return MODEL_60_RESPONSES[code]

    monkeypatch.setattr(checker, "query_motor", query)
    assert (
        checker.check_motors(
            robot_config(count),
            options(versions=True, verbose=verbose, robot="rp1_24dof" if count == 24 else "rp1"),
        )[0]
        == "PASS"
    )
    output = capsys.readouterr().out
    lines = [line for line in output.splitlines() if line.startswith("  [")]
    assert len(lines) == count
    assert all(
        "参数 6/6；版本=READ FW=0.23.254 HW=128.2.255 PROTO=1.0.3.200" in line for line in lines
    )
    assert ("raw=A01E0017FE8002FF010003C8" in output) == verbose
    assert "未校验允许版本" in output
    assert calls.count(0x1E) == count and len(calls) == count * 7


def test_motor_version_fields_are_unsigned_components(checker):
    assert checker.decode_motor_version(bytes.fromhex("A01E0017FE8002FF010003C8")) == (
        (0, 23, 254),
        (128, 2, 255),
        (1, 0, 3, 200),
    )


def test_zero_motor_version_is_decodable_not_a_bms_placeholder(checker):
    summary, details = checker.describe_motor_versions(
        [(0.01, bytes.fromhex("A01E") + bytes(10))], False
    )
    assert summary == "；版本=READ FW=0.0.0 HW=0.0.0 PROTO=0.0.0.0"
    assert details == []


@pytest.mark.parametrize("length", [0, 1, 2, 3, 8, 11, 13, 14, 16])
def test_motor_version_requires_exactly_twelve_bytes(checker, length):
    payload = (bytes.fromhex("A01E0017FE8002FF010003C8") + bytes(64))[:length]
    with pytest.raises(ValueError, match="应为12字节"):
        checker.decode_motor_version(payload)


@pytest.mark.parametrize("header", ["801E", "C01E", "A017", "A11E", "AF1E", "BF1E"])
def test_motor_version_rejects_wrong_type_code_or_error_flags(checker, header):
    with pytest.raises(ValueError):
        checker.decode_motor_version(bytes.fromhex(header) + bytes(10))


@pytest.mark.parametrize(
    "kind", ["missing", "duplicate", "conflict", "short", "mixed", "motor_error"]
)
def test_motor_version_ambiguous_replies_remain_unknown_with_raw(checker, kind):
    good = bytes.fromhex("A01E01020301000203000100")
    replies = [(0.01, good)]
    if kind == "missing":
        replies = []
    elif kind == "duplicate":
        replies.append((0.02, good))
    elif kind == "conflict":
        replies.append((0.02, bytes.fromhex("A01E01020401000203000100")))
    elif kind == "short":
        replies = [
            (0.01, bytes.fromhex("A01E010203040506")),
            (0.02, bytes.fromhex("A01E0708090A0B0C")),
        ]
    elif kind == "mixed":
        replies.append((0.02, bytes.fromhex("A01E")))
    elif kind == "motor_error":
        replies.append((0.02, bytes.fromhex("A11E01020301000203000100")))
    summary, details = checker.describe_motor_versions(replies, False)
    if kind == "duplicate":
        assert "版本=READ FW=1.2.3 HW=1.0.2 PROTO=3.0.1.0" in summary
        assert details == []
    else:
        assert "版本=UNKNOWN" in summary and "FW=" not in summary
        assert len(details) == len(replies)
        assert all(payload.hex().upper() in line for (_, payload), line in zip(replies, details))
    if kind == "motor_error":
        assert "错误码0x01" in summary
    if kind == "conflict":
        assert "不一致" in summary


@pytest.mark.parametrize("reply_mode", ["missing", "malformed", "error"])
def test_missing_version_does_not_skip_parameters_or_other_motors(
    checker, monkeypatch, capsys, reply_mode
):
    calls = []
    monkeypatch.setattr(checker, "check_link", lambda *args, **kwargs: None)

    def query(iface, kind, motor_id, code, timeout, collect=False):
        calls.append((motor_id, code))
        if code == 0x1E:
            if reply_mode == "error":
                raise OSError("receive failed")
            return [] if reply_mode == "missing" else [(0.01, bytes.fromhex("A01E"))]
        return MODEL_60_RESPONSES[code]

    monkeypatch.setattr(checker, "query_motor", query)
    status, detail = checker.check_motors(robot_config(), options(versions=True))
    assert status == "PASS" and "144/144" in detail
    assert len(calls) == 24 * 7
    assert capsys.readouterr().out.count("版本=UNKNOWN") == 24


@pytest.mark.parametrize("version", ["0x1234", "0x0"])
def test_bms_versions_use_current_invocation_and_keep_placeholder_unknown(
    checker, monkeypatch, capsys, version
):
    invocation = "a" * 32
    calls = []

    def run(*argv):
        calls.append(argv)
        if argv[0] == "dpkg-query":
            return "install ok installed\t1.2.3"
        if argv[0] == "systemctl":
            return invocation
        assert argv[0] == "journalctl"
        assert f"_SYSTEMD_INVOCATION_ID={invocation}" in argv
        assert "--grep=FW Version:" in argv
        return journal(f" > FW Version: {version} | HW Version: {version}")

    monkeypatch.setattr(checker, "command", run)
    checker.show_versions()
    output = capsys.readouterr().out
    assert "板卡软件包 roboparty-bms: 1.2.3（非设备固件版本）" in output
    if version == "0x0":
        assert "UNKNOWN BMS设备FW: 0x0" in output and "占位值" in output
    else:
        assert "BMS设备FW原始版本码: 0x1234" in output
        assert "不推断语义版本号" in output
    assert len(calls) == 7
    assert all("restart" not in call and "start" not in call for call in calls)


def test_version_report_does_not_search_old_bms_runs(checker, monkeypatch, capsys):
    calls = []

    def run(*argv):
        calls.append(argv)
        if argv[0] == "dpkg-query":
            return "install ok installed\t1.2.3"
        assert argv[0] == "systemctl"
        return ""

    monkeypatch.setattr(checker, "command", run)
    checker.show_versions()
    assert "当前无有效 InvocationID" in capsys.readouterr().out
    assert len(calls) == 6


# Independent, literal model-60 responses; no call to implementation baseline builders.
MODEL_60_RESPONSES = {
    0x17: bytes.fromhex("A017000001F4"),
    0x18: bytes.fromhex("A01800000032"),
    0x19: bytes.fromhex("A019FB1E04E2"),
    0x1A: bytes.fromhex("A01AF773088D"),
    0x1B: bytes.fromhex("A01BFE9F0161"),
    0x1F: bytes.fromhex("A01F0000"),
}


@pytest.mark.parametrize("failure", [None, "mismatch", "timeout", "oserror", "motor_error"])
def test_motor_read_must_compare_all_six_values(checker, monkeypatch, failure, capsys):
    calls = []
    monkeypatch.setattr(checker, "check_link", lambda *args, **kwargs: None)

    def query(iface, kind, motor_id, code, timeout):
        calls.append((motor_id, code))
        if motor_id == 1 and code == 0x1F:
            if failure == "mismatch":
                return bytes.fromhex("A01F0001")
            if failure == "timeout":
                return None
            if failure == "oserror":
                raise OSError("bus error")
            if failure == "motor_error":
                return bytes.fromhex("A11F0000")
        return MODEL_60_RESPONSES[code]

    monkeypatch.setattr(checker, "query_motor", query)
    status, detail = checker.check_motors(robot_config(), options())
    assert status == ("FAIL" if failure else "PASS")
    assert len(calls) == 24 * 6
    assert ("143/144" if failure else "144/144") in detail
    output = capsys.readouterr().out
    assert len([line for line in output.splitlines() if line.startswith("  [")]) == 24
    assert output.count("实际=") == (1 if failure == "mismatch" else 0)
    if failure == "mismatch":
        assert "FAIL CAN_TIMEOUT: 实际=[0.0, 1.0] ms" in output
    elif failure:
        assert "FAIL CAN_TIMEOUT:" in output
    if failure == "motor_error":
        assert "电机报告错误码0x01" in output and "raw=A11F0000" in output


def test_motor_verbose_shows_values_without_changing_queries(checker, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(checker, "check_link", lambda *args, **kwargs: None)

    def query(iface, kind, motor_id, code, timeout):
        calls.append(code)
        return MODEL_60_RESPONSES[code]

    monkeypatch.setattr(checker, "query_motor", query)
    assert checker.check_motors(robot_config(), options(verbose=True))[0] == "PASS"
    assert len(calls) == 144
    assert capsys.readouterr().out.count("实际=") == 144


@pytest.mark.parametrize("bad", ["down", "busoff", "wrong_bitrate", "not_fd", "vcan"])
def test_link_preflight_rejects_unusable_or_virtual_can(checker, monkeypatch, bad):
    link = {
        "flags": ["UP"],
        "mtu": 72,
        "linkinfo": {
            "info_kind": "can",
            "info_data": {"state": "ERROR-ACTIVE", "bittiming": {"bitrate": 1000000}},
        },
    }
    if bad == "down":
        link["flags"] = []
    elif bad == "busoff":
        link["linkinfo"]["info_data"]["state"] = "BUS-OFF"
    elif bad == "wrong_bitrate":
        link["linkinfo"]["info_data"]["bittiming"]["bitrate"] = 500000
    elif bad == "not_fd":
        link["mtu"] = 16
    else:
        link["linkinfo"]["info_kind"] = "vcan"
    monkeypatch.setattr(checker, "command", lambda *args: json.dumps([link]))
    with pytest.raises(RuntimeError):
        checker.check_link("can0", fd=True, bitrate=1000000)


@pytest.mark.parametrize(
    "fault", [None, "other_id", "missing_gyro", "zero_quat", "slow", "burst", "short", "standard"]
)
def test_imu_needs_both_correct_streams_valid_quaternion_and_sustained_rate(
    checker, monkeypatch, fault
):
    clock = [0.0]
    frames = []
    times = [index / 100 for index in range(1, 100)]
    if fault == "slow":
        times = [0.1, 0.5, 0.9]
    if fault == "burst":
        times = [index / 1000 for index in range(1, 100)]
    for timestamp in times:
        for pgn in (0xFF46, 0xFF37):
            if fault == "missing_gyro" and pgn == 0xFF37:
                continue
            can_id = checker.EFF | (pgn << 8) | (9 if fault == "other_id" else 8)
            if fault == "standard":
                can_id &= ~checker.EFF
            data = struct.pack("<4h", 10000 if pgn == 0xFF46 else 0, 0, 0, 0)
            if fault == "zero_quat":
                data = bytes(8)
            if fault == "short":
                data = bytes(4)
            frames.append((timestamp, (can_id, data)))
    iterator = iter(frames)

    def receive(*args):
        item = next(iterator, None)
        if item is None:
            clock[0] = 1
            return None
        clock[0], frame = item
        return frame

    bus = Bus()
    monkeypatch.setattr(checker, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(checker, "check_link", lambda *args, **kwargs: None)
    monkeypatch.setattr(checker, "receive", receive)
    monkeypatch.setattr(checker, "open_can", lambda *args, **kwargs: bus)
    assert checker.check_imu(robot_config(), options())[0] == ("PASS" if fault is None else "FAIL")
    assert bus.sent == []


def test_receive_uses_dlc_not_padding_and_has_a_deadline(checker):
    bus = Bus()
    bus.recv = lambda _: struct.pack("=IBB2x64s", 1, 4, 0, bytes.fromhex("A01F0000"))
    assert checker.receive(bus, checker.time.monotonic() + 1) == (1, bytes.fromhex("A01F0000"))
    assert checker.receive(bus, checker.time.monotonic() - 1) is None
    bus.recv = lambda _: (_ for _ in ()).throw(socket.timeout())
    assert checker.receive(bus, checker.time.monotonic() + 1) is None


def test_main_continues_after_one_failure_and_reports_unknown(checker, monkeypatch, tmp_path):
    config = tmp_path / "robot.yaml"
    config.write_text("unused")
    monkeypatch.setattr(checker.sys, "argv", ["check", "--config", str(config)])
    monkeypatch.setattr(checker, "load_config", lambda _: robot_config())
    monkeypatch.setattr(
        checker, "check_bms", lambda _: (_ for _ in ()).throw(PermissionError("denied"))
    )
    monkeypatch.setattr(checker, "check_led", lambda: ("UNKNOWN", "physical output unconfirmed"))
    seen = []
    monkeypatch.setattr(
        checker, "check_imu", lambda *args: seen.append("imu") or ("PASS", "synthetic")
    )
    monkeypatch.setattr(
        checker, "check_motors", lambda *args: seen.append("motor") or ("PASS", "synthetic")
    )
    assert checker.main() == 1
    assert seen == ["imu", "motor"]
    monkeypatch.setattr(checker, "check_bms", lambda _: ("PASS", "synthetic"))
    assert checker.main() == 2


def test_bash_entrypoint_help_and_rejects_write_mode():
    result = subprocess.run(
        ["bash", str(SCRIPT), "--help"], capture_output=True, text=True, timeout=5
    )
    assert result.returncode == 0 and "--robot" in result.stdout
    result = subprocess.run(
        ["bash", str(SCRIPT), "--mode", "write"], capture_output=True, text=True, timeout=5
    )
    assert result.returncode == 2 and "unrecognized arguments" in result.stderr
