"""reaper fw <site> bmc|bios <family> <artifact> [--yes] (spec 2026-11-11 §5-§7)."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

pytest.importorskip("reapercli")
from reapercli.__main__ import Context, main  # noqa: E402
from reapercli.lib.remote import Result  # noqa: E402

REAL = Path(__file__).resolve().parents[1]
REL = "SXC-Lab_OCP-Updates/OpenBMC/flax-onetree-1.1.6.tar"
BIOS_REL = "SXC-Lab_OCP-Updates/X/tiogapass-bios-TPO_P27A.tar"
SHA = "ab" * 32
GOUDA, EDAM = "bang-gouda", "bang-edam"      # ssh targets off site (flax@<name>)
EDAM_IP = "192.168.88.22"
VIP = "192.168.88.10"
MAN = {"bmc": "purpose=xyz.openbmc_project.Software.Version.VersionPurpose.BMC\nversion=flax-onetree-1.1.6\n",
       "bios": "purpose=xyz.openbmc_project.Software.Version.VersionPurpose.Host\nversion=TPO_P27A\n"}


def git(cwd, *a):
    return subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def clone(tmp_path):
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    c = tmp_path / "reaper"
    subprocess.run(["git", "clone", "-q", str(origin), str(c)], check=True, capture_output=True)
    for d in ("inventories", "files/reaper"):
        shutil.copytree(REAL / d, c / d, ignore=shutil.ignore_patterns("vault.yml"))
    git(c, "checkout", "-q", "-b", "main")
    git(c, "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A")
    git(c, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "seed")
    git(c, "push", "-q", "-u", "origin", "main")
    git(c, "config", "user.name", "t")
    git(c, "config", "user.email", "t@t")
    return c


class Site:
    """The bangs as the fake runner answers for them. A play moves the rendered state."""
    def __init__(self, lane="bmc", edam_sha=SHA, tar_on_gouda=True, rendered=None, clean_fails=False,
                 rows=None, drift=False, interrupt=False, clean_garbage=False, ages=None):
        self.lane, self.edam_sha, self.tar_on_gouda, self.clean_fails = lane, edam_sha, tar_on_gouda, clean_fails
        self.drift, self.interrupt, self.clean_garbage = drift, interrupt, clean_garbage
        self.ages = ages or {"manifest": 100, "post_manifest": 100, "agent": 200, "console": 200,
                             "flax-post": 200, "flax-post-fwd": 200, "flax-post-biosd": 200}
        self.rendered = rendered or {"bmc": "flax-onetree-1.1.5", "bios": "TPO_P26F"}
        self.post = "TPO_P26F"
        self.rows = rows if rows is not None else [{"port": "et1b1", "phase": "needs_update",
                                                    "target": "x", "action": "delete", "reason": "r"}]
        self.cmds, self.plays = [], []

    def runner(self, argv, stdin, timeout):
        host, cmd = argv[-2], argv[-1]
        self.cmds.append((host, cmd, stdin))
        ip = host.split("@")[1]
        if cmd == "ip -o -4 addr":
            return Result(0, f"2: bond0 inet {VIP}/24 brd x\n" if ip == GOUDA else "", "")
        if "--MANIFEST--" in cmd:
            return Result(0, f"{SHA}  x\n--MANIFEST--\n{MAN[self.lane]}", "") if self.tar_on_gouda \
                else Result(3, "", "")
        if cmd.startswith("test -r") and "sha256sum" in cmd:
            return Result(0, f"{self.edam_sha}  x\n", "") if self.edam_sha else Result(1, "", "")
        if cmd.startswith("set -e;") and "rsync" in cmd:
            self.edam_sha = SHA
            return Result(0, "", "")
        if cmd.startswith("cat /etc/flax/bmc-firmware-versions.json"):
            return Result(0, json.dumps({"tiogapass-openbmc": {"target_version": self.rendered["bmc"]}}), "")
        if cmd.startswith("cat /etc/flax/host-firmware-versions.json"):
            return Result(0, json.dumps({"tiogapass-bios": {"target_version": self.rendered["bios"]}}), "")
        if cmd.startswith("cat /etc/flax/bios-firmware-versions.json"):
            return Result(0, json.dumps([{"family": "tiogapass", "target": self.post}]), "")
        if cmd.startswith("docker inspect prod-api-1"):
            return Result(0, "prod-api:abc\n", "") if ip == GOUDA else Result(1, "", "")
        if cmd.startswith("docker inspect prod-"):
            if ip != GOUDA:
                return Result(1, "", "")
            p = "BMC_FW_" if "bmc_fw" in cmd else "BIOS_FW_"
            return Result(0, f"running\nimage=prod-agent:abc\n{p}MODE=enforce\n{p}ENABLE_PORTS=\n"
                             f"{p}DISABLE_PORTS=et5b1\nSWITCHPORTRECON_URL=http://localhost:8000\n", "")
        if " config --format json" in cmd:
            agent = "bmc_fw" if self.lane == "bmc" else "bios_fw"
            p = "BMC_FW_" if self.lane == "bmc" else "BIOS_FW_"
            env = {f"{p}MODE": "detect" if self.drift else "enforce", f"{p}ENABLE_PORTS": "",
                   f"{p}DISABLE_PORTS": "et5b1", "PATH": "/x"}
            return Result(0, json.dumps({"services": {agent: {"image": "prod-agent:abc", "environment": env}}}), "")
        if "echo manifest" in cmd:
            if ip != GOUDA:
                return Result(0, f"manifest {self.ages['manifest']}\n", "")
            return Result(0, "".join(f"{k} {v}\n" for k, v in self.ages.items()), "")
        if "--force-recreate" in cmd:
            self.ages["agent"] = 1000
            return Result(0, "", "")
        if cmd.startswith("sudo -n systemctl restart ") or cmd.startswith("docker restart "):
            name = cmd.split()[-1]
            self.ages["console" if "flax-control" in name else name] = 1000
            return Result(0, "", "")
        if "python -" in cmd:
            spec = stdin.splitlines()[0]
            if "'apply': True" in spec and ip == GOUDA and self.interrupt:
                raise KeyboardInterrupt
            if "'apply': True" in spec and ip == GOUDA and self.clean_garbage:
                return Result(0, "Traceback: not json", "")
            if "'apply': True" in spec and self.clean_fails and ip == GOUDA:
                return Result(1, "", "boom")
            rows = self.rows
            if "'ports': " in spec:
                rows = [dict(r, action="delete") for r in self.rows if r["action"] == "delete"]
            elif "'apply': True" in spec:
                self.rows = [dict(r, action="keep", reason="on new target") for r in self.rows]
            return Result(0, json.dumps({"rows": rows, "applied": "'apply': True" in spec,
                                         "backup": "/etc/flax/x.reaperfw-t" if "'apply': True" in spec else None}),
                          "")
        return Result(0, "", "")        # drain, stop, recreate, release

    def play(self, argv, env):
        self.plays.append(argv)
        book = Path(argv[-1]).name
        if book == "apply_bmc_fw_manifest.yml":
            self.rendered["bmc"] = "flax-onetree-1.1.6"
            self.ages.update(manifest=300, console=301, **{"flax-post": 301, "flax-post-fwd": 301})
        elif book == "apply_host_fw_manifest.yml":
            self.rendered["bios"] = "TPO_P27A"
            self.ages.update(manifest=300, console=301)
        elif book == "apply_bang_services.yml":
            self.post = "TPO_P27A"
            self.ages.update(post_manifest=300, **{"flax-post-biosd": 301})
        return 0

    def remote(self, needle):
        return [c for _, c, _ in self.cmds if needle in c]


def run(clone, site, tmp_path, *args):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    (home / ".vault_pass_eindhoven").write_text("x")
    ctx = Context(root=clone, home=home, hostname="orbit", runner=site.runner, play_runner=site.play)
    return main(["fw", "eindhoven", *args], ctx)


def bmc(clone, site, tmp_path, *extra):
    return run(clone, site, tmp_path, "bmc", "tiogapass-openbmc", "/export/share/" + REL, *extra)


def test_plan_only_changes_nothing(clone, tmp_path, capsys):
    s = Site()
    head = git(clone, "rev-parse", "HEAD")
    assert bmc(clone, s, tmp_path) == 0
    out = capsys.readouterr().out
    assert git(clone, "rev-parse", "HEAD") == head and git(clone, "status", "--porcelain") == ""
    assert s.plays == [] and not s.remote("fwdrain") and not s.remote("rsync")
    assert out.rstrip().endswith("(plan only: add --yes to run)")
    assert "flax-onetree-1.1.5 -> flax-onetree-1.1.6" in out and "et1b1" in out


def test_plan_prints_live_mode_and_port_lists(clone, tmp_path, capsys):
    bmc(clone, Site(), tmp_path)
    out = capsys.readouterr().out
    assert "mode enforce" in out and "ENABLE_PORTS (empty: every port)" in out and "DISABLE_PORTS et5b1" in out


def test_yes_happy_path_order(clone, tmp_path, capsys):
    s = Site()
    assert bmc(clone, s, tmp_path, "--yes") == 0
    msg = git(clone, "log", "-1", "--format=%B")
    assert msg.splitlines()[0] == "fw(eindhoven): tiogapass-openbmc flax-onetree-1.1.5 -> flax-onetree-1.1.6"
    assert f"artifact: {REL}" in msg and "Claude" not in msg
    assert git(clone, "rev-parse", "HEAD") == git(clone, "rev-parse", "origin/main")
    assert [Path(a[-1]).name for a in s.plays] == ["apply_bmc_fw_manifest.yml"]
    gouda = [c for h, c, _ in s.cmds if h.endswith(GOUDA)]
    i_drain = next(i for i, c in enumerate(gouda) if "fwdrain.py on bmc_fw" in c)
    i_stop = next(i for i, c in enumerate(gouda) if " stop bmc_fw" in c)
    i_clean = next(i for i, (h, c, st) in enumerate([x for x in s.cmds if x[0].endswith(GOUDA)])
                   if "python -" in c and "'apply': True" in st)
    i_start = next(i for i, c in enumerate(gouda) if "--force-recreate bmc_fw" in c)
    assert i_drain < i_stop < i_clean < i_start
    edam_clean = [st for h, c, st in s.cmds if h.endswith(EDAM) and "docker run --rm -i" in c]
    assert edam_clean and "'ports': ['et1b1']" in edam_clean[0]
    assert "post's BMC target moves too" in capsys.readouterr().out


def test_bios_also_renders_post_manifest(clone, tmp_path):
    s = Site(lane="bios")
    assert run(clone, s, tmp_path, "bios", "tiogapass-bios", BIOS_REL, "--yes") == 0
    assert [Path(a[-1]).name for a in s.plays] == ["apply_host_fw_manifest.yml", "apply_bang_services.yml"]
    assert "post_bios_manifest" in s.plays[1]
    diff = git(clone, "show", "HEAD", "--", "inventories/eindhoven/group_vars/all/vars.yml")
    assert 'target_version: "TPO_P27A"' in diff and 'target: "TPO_P27A"' in diff
    assert f'bin_url: "http://bang/export/share/{BIOS_REL}"' in diff


def test_missing_tar_on_holder_refused_before_any_change(clone, tmp_path):
    s = Site(tar_on_gouda=False)
    head = git(clone, "rev-parse", "HEAD")
    assert bmc(clone, s, tmp_path, "--yes") != 0
    assert git(clone, "rev-parse", "HEAD") == head and s.plays == [] and not s.remote("fwdrain")


def test_purpose_mismatch_refused(clone, tmp_path):
    s = Site(lane="bios")             # the tar says Host, the lane is bmc
    head = git(clone, "rev-parse", "HEAD")
    assert bmc(clone, s, tmp_path, "--yes") != 0
    assert git(clone, "rev-parse", "HEAD") == head and s.plays == [] and not s.remote("fwdrain")


def test_standby_missing_tar_is_synced(clone, tmp_path):
    s = Site(edam_sha=None)
    assert bmc(clone, s, tmp_path, "--yes") == 0
    [sync] = s.remote("rsync")
    assert f"flax@{EDAM_IP}:" in sync


def test_standby_same_sha_not_synced(clone, tmp_path):
    s = Site()
    assert bmc(clone, s, tmp_path, "--yes") == 0
    assert s.remote("rsync") == []


def test_push_failure_stops_before_bangs(clone, tmp_path, capsys):
    git(clone, "remote", "set-url", "--push", "origin", str(tmp_path / "nowhere.git"))
    s = Site()
    assert bmc(clone, s, tmp_path, "--yes") == 1
    assert "is local, not pushed" in capsys.readouterr().out
    assert s.plays == [] and not s.remote("fwdrain")


def test_resume_after_inventory_pushed(clone, tmp_path, capsys):
    bmc(clone, Site(), tmp_path, "--yes")          # moves the inventory, pushes
    s = Site()                                     # but the bangs still say 1.1.5 and rows are dirty
    capsys.readouterr()
    assert bmc(clone, s, tmp_path, "--yes") == 0
    out = capsys.readouterr().out
    assert "already at" not in out
    assert [Path(a[-1]).name for a in s.plays] == ["apply_bmc_fw_manifest.yml"]
    assert s.remote("fwdrain.py on bmc_fw")


def test_true_noop(clone, tmp_path, capsys):
    bmc(clone, Site(), tmp_path, "--yes")
    head = git(clone, "rev-parse", "HEAD")
    s = Site(rendered={"bmc": "flax-onetree-1.1.6", "bios": "TPO_P26F"},
             rows=[{"port": "et1b1", "phase": "up_to_date", "target": "flax-onetree-1.1.6",
                    "action": "keep", "reason": "on new target"}])
    capsys.readouterr()
    assert bmc(clone, s, tmp_path, "--yes") == 0
    assert "already at flax-onetree-1.1.6" in capsys.readouterr().out
    assert git(clone, "rev-parse", "HEAD") == head and s.plays == [] and not s.remote("fwdrain")


def test_clean_failure_still_recreates_and_releases(clone, tmp_path, capsys):
    s = Site(clean_fails=True)
    assert bmc(clone, s, tmp_path, "--yes") == 1
    assert s.remote("--force-recreate bmc_fw")
    assert "row clean failed" in capsys.readouterr().out


def test_dirty_clone_refused(clone, tmp_path):
    (clone / "stray").write_text("x")
    s = Site()
    assert bmc(clone, s, tmp_path, "--yes") != 0
    assert s.plays == [] and not s.remote("fwdrain")


def test_report_only_family_refused(clone, tmp_path):
    s = Site()
    assert run(clone, s, tmp_path, "bmc", "leopard-ami", REL, "--yes") != 0
    assert s.plays == []


def test_completion_offers_lanes_and_flashable_families(capsys):
    ctx = Context(root=REAL, home=Path.home(), hostname="orbit")
    assert main(["completion"], ctx) == 0
    line = next(l for l in capsys.readouterr().out.splitlines() if l.strip().startswith("fw)"))
    words = line.split('"')[1].split()
    assert {"bmc", "bios", "--yes", "tiogapass-openbmc", "tiogapass-bios"} <= set(words)
    assert "leopard-ami" not in words


def test_plan_noop_says_already_and_bmc_has_no_post_bios_step(clone, tmp_path, capsys):
    bmc(clone, Site(), tmp_path, "--yes")
    s = Site(rendered={"bmc": "flax-onetree-1.1.6", "bios": "TPO_P26F"},
             rows=[{"port": "et1b1", "phase": "up_to_date", "target": "flax-onetree-1.1.6",
                    "action": "keep", "reason": "on new target"}])
    capsys.readouterr()
    assert bmc(clone, s, tmp_path) == 0
    out = capsys.readouterr().out
    assert "already at flax-onetree-1.1.6" in out and POST_STEP not in out


POST_STEP = "--tags post_bios_manifest"


# ---------------------------------------------------------------- review fixes (I1-I3, I2)
def test_interrupt_during_clean_still_recreates(clone, tmp_path):
    """review I1: Ctrl-C / a dropped session between stop and start must not leave it down."""
    s = Site(interrupt=True)
    assert bmc(clone, s, tmp_path, "--yes") == 1
    assert s.remote("--force-recreate bmc_fw")


def test_unparseable_clean_output_still_recreates(clone, tmp_path, capsys):
    s = Site(clean_garbage=True)
    assert bmc(clone, s, tmp_path, "--yes") == 1
    assert s.remote("--force-recreate bmc_fw")
    assert "row clean failed" in capsys.readouterr().out


def test_env_drift_refused_before_anything(clone, tmp_path, capsys):
    """review I3: the recreate would arm the compose file's env, not the running one."""
    s = Site(drift=True)
    head = git(clone, "rev-parse", "HEAD")
    assert bmc(clone, s, tmp_path, "--yes") == 3
    assert git(clone, "rev-parse", "HEAD") == head and s.plays == [] and not s.remote("fwdrain")
    assert "BMC_FW_MODE: running enforce, recreate would use detect" in capsys.readouterr().err


