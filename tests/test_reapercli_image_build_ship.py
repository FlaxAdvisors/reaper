"""reaper image build|ship (spec §6): each site's primary builds from its own clone at the
controller's HEAD; ship streams primary -> its own standby over the mgmt LAN."""
import re
import subprocess
from pathlib import Path

import pytest

pytest.importorskip("reapercli")
from reapercli.__main__ import Context, main  # noqa: E402
from reapercli.lib.images import images, version_tag  # noqa: E402
from reapercli.lib.remote import Result  # noqa: E402
from test_reapercli_guards import make_clone  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
TAG = version_tag(ROOT)


class Fake:
    def __init__(self, head, primary_head=None, have=None):
        self.head, self.primary_head = head, primary_head or head
        # host -> images present ("repo:tag"); a bare tag means every image of that tag
        self.have = {k: {i for t in v for i in ([t] if ":" in t else images(t).split())}
                     for k, v in (have or {}).items()}
        self.log, self.targets = [], []

    def __call__(self, argv, stdin, timeout):
        local = argv[0] == "bash"
        host, cmd = ("local", argv[-1]) if local else (argv[-2].replace("flax@bang-", "bang-"), argv[-1])
        self.targets.append(host)
        host = host.split("@")[-1]
        self.log.append((host, cmd))
        if "rev-parse HEAD" in cmd:
            return Result(0, self.primary_head + "\n", "")
        if "docker image inspect" in cmd:
            want = set(re.findall(r"(flax-[a-z-]+:\S+)", cmd.split(">")[0]))
            return Result(0 if want <= self.have.get(host, set()) else 1, "", "")
        if "docker save" in cmd:
            standby = "bang-edam" if "192.168.88.22" in cmd else "bang-blues"
            self.have.setdefault(standby, set()).update(re.findall(r"(flax-[a-z-]+:\S+)", cmd.split("|")[0]))
        return Result(0, "built\n", "")

    def cmds(self, word):
        return [(h, c) for h, c in self.log if word in c]


