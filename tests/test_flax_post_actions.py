# tests/test_flax_post_actions.py
"""flax_post.actions — power + chassis-identify action logic (pure, unit-testable).

The subprocess helpers (powertriage/identtriage) are external shell tools; tests
inject a fake runner and never shell out for real."""
import subprocess

import pytest

from flax_post import actions


# --- flash_active ------------------------------------------------------

def test_flash_active_true_when_bmc_flashing():
    record = {"fw": {"bmc": {"phase": "flashing"}, "bios": {}, "nic": {}}}
    assert actions.flash_active(record) is True


def test_flash_active_true_when_bios_checking():
    record = {"fw": {"bmc": {}, "bios": {"phase": "checking"}, "nic": {}}}
    assert actions.flash_active(record) is True


def test_flash_active_true_when_nic_activating():
    record = {"fw": {"bmc": {}, "bios": {}, "nic": {"phase": "activating"}}}
    assert actions.flash_active(record) is True


def test_flash_active_true_when_monitoring():
    record = {"fw": {"bmc": {"phase": "monitoring"}, "bios": {}, "nic": {}}}
    assert actions.flash_active(record) is True


def test_flash_active_false_when_all_up_to_date():
    record = {"fw": {"bmc": {"phase": "up_to_date"}, "bios": {"phase": "done"},
                     "nic": {"phase": "up_to_date"}}}
    assert actions.flash_active(record) is False


def test_flash_active_null_safe_missing_fw_key():
    assert actions.flash_active({}) is False


def test_flash_active_null_safe_missing_phase():
    record = {"fw": {"bmc": {}, "bios": {}, "nic": {}}}
    assert actions.flash_active(record) is False


def test_flash_active_null_safe_missing_slice():
    record = {"fw": {"bmc": {"phase": "flashing"}}}   # bios/nic absent entirely
    assert actions.flash_active(record) is True


# --- run_power -----------------------------------------------------------

def test_run_power_off_blocked_does_not_call_runner():
    calls = []

    def fake_runner(argv, timeout):
        calls.append(argv)
        return (0, "should not run")

    result = actions.run_power("10.0.0.5", "off", blocked=True, runner=fake_runner)
    assert result == {"ok": False, "blocked": True, "reason": "firmware flash in progress"}
    assert calls == []


def test_run_power_on_happy_path_calls_runner_with_exact_argv():
    calls = []

    def fake_runner(argv, timeout):
        calls.append((argv, timeout))
        return (0, "chassis power on")

    result = actions.run_power("10.0.0.5", "on", blocked=False, runner=fake_runner)
    assert calls == [(["powertriage", "10.0.0.5", "on"], 120)]
    assert result == {"ok": True, "action": "on", "ip": "10.0.0.5", "output": "chassis power on"}


def test_run_power_off_not_blocked_calls_runner():
    calls = []

    def fake_runner(argv, timeout):
        calls.append(argv)
        return (0, "chassis power off")

    result = actions.run_power("10.0.0.5", "off", blocked=False, runner=fake_runner)
    assert calls == [["powertriage", "10.0.0.5", "off"]]
    assert result["ok"] is True and result["action"] == "off"


def test_run_power_nonzero_rc_maps_to_ok_false():
    def fake_runner(argv, timeout):
        return (1, "error: unreachable")

    result = actions.run_power("10.0.0.5", "cycle", blocked=False, runner=fake_runner)
    assert result == {"ok": False, "action": "cycle", "ip": "10.0.0.5", "output": "error: unreachable"}


def test_run_power_invalid_action_raises():
    with pytest.raises(ValueError):
        actions.run_power("10.0.0.5", "reboot-hard", blocked=False, runner=lambda argv, timeout: (0, ""))


# --- run_identify ----------------------------------------------------------

def test_run_identify_calls_runner_with_exact_argv():
    calls = []

    def fake_runner(argv, timeout):
        calls.append((argv, timeout))
        return (0, "identify on")

    result = actions.run_identify("10.0.0.5", "on", runner=fake_runner)
    assert calls == [(["identtriage", "10.0.0.5", "on"], 60)]
    assert result == {"ok": True, "mode": "on", "ip": "10.0.0.5", "output": "identify on"}


def test_run_identify_nonzero_rc_maps_to_ok_false():
    def fake_runner(argv, timeout):
        return (1, "error")

    result = actions.run_identify("10.0.0.5", "force", runner=fake_runner)
    assert result == {"ok": False, "mode": "force", "ip": "10.0.0.5", "output": "error"}


def test_run_identify_invalid_mode_raises():
    with pytest.raises(ValueError):
        actions.run_identify("10.0.0.5", "blink", runner=lambda argv, timeout: (0, ""))


# --- bmc_ip_for_port -------------------------------------------------------

def test_bmc_ip_for_port_hit():
    slots = [{"port": "et6b1", "bmc_ip": "10.0.0.5"}, {"port": "et6b2", "bmc_ip": "10.0.0.6"}]
    assert actions.bmc_ip_for_port("et6b2", slots) == "10.0.0.6"


def test_bmc_ip_for_port_miss_unknown_port():
    slots = [{"port": "et6b1", "bmc_ip": "10.0.0.5"}]
    assert actions.bmc_ip_for_port("et9z9", slots) is None


def test_bmc_ip_for_port_miss_no_bmc_ip():
    slots = [{"port": "et6b1", "bmc_ip": None}]
    assert actions.bmc_ip_for_port("et6b1", slots) is None


# --- _default_runner: timeout / missing binary -----------------------------

def test_default_runner_timeout_returns_124(monkeypatch):
    def fake_run(argv, capture_output, text, timeout):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=timeout)

    monkeypatch.setattr(actions.subprocess, "run", fake_run)
    rc, out = actions._default_runner(["powertriage", "10.0.0.5", "on"], timeout=5)
    assert rc == 124
    assert out == "timeout"


def test_default_runner_missing_binary_returns_127(monkeypatch):
    def fake_run(argv, capture_output, text, timeout):
        raise FileNotFoundError()

    monkeypatch.setattr(actions.subprocess, "run", fake_run)
    rc, out = actions._default_runner(["powertriage", "10.0.0.5", "on"], timeout=5)
    assert rc == 127
    assert "powertriage" in out and "not found" in out


def test_default_runner_success_combines_stdout_stderr(monkeypatch):
    class _CP:
        returncode = 0
        stdout = "out-line\n"
        stderr = "err-line\n"

    def fake_run(argv, capture_output, text, timeout):
        assert argv == ["identtriage", "10.0.0.5", "force"]
        assert capture_output is True and text is True and timeout == 60
        return _CP()

    monkeypatch.setattr(actions.subprocess, "run", fake_run)
    rc, out = actions._default_runner(["identtriage", "10.0.0.5", "force"], timeout=60)
    assert rc == 0
    assert "out-line" in out and "err-line" in out
