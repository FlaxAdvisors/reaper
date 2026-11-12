"""components.yml: what reaper manages, and where (spec §5.2). Anything a role
installs but the registry does not list is a registry bug."""
import re
from pathlib import Path

import pytest
import yaml

pytest.importorskip("reapercli")
from reapercli.lib.components import expected_running, load_components, placed_on  # noqa: E402
from reapercli.lib.inventory import site_hosts  # noqa: E402
from reapercli.lib.sites import load_sites  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SITES = load_sites(ROOT)
COMPS = load_components(ROOT, SITES)

# Tags that select nothing reaper reports on, each with its reason.
UNCLAIMED_OK = {
    "always": "ansible meta-tag",
    "post_bios_manifest": "reaper fw renders the post BIOS manifest alone; the task is also tagged flax_post",
}


def _claimed(attr):
    out = set()
    for c in COMPS.values():
        out.update(getattr(c, attr))
    return out


def test_loads_and_validates(tmp_path):
    bad = tmp_path / "files" / "reaper"
    bad.mkdir(parents=True)
    (bad / "components.yml").write_text("x: {lane: bogus, tags: [], placement: both, kind: host}\n")
    with pytest.raises(ValueError, match="lane"):
        load_components(tmp_path, SITES)
    (bad / "components.yml").write_text("eindhoven: {lane: infra, tags: [], placement: both, kind: host}\n")
    with pytest.raises(ValueError, match="site"):
        load_components(tmp_path, SITES)


def _playbook_tags():
    tags = set()
    for f in [ROOT / "playbooks" / "apply_bang_services.yml", *ROOT.glob("roles/*/tasks/*.yml")]:
        for m in re.finditer(r"tags:\s*\[([^\]]*)\]", f.read_text()):
            tags.update(t.strip() for t in m.group(1).split(",") if t.strip())
    return tags


def test_every_playbook_tag_is_claimed():
    tags = _playbook_tags()
    assert {"flax_post", "post_tgz", "kea"} <= tags, "tag scan found nothing (presence proof)"
    claimed = {t for c in COMPS.values() for t in c.tags}
    assert tags - claimed - set(UNCLAIMED_OK) == set()


def _defaults():
    d = {}
    for f in ROOT.glob("roles/*/defaults/main.yml"):
        d.update(yaml.safe_load(f.read_text()) or {})
    return d


def _expand(name, defaults):
    return re.sub(r"\{\{\s*(\w+)\s*\}\}", lambda m: str(defaults.get(m.group(1), m.group(0))), name)


def _installed():
    d = _defaults()
    units, timers, crons, containers = set(), set(), set(), set()
    for f in ROOT.glob("roles/*/tasks/*.yml"):
        text = f.read_text()
        for m in re.finditer(r"dest:\s*\"?/etc/systemd/system/([^\"\s]+)\.(service|timer)", text):
            (timers if m.group(2) == "timer" else units).add(_expand(m.group(1), d))
        for m in re.finditer(r"dest:\s*\"?/etc/systemd/system/(\{\{[^}]+\}\})", text):
            units.add(_expand(m.group(1), d).removesuffix(".service"))
        for m in re.finditer(r"cron_file:\s*\"?([^\"\n]+)", text):
            crons.add(_expand(m.group(1).strip(), d))
    for f in ROOT.glob("roles/*/templates/*.service.j2"):
        units.add(f.name.removesuffix(".service.j2"))
        for m in re.finditer(r"--name\s+([A-Za-z0-9_.-]+)", f.read_text()):
            containers.add(m.group(1))
    return units, timers, crons, containers


def test_every_installed_unit_is_claimed():
    units, timers, crons, containers = _installed()
    assert "flax-post" in units and "kea-dhcp4" in containers, "scan found nothing (presence proof)"
    templated = {u for u in units | timers if u.endswith("@")}
    assert units - templated - _claimed("units") - _claimed("timers") == set()
    assert timers - templated - _claimed("timers") - _claimed("units") == set()
    assert crons - _claimed("crons") == set()
    assert containers - _claimed("containers") == set()
    for t in templated:   # health-hook@ -> its instances are listed
        assert any(x.startswith(t) for x in _claimed("timers") | _claimed("units")), t