def test_env_drift_shown_in_plan(clone, tmp_path, capsys):
    assert bmc(clone, Site(drift=True), tmp_path) == 0
    assert "recreate would use detect" in capsys.readouterr().out


def test_agent_verified_after_start(clone, tmp_path, capsys):
    s = Site()
    assert bmc(clone, s, tmp_path, "--yes") == 0
    after = [c for c in s.remote("docker inspect prod-bmc_fw-1")]
    i_start = next(i for i, (_, c, _) in enumerate(s.cmds) if "--force-recreate bmc_fw" in c)
    assert any("docker inspect prod-bmc_fw-1" in c for _, c, _ in s.cmds[i_start:]) and after
    assert "bmc_fw running on bang-gouda: mode enforce" in capsys.readouterr().out


def _at_new(clone, tmp_path):
    bmc(clone, Site(), tmp_path, "--yes")
    return [{"port": "et1b1", "phase": "up_to_date", "target": "flax-onetree-1.1.6",
             "action": "keep", "reason": "on new target"}]


def test_stale_agent_is_not_already_at(clone, tmp_path, capsys):
    """review I2: files at the new target but the agent started before the render."""
    rows = _at_new(clone, tmp_path)
    s = Site(rendered={"bmc": "flax-onetree-1.1.6", "bios": "TPO_P26F"}, rows=rows,
             ages={"manifest": 300, "post_manifest": 100, "agent": 200, "console": 400,
                   "flax-post": 400, "flax-post-fwd": 400, "flax-post-biosd": 400})
    capsys.readouterr()
    assert bmc(clone, s, tmp_path, "--yes") == 0
    assert "already at" not in capsys.readouterr().out
    assert s.remote("fwdrain.py on bmc_fw") and s.remote("--force-recreate bmc_fw") and s.plays == []


def test_stale_post_reader_restarted(clone, tmp_path):
    rows = _at_new(clone, tmp_path)
    s = Site(rendered={"bmc": "flax-onetree-1.1.6", "bios": "TPO_P26F"}, rows=rows,
             ages={"manifest": 300, "post_manifest": 100, "agent": 400, "console": 400,
                   "flax-post": 400, "flax-post-fwd": 200, "flax-post-biosd": 400})
    assert bmc(clone, s, tmp_path, "--yes") == 0
    assert s.remote("sudo -n systemctl restart flax-post-fwd") and not s.remote("fwdrain")


def test_fresh_readers_true_noop(clone, tmp_path, capsys):
    rows = _at_new(clone, tmp_path)
    s = Site(rendered={"bmc": "flax-onetree-1.1.6", "bios": "TPO_P26F"}, rows=rows,
             ages={"manifest": 300, "post_manifest": 100, "agent": 400, "console": 400,
                   "flax-post": 400, "flax-post-fwd": 400, "flax-post-biosd": 400})
    capsys.readouterr()
    assert bmc(clone, s, tmp_path, "--yes") == 0
    assert "already at" in capsys.readouterr().out and not s.remote("fwdrain")