def ctx(tmp_path, runner, *sites, hostname="orbit"):
    for s in sites or ("eindhoven",):
        (tmp_path / f".vault_pass_{s}").write_text("x\n")
    clone = make_clone(tmp_path)
    return Context(root=ROOT, home=tmp_path, hostname=hostname, runner=runner, clone=clone), \
        subprocess.run(["git", "-C", str(clone), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()


def head_ctx(tmp_path, *sites, hostname="orbit", **kw):
    c, head = ctx(tmp_path, None, *sites, hostname=hostname)
    f = Fake(head, **kw)
    c.runner = f
    return c, f, head


def test_build_refuses_version_mismatch(tmp_path):
    c, f, _ = head_ctx(tmp_path)
    assert main(["image", "build", "eindhoven", TAG + "9", "--yes"], c) == 3
    assert not f.cmds("docker build")


def test_build_refuses_primary_clone_behind(tmp_path, capsys):
    c, f, _ = head_ctx(tmp_path, primary_head="f" * 40)
    assert main(["image", "build", "eindhoven", TAG, "--yes"], c) == 3
    assert "git pull" in capsys.readouterr().err
    assert not f.cmds("docker build")


def test_build_refuses_existing_tag(tmp_path):
    c, f, _ = head_ctx(tmp_path, have={"bang-gouda": {TAG}})
    assert main(["image", "build", "eindhoven", TAG, "--yes"], c) == 3
    assert not f.cmds("docker build")


def test_build_plan_only(tmp_path):
    c, f, _ = head_ctx(tmp_path)
    assert main(["image", "build", "eindhoven", TAG], c) == 0
    assert not f.cmds("docker build")


def test_build_parallel_one_per_site(tmp_path):
    c, f, _ = head_ctx(tmp_path, "eindhoven", "lavergne")
    assert main(["image", "build", "all", TAG, "--yes"], c) == 0
    builds = f.cmds(f"docker build -t flax-control:{TAG}")
    assert sorted(h for h, _ in builds) == ["bang-gouda", "bang-rhythm"]


def test_build_local_on_primary(tmp_path):
    c, f, _ = head_ctx(tmp_path, hostname="bang-gouda")
    assert main(["image", "build", "eindhoven", TAG, "--yes"], c) == 0
    [(host, cmd)] = f.cmds("docker build")
    assert host == "local" and cmd.startswith(f"cd {ROOT}")
    assert not f.cmds("rev-parse")           # the local clone IS the guarded clone


def test_ship_targets_own_standby_mgmt_ip(tmp_path):
    c, f, _ = head_ctx(tmp_path, have={"bang-gouda": {TAG}})
    assert main(["image", "ship", "eindhoven", TAG, "--yes"], c) == 0
    [(host, cmd)] = f.cmds("docker save")
    assert host == "bang-gouda" and "flax@192.168.88.22" in cmd and "docker load" in cmd


def test_ship_skips_when_present(tmp_path):
    c, f, _ = head_ctx(tmp_path, have={"bang-gouda": {TAG}, "bang-edam": {TAG}})
    assert main(["image", "ship", "eindhoven", TAG, "--yes"], c) == 0
    assert not f.cmds("docker save")


def test_ship_refuses_when_primary_lacks_tag(tmp_path):
    c, f, _ = head_ctx(tmp_path)
    assert main(["image", "ship", "eindhoven", TAG, "--yes"], c) == 3
    assert not f.cmds("docker save")


def test_ship_dormant_standby_warns(tmp_path, capsys):
    c, f, _ = head_ctx(tmp_path, "braintree", have={"bang-fiesta": {TAG}})
    assert main(["image", "ship", "braintree", TAG, "--yes"], c) == 0
    assert not f.cmds("docker save")
    assert "dormant" in capsys.readouterr().out


def test_build_on_named_branch(tmp_path):
    c, f, _ = head_ctx(tmp_path)
    subprocess.run(["git", "-C", str(c.clone), "checkout", "-qb", "feat"], check=True)
    subprocess.run(["git", "-C", str(c.clone), "push", "-qu", "origin", "feat"], check=True, capture_output=True)
    f.head = f.primary_head = subprocess.run(["git", "-C", str(c.clone), "rev-parse", "HEAD"],
                                             capture_output=True, text=True).stdout.strip()
    assert main(["image", "build", "eindhoven", TAG], c) == 3
    assert main(["image", "build", "eindhoven", TAG, "--branch", "feat"], c) == 0


def test_build_and_ship_run_as_the_operator(tmp_path):
    """The bang aliases log in as flax (reaper setup); the build must use the operator's
    own clone, and the ship the operator's key (flax->flax gouda->edam is denied)."""
    c, f, _ = head_ctx(tmp_path, have={"bang-gouda": {TAG}})
    c.user = "tester"
    assert main(["image", "ship", "eindhoven", TAG, "--yes"], c) == 0
    save = [t for t, (_, cmd) in zip(f.targets, f.log) if "docker save" in cmd]
    assert save == ["tester@bang-gouda"]


def test_build_makes_the_sol_relay_image_too(tmp_path):
    c, f, _ = head_ctx(tmp_path)
    assert main(["image", "build", "eindhoven", TAG, "--yes"], c) == 0
    [(_, cmd)] = f.cmds("docker build -t flax-control:")
    assert f"docker build -t flax-post-sol:{TAG} flax_post_sol" in cmd


def test_ship_sends_both_images_when_the_standby_lacks_one(tmp_path):
    """The SOL relay shares the tag: a standby holding only flax-control still gets the pair."""
    c, f, _ = head_ctx(tmp_path, have={"bang-gouda": {TAG}, "bang-edam": {f"flax-control:{TAG}"}})
    assert main(["image", "ship", "eindhoven", TAG, "--yes"], c) == 0
    [(_, cmd)] = f.cmds("docker save")
    assert f"docker save flax-control:{TAG} flax-post-sol:{TAG} |" in cmd
    assert f.have["bang-edam"] >= {f"flax-control:{TAG}", f"flax-post-sol:{TAG}"}