def _sh(site):
    return site_hosts(SITES[site], ROOT, "orbit")


def test_placed_on():
    assert placed_on(COMPS["rackpost"], _sh("eindhoven"), SITES["eindhoven"], ROOT) == ("bang-gouda",)
    assert placed_on(COMPS["rackpost"], _sh("braintree"), SITES["braintree"], ROOT) == ()
    assert placed_on(COMPS["post_fleet_viewer"], _sh("lavergne"), SITES["lavergne"], ROOT) == \
        ("bang-rhythm", "bang-blues")
    assert placed_on(COMPS["flax_iperf"], _sh("eindhoven"), SITES["eindhoven"], ROOT) == ("bang-edam",)


def test_expected_running():
    placed = ("bang-gouda", "bang-edam")
    assert expected_running(COMPS["flax_post"], "bang-edam", placed, "bang-gouda") is False
    assert expected_running(COMPS["flax_post"], "bang-gouda", placed, "bang-gouda") is True


# ---- Phase 2: compose blocks (plan 2026-11-08 Task 1)
FS = ("/etc/flax/flax-stack", "flax-stack", "docker-compose.yml", "flax-stack.env")


def _cb(name):
    c = COMPS[name].compose
    return (c.dir, c.project, c.file, c.env_file), c.services


def test_compose_blocks():
    assert _cb("flax_console") == (FS, ("flax-control",))
    assert _cb("flax_stack") == (FS, ("nginx",))
    assert _cb("triage") == (("/etc/flax/triage", "prod", "docker-compose-prod.yml", None),
                             ("server", "api", "bmc_fw", "bios_fw", "fru_fw"))
    assert _cb("health") == (("/etc/flax/health", "prod-health", "docker-compose-prod.yml", None),
                             ("prometheus", "alertmanager", "grafana", "blackbox", "snmp_exporter",
                              "smartctl_exporter"))
    assert "flax-stack" in COMPS["flax_stack"].norestart


def test_postgres_is_never_a_compose_service():
    assert not [c.name for c in COMPS.values() if c.compose and "postgres" in c.compose.services]


def test_triage_drain():
    assert COMPS["triage"].compose.drain == {"services": ("bmc_fw", "bios_fw", "fru_fw"),
                                             "cmd": "docker exec prod-api-1 python fwdrain.py on",
                                             "release": "docker exec prod-api-1 python fwdrain.py off"}


def test_compose_kind_requires_block(tmp_path):
    d = tmp_path / "files" / "reaper"
    d.mkdir(parents=True)
    (d / "components.yml").write_text("stackx: {lane: infra, tags: [], placement: both, kind: compose}\n")
    with pytest.raises(ValueError, match="stackx"):
        load_components(tmp_path, SITES)


def test_compose_argv_env_file():
    from reapercli.lib.components import compose_argv
    assert compose_argv(COMPS["flax_console"].compose, "up", "-d", "--no-deps", "flax-control") == (
        "cd /etc/flax/flax-stack && docker compose --env-file flax-stack.env -p flax-stack"
        " -f docker-compose.yml up -d --no-deps flax-control")
    assert compose_argv(COMPS["triage"].compose, "ps") == (
        "cd /etc/flax/triage && docker compose -p prod -f docker-compose-prod.yml ps")


def test_dangerous_units_are_norestart():
    for comp, unit in (("docker", "docker"), ("keepalived", "keepalived"), ("triage_firewall", "firewalld")):
        assert unit in COMPS[comp].norestart


def test_udp_ports_load():
    # plan 2026-11-09-site-health-board Task 4: the board checks UDP listeners too
    comps = load_components(ROOT, SITES)
    assert comps["kea"].udp_ports == (67, 547)
    assert comps["bang_services"].udp_ports == (53, 69)
    assert comps["keepalived"].udp_ports == ()
