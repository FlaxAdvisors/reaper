"""The bang probe's pure parsers, against output captured from bang-gouda
(tests/fixtures/reapercli/gouda/, 2026-11-07)."""
import os
from pathlib import Path

import pytest

pytest.importorskip("reapercli")
from reapercli import probe_remote as pr  # noqa: E402

F = Path(__file__).parent / "fixtures" / "reapercli" / "gouda"


def read(name):
    return (F / name).read_text()


def test_parse_df_root():
    disks = {d["mount"]: d["used_pct"] for d in pr.parse_df(read("df.txt"))}
    assert disks["/"] == 42


def test_parse_systemctl_show():
    units = pr.parse_systemctl_show(read("systemctl_show.txt"))
    assert units["flax-post"]["active"] == "active"
    assert units["flax-post"]["sub"] == "running"
    assert units["flax-post"]["enabled"] == "disabled"     # VIP-gated: started by notify_master
    assert units["flax-post"]["restarts"] == 0
    assert units["flax-iperf"]["active"] == "inactive"     # not the iperf server


def test_parse_docker_ps_and_inspect():
    ps = pr.parse_docker_ps(read("docker_ps.jsonl"))
    assert ps["kea-dhcp4"]["state"] == "running"
    assert ps["kea-dhcp4"]["image"] == "docker.cloudsmith.io/isc/docker/kea-dhcp4:2.6.3"
    insp = pr.parse_docker_inspect(read("docker_inspect.jsonl"))
    assert insp["kea-dhcp4"]["health"] == "healthy"
    assert insp["flax-post"]["health"] == ""               # no healthcheck
    assert insp["flax-post"]["restarts"] == 0


def test_parse_timers():
    t = pr.parse_timers(read("timers.json"))
    assert t["health-hook@chk_services_metric"]["last"] > 1_700_000_000
    assert t["wlan-watchdog"]["next"] > t["wlan-watchdog"]["last"]


def test_parse_ss_listen():
    ports = pr.parse_ss_listen(read("ss.txt"))
    assert 8446 in ports and 22 in ports
    assert ports == sorted(set(ports))


def test_parse_tailscale():
    peers = pr.parse_tailscale(read("tailscale.txt"))
    assert peers["bang-edam"] == "direct"
    assert peers["bang-rhythm"] == "relay"
    assert peers["bang-lorax"] == "offline"
    assert peers["bang-siesta"] == "offline"          # offline wins over relay
    assert peers["bang-gouda"] == "idle"


def test_collect_survives_missing_tools(monkeypatch):
    monkeypatch.setenv("PATH", "/nonexistent")
    facts = pr.collect({"units": ["flax-post"], "containers": ["kea-dhcp4"], "timers": [],
                        "files": [], "freshness": {}, "vip": "192.168.88.10"})
    assert "error" in facts["containers"]
    assert "error" in facts["units"]
    assert "host" in facts


def test_parse_df_one_row_per_filesystem():
    text = ("Filesystem 1024-blocks Used Available Capacity Mounted on\n"
            "/dev/sda2 100 93 7 93% /\n"
            "/dev/sda2 100 93 7 93% /.snapshots\n"
            "/dev/sda2 100 93 7 93% /var\n"
            "/dev/sdb1 100 87 13 87% /export\n")
    assert pr.parse_df(text) == [{"mount": "/", "used_pct": 93}, {"mount": "/export", "used_pct": 87}]


def test_toolchain_reports_pyyaml_and_jinja2():
    import jinja2
    import yaml
    tc = pr.toolchain({})
    assert tc["PyYAML"] == yaml.__version__ and tc["Jinja2"] == jinja2.__version__


def test_toolchain_reports_ssh_libraries():
    tc = pr.toolchain({})
    assert "ansible-pylibssh" in tc and "paramiko" in tc


def test_hung_docker_does_not_eat_the_probe(tmp_path, monkeypatch):
    fake = tmp_path / "docker"
    fake.write_text("#!/bin/sh\nsleep 30\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:/usr/bin:/bin")
    import time
    t0 = time.time()
    facts = pr.collect({"units": [], "containers": [], "timers": [], "files": [], "freshness": {},
                        "vip": "192.168.88.10"})
    assert time.time() - t0 < 12
    for key in ("containers", "pg", "images"):
        assert "docker not answering" in facts[key]["error"], key



def test_parse_inspect_keeps_flax_json_binds():
    import json as _j
    line = _j.dumps({"name": "/flax-observe", "state": {"StartedAt": "2026-11-08T12:00:00Z", "Pid": 42},
                     "restarts": 0,
                     "mounts": [{"Type": "bind", "Source": "/etc/flax/geometry.json", "Destination": "/etc/flax/geometry.json"},
                                {"Type": "bind", "Source": "/etc/flax/flax-stack/x.conf", "Destination": "/x.conf"},
                                {"Type": "volume", "Source": "/var/lib/docker/volumes/v/_data", "Destination": "/v"}]})
    d = pr.parse_docker_inspect(line)["flax-observe"]
    assert d["binds"] == [["/etc/flax/geometry.json", "/etc/flax/geometry.json"]]
    assert d["pid"] == 42


def test_parse_proc_stat_maps_back_to_container():
    conts = {"flax-observe": {"state": "running", "pid": 42,
                              "binds": [["/etc/flax/geometry.json", "/etc/flax/geometry.json"]]}}
    got = pr.parse_bind_stat("6793797 /proc/42/root/etc/flax/geometry.json\n", conts, {"/etc/flax/geometry.json": 6977647})
    assert got == {"flax-observe": {"/etc/flax/geometry.json": [6977647, 6793797]}}
