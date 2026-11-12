"""reaper fw: bang-side steps (spec 2026-11-11 §6 steps 2, 4, 7, 8)."""
import json
from pathlib import Path

import pytest

pytest.importorskip("reapercli")
from reapercli.lib import fwsteps  # noqa: E402
from reapercli.lib.components import load_components  # noqa: E402
from reapercli.lib.guards import Refused  # noqa: E402
from reapercli.lib.inventory import site_hosts  # noqa: E402
from reapercli.lib.remote import Result  # noqa: E402
from reapercli.lib.sites import load_sites  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SITES = load_sites(ROOT)
TRIAGE = load_components(ROOT, SITES)["triage"]
SH = site_hosts(SITES["eindhoven"], ROOT, "orbit")
REL = "SXC-Lab_OCP-Updates/OpenBMC/flax-onetree-1.1.5.tar"
SHA = "ab" * 32
MANIFEST = "purpose=xyz.openbmc_project.Software.Version.VersionPurpose.BMC\nversion=flax-onetree-1.1.5\n"


class Fake:
    def __init__(self, out="", rc=0):
        self.calls, self.out, self.rc = [], out, rc

    def __call__(self, argv, stdin, timeout):
        self.calls.append((argv, stdin))
        return Result(self.rc, self.out, "")


def test_artifact_rel_forms():
    assert fwsteps.artifact_rel("/export/share/" + REL) == REL
    assert fwsteps.artifact_rel(REL) == REL
    assert fwsteps.artifact_rel("/export/share//" + REL) == REL


@pytest.mark.parametrize("bad", ["/tmp/x.tar", "/export/share/../etc/x.tar", "../x.tar", "/export/share/", ""])
def test_artifact_rel_refused(bad):
    with pytest.raises(Refused):
        fwsteps.artifact_rel(bad)


def test_read_tar_parses_sha_and_manifest():
    r = Fake(f"{SHA}  /export/share/{REL}\n--MANIFEST--\n{MANIFEST}")
    info = fwsteps.read_tar(SH, "bang-gouda", REL, r)
    assert info.sha256 == SHA and "version=flax-onetree-1.1.5" in info.manifest_text
    argv = r.calls[0][0]
    assert argv[0] == "ssh" and "flax@" in argv[-2] and f"/export/share/{REL}" in argv[-1]


def test_read_tar_missing_refused():
    with pytest.raises(Refused, match="not on bang-gouda"):
        fwsteps.read_tar(SH, "bang-gouda", REL, Fake("", rc=3))


def test_remote_sha_none_when_missing():
    assert fwsteps.remote_sha(SH, "bang-edam", REL, Fake("", rc=1)) is None
    assert fwsteps.remote_sha(SH, "bang-edam", REL, Fake(f"{SHA}  x\n")) == SHA


def test_sync_cmd_bang_to_bang_as_flax():
    cmd = fwsteps.sync_cmd(REL, "192.168.88.22")
    assert "flax@192.168.88.22" in cmd and "BatchMode=yes" in cmd
    assert f"/export/share/{REL}" in cmd and "mkdir -p" in cmd


def test_agent_stop_drains_only_that_agent_then_stops():
    drain, stop = fwsteps.agent_stop_cmds(TRIAGE, "bios_fw")
    assert drain == "docker exec prod-api-1 python fwdrain.py on bios_fw"
    assert stop == ("sudo -n bash -c 'cd /etc/flax/triage && docker compose -p prod "
                    "-f docker-compose-prod.yml stop bios_fw'")


def test_agent_start_is_svc_restart_recreate():
    assert fwsteps.agent_start_cmd(TRIAGE, "bmc_fw").endswith("--force-recreate bmc_fw'")


def test_clean_rows_pipes_spec_and_main():
    r = Fake('{"rows": [], "applied": false, "backup": null}\n')
    out = fwsteps.clean_rows(SH, "bang-gouda", {"lane": "bmc", "apply": False}, r)
    argv, stdin = r.calls[0]
    assert out == {"rows": [], "applied": False, "backup": None}
    assert stdin.startswith("SPEC = ") and stdin.rstrip().endswith("main(SPEC)")
    assert "docker exec -i prod-api-1 python -" in argv[-1]


def test_agent_env_reads_names_we_show():
    r = Fake("running\nBMC_FW_MODE=enforce\nBMC_FW_ENABLE_PORTS=\nBMC_FW_DISABLE_PORTS=et5b1\n"
             "SWITCHPORTRECON_URL=http://localhost:8000\nOTHER=x\n")
    e = fwsteps.agent_env(SH, "bang-gouda", "bmc_fw", r)
    assert {k: e[k] for k in ("running", "MODE", "ENABLE_PORTS", "DISABLE_PORTS", "SWITCHPORTRECON_URL")} == {
        "running": True, "MODE": "enforce", "ENABLE_PORTS": "", "DISABLE_PORTS": "et5b1",
        "SWITCHPORTRECON_URL": "http://localhost:8000"}


def test_agent_env_absent_container():
    assert fwsteps.agent_env(SH, "bang-edam", "bmc_fw", Fake("", rc=1)) == {"running": False}


def test_rendered_target():
    r = Fake('{"tiogapass-openbmc": {"target_version": "flax-onetree-1.1.5"}}')
    assert fwsteps.rendered_target(SH, "bang-gouda", "bmc", "tiogapass-openbmc", r) == "flax-onetree-1.1.5"
    assert fwsteps.rendered_target(SH, "bang-gouda", "bmc", "nope", r) is None


def test_clean_rows_standby_one_shot_from_holders_image():
    r = Fake('{"rows": [], "applied": true, "backup": null}\n')
    fwsteps.clean_rows(SH, "bang-edam", {"lane": "bmc"}, r, image="prod-api:e62a7a6ad0a5")
    cmd = r.calls[0][0][-1]
    assert cmd.startswith("docker run --rm -i") and "-v /etc/flax:/etc/flax" in cmd
    assert "-w /home/app/api prod-api:e62a7a6ad0a5 python -" in cmd


def test_post_bios_targets():
    r = Fake('[{"family": "tiogapass", "target": "TPO_P26F"}, {"family": "leopard", "target": "F06"}]')
    assert fwsteps.post_bios_targets(SH, "bang-gouda", r) == {"tiogapass": "TPO_P26F", "leopard": "F06"}
    assert fwsteps.post_bios_targets(SH, "bang-gouda", Fake("", rc=1)) == {}


def test_api_image():
    assert fwsteps.api_image(SH, "bang-gouda", Fake("prod-api:e62a7a6ad0a5\n")) == "prod-api:e62a7a6ad0a5"
    assert fwsteps.api_image(SH, "bang-gouda", Fake("", rc=1)) is None


def test_agent_env_carries_image_and_every_agent_var():
    r = Fake("running\nimage=prod-bmc_fw:abc\nBMC_FW_MODE=enforce\nBMC_FW_NETRESET_PORTS=-\nOTHER=x\n")
    e = fwsteps.agent_env(SH, "bang-gouda", "bmc_fw", r)
    assert e["image"] == "prod-bmc_fw:abc" and e["env"] == {"BMC_FW_MODE": "enforce", "BMC_FW_NETRESET_PORTS": "-"}


def test_compose_service_parses_config_json():
    cfg = {"services": {"bmc_fw": {"image": "prod-bmc_fw:abc",
                                   "environment": {"BMC_FW_MODE": "enforce", "PATH": "/x"}}}}
    r = Fake(json.dumps(cfg))
    s = fwsteps.compose_service(SH, "bang-gouda", TRIAGE, "bmc_fw", r)
    assert s == {"image": "prod-bmc_fw:abc", "env": {"BMC_FW_MODE": "enforce"}}
    assert " config --format json" in r.calls[0][0][-1]


def test_env_drift():
    live = {"image": "i:1", "env": {"BMC_FW_MODE": "detect", "BMC_FW_ENABLE_PORTS": ""}}
    cfg = {"image": "i:1", "env": {"BMC_FW_MODE": "enforce", "BMC_FW_ENABLE_PORTS": ""}}
    assert fwsteps.env_drift(live, cfg) == ["BMC_FW_MODE: running detect, recreate would use enforce"]
    assert fwsteps.env_drift(live, dict(live)) == []
    assert fwsteps.env_drift(dict(live, image="i:0"), live) == ["image: running i:0, recreate would use i:1"]


def test_reader_ages_parse():
    r = Fake("manifest 100\nagent 90\nflax-post-fwd 120\nconsole 101\n")
    assert fwsteps.reader_ages(SH, "bang-gouda", "bmc", r) == {
        "manifest": 100, "agent": 90, "flax-post-fwd": 120, "console": 101}
    assert fwsteps.stale(fwsteps.reader_ages(SH, "bang-gouda", "bmc", r), "bmc") == ["agent"]
    b = {"manifest": 100, "post_manifest": 200, "flax-post-biosd": 150, "console": 300, "agent": 300}
    assert fwsteps.stale(b, "bios") == ["flax-post-biosd"]
