"""reapercli.lib.triage: the triage deploy's clone guard, fetch, .env render, build, ship
and verify (spec 2026-11-08-reaper-triage-deploy-design)."""
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from reapercli.lib import triage  # noqa: E402
from reapercli.lib.guards import Refused  # noqa: E402
from reapercli.lib.inventory import Bang, SiteHosts  # noqa: E402
from reapercli.lib.remote import Result  # noqa: E402

SHA = "0123456789abcdef0123456789abcdef01234567"
OLD = "fedcba9876543210fedcba9876543210fedcba98"
COMPOSE = ("services:\n  api:\n    image: prod-api:${TRIAGE_TAG:?set by reaper deploy}\n"
           "    environment:\n      - VERSION=${version}\n      - X=${C:-x}\n"
           "      - BMC_FW_DISABLE_PORTS=${BMC_FW_DISABLE_PORTS?}\n")
SH = SiteHosts("eindhoven", "bang-gouda", ("bang-edam",),
               (Bang("bang-gouda", "192.168.88.21", False), Bang("bang-edam", "192.168.88.22", False)),
               False, "192.168.88.10")
COMP = SimpleNamespace(compose=SimpleNamespace(services=("server", "api")))
IMGS = ["prod-server:0123456789ab", "prod-api:0123456789ab"]


class Fake:
    def __init__(self, branch="main", dirty=0, pushed=1, behind=0, ref=None, refok=1,
                 files=None, have=None, ps=None, fail=None):
        self.state = dict(branch=branch, dirty=dirty, head=SHA, pushed=pushed, behind=behind,
                          dist=2000, uisrc=1000, fetched=1)
        if ref:
            self.state.update(ref=ref, refok=refok)
        self.files = files if files is not None else {"docker-compose-prod.yml": COMPOSE,
                                                      ".env": "FLASK_APP=x\n", ".env_prod": "A=1\n"}
        self.have = have if have is not None else {"tester@bang-gouda": set(), "flax@bang-edam": set()}
        self.ps, self.fail, self.log = ps or "", fail, []

    def __call__(self, argv, stdin, timeout):
        target, cmd = (argv[-2], argv[-1]) if argv[0] == "ssh" else ("local", argv[-1])
        self.log.append((target, cmd))
        if self.fail and self.fail in cmd:
            return Result(1, "", "boom")
        if "branch --show-current" in cmd:
            return Result(0, "".join(f"{k}={v}\n" for k, v in self.state.items()), "")
        if "git show" in cmd:
            name = cmd.rsplit(":", 1)[1].strip("'")
            return (Result(0, self.files[name], "") if name in self.files
                    else Result(128, "", "fatal: path does not exist"))
        if "docker image inspect" in cmd:
            return Result(0, "".join(i + "\n" for i in IMGS if i not in self.have[target]), "")
        if " build" in cmd:
            self.have[target] |= set(IMGS)
        elif "docker save" in cmd:
            self.have["flax@bang-edam"] |= set(IMGS)
        elif "docker ps" in cmd:
            return Result(0, self.ps, "")
        return Result(0, "", "")


def ctx(fake):
    return SimpleNamespace(runner=fake, user="tester", hostname="orbit")


# ---------------------------------------------------------------- guard
def test_guard_clean_main_returns_short_tag():
    src = triage.guard(SH, ctx(Fake()))
    assert (src.sha, src.tag, src.target, src.ref) == (SHA, SHA[:12], "tester@bang-gouda", False)


@pytest.mark.parametrize("kw,word", [({"dirty": 3}, "dirty"), ({"pushed": 0}, "push first"),
                                     ({"behind": 2}, "behind"), ({"branch": "wip"}, "--triage-branch")])
def test_guard_refusals(kw, word):
    with pytest.raises(Refused, match=word):
        triage.guard(SH, ctx(Fake(**kw)))


def test_guard_named_branch_allowed():
    assert triage.guard(SH, ctx(Fake(branch="wip")), branch="wip").sha == SHA


def test_guard_ref_returns_ref_sha():
    src = triage.guard(SH, ctx(Fake(ref=OLD)), ref=OLD[:7])
    assert (src.sha, src.tag, src.ref) == (OLD, OLD[:12], True)


def test_guard_ref_not_on_origin_refuses():
    with pytest.raises(Refused, match="not on origin/main"):
        triage.guard(SH, ctx(Fake(ref=OLD, refok=0)), ref=OLD[:7])


def test_guard_unreadable_clone_refuses():
    with pytest.raises(Refused, match="~/git/triage"):
        triage.guard(SH, ctx(Fake(fail="branch --show-current")))


# ---------------------------------------------------------------- fetch / render
def test_fetch_writes_0600_files(tmp_path):
    f = Fake()
    got = triage.fetch(triage.guard(SH, ctx(f)), ctx(f), tmp_path)
    assert got["docker-compose-prod.yml"] == COMPOSE
    for name in triage.FILES:
        assert stat.S_IMODE((tmp_path / name).stat().st_mode) == 0o600
    assert any(f"git show {SHA}:" in c for _, c in f.log)


def test_fetch_refuses_compose_without_triage_tag(tmp_path):
    f = Fake(files={"docker-compose-prod.yml": "services: {}\n", ".env": "", ".env_prod": ""})
    with pytest.raises(Refused, match="TRIAGE_TAG"):
        triage.fetch(triage.guard(SH, ctx(f)), ctx(f), tmp_path)


def test_required_vars_reads_both_forms():
    assert triage.required_vars("${A?} ${B:?msg here} ${C:-x} ${version} ${D-y}") == {"A", "B"}


def test_render_env_order_and_tag():
    text = triage.render_env("FLASK_APP=x\n", "abc", {"Z": "1", "B": ""})
    assert text == "FLASK_APP=x\nTRIAGE_TAG=abc\nB=\nZ=1\n"


def test_render_refuses_unset_required():
    env = triage.render_env("", "abc", {"MEZZ_FLASH_PORTS": "a"})
    with pytest.raises(Refused, match="BMC_FW_DISABLE_PORTS"):
        triage.check_env(COMPOSE, env)
    triage.check_env(COMPOSE, triage.render_env("", "abc", {"BMC_FW_DISABLE_PORTS": ""}))


def test_images_from_component():
    assert triage.images(COMP, "t") == ["prod-server:t", "prod-api:t"]


# ---------------------------------------------------------------- build / ship / verify
def test_build_cmd_sets_tag_and_lists():
    cmd = triage.build_cmd({"TRIAGE_TAG": "abc", "BMC_FW_DISABLE_PORTS": "et5b1,et5b2", "E": ""})
    assert cmd.startswith("cd ~/git/triage && ")
    assert "TRIAGE_TAG=abc BMC_FW_DISABLE_PORTS=et5b1,et5b2 E='' docker compose -p prod" in cmd
    assert cmd.endswith("-f docker-compose-prod.yml build")


def test_ship_cmd_one_stream_all_images():
    cmd = triage.ship_cmd(IMGS, "192.168.88.22")
    assert cmd.count("docker save") == 1 and all(i in cmd for i in IMGS)
    assert "flax@192.168.88.22" in cmd and "set -o pipefail" in cmd


def test_verify_flags_wrong_image_and_missing():
    out = "prod-server-1 prod-server:latest running\n"
    assert triage.verify(out, COMP, "t") == ["prod-server-1: runs prod-server:latest, want prod-server:t",
                                             "prod-api-1: not running"]
    assert triage.verify("prod-server-1 prod-server:t running\nprod-api-1 prod-api:t running\n", COMP, "t") == []


def _prep(f, yes=True, **kw):
    src = triage.guard(SH, ctx(f), **kw)
    return triage.prepare(SH, IMGS, ctx(f), src, {"TRIAGE_TAG": src.tag}, yes)


def test_prepare_plan_only_runs_nothing_but_checks():
    f = Fake()
    lines = _prep(f, yes=False)
    assert "build on bang-gouda" in "\n".join(lines) and "ship bang-edam" in "\n".join(lines)
    assert not any(" build" in c or "docker save" in c for _, c in f.log)


def test_prepare_builds_then_ships():
    f = Fake()
    _prep(f)
    cmds = [c for _, c in f.log]
    b = next(i for i, c in enumerate(cmds) if " build" in c)
    s = next(i for i, c in enumerate(cmds) if "docker save" in c)
    assert b < s and f.log[b][0] == "tester@bang-gouda" and f.log[s][0] == "tester@bang-gouda"


def test_prepare_skips_build_when_present():
    f = Fake(have={"tester@bang-gouda": set(IMGS), "flax@bang-edam": set(IMGS)})
    lines = _prep(f)
    assert not any(" build" in c or "docker save" in c for _, c in f.log)
    assert "images present" in "\n".join(lines)


def test_ref_without_images_refuses():
    with pytest.raises(Refused, match="check it out"):
        _prep(Fake(ref=OLD), ref=OLD[:7])


def test_prepare_refuses_when_standby_still_lacks_images():
    f = Fake()
    f.have = {"tester@bang-gouda": set(IMGS), "flax@bang-edam": set()}
    real = f.__call__

    def noload(argv, stdin, timeout):
        r = real(argv, stdin, timeout)
        if "docker save" in argv[-1]:
            f.have["flax@bang-edam"] = set()
        return r
    with pytest.raises(Refused, match="bang-edam"):
        src = triage.guard(SH, ctx(f))
        triage.prepare(SH, IMGS, SimpleNamespace(runner=noload, user="tester", hostname="orbit"),
                       src, {"TRIAGE_TAG": src.tag}, True)


def test_prepare_dormant_standby_not_shipped():
    sh = SiteHosts("braintree", "bang-gouda", ("bang-edam",),
                   (Bang("bang-gouda", "192.168.88.21", False), Bang("bang-edam", "192.168.88.22", True)),
                   False)
    f = Fake()
    src = triage.guard(sh, ctx(f))
    lines = triage.prepare(sh, IMGS, ctx(f), src, {"TRIAGE_TAG": src.tag}, True)
    assert "bang-edam: dormant, not shipped" in "\n".join(lines)
    assert not any("docker save" in c or t == "flax@bang-edam" for t, c in f.log)


# ---------------------------------------------------------------- review fixes
def test_site_env_refuses_a_null_list(monkeypatch):
    """`BMC_FW_DISABLE_PORTS:` with no value is a typo, not an empty list: refusing keeps a
    station blade out of bmc_fw's reach (braintree writes "" explicitly)."""
    import reapercli.lib.inventory as inv
    monkeypatch.setattr(inv, "_group_vars", lambda site, g: {"apply_triage_compose_env":
                                                              {"BMC_FW_DISABLE_PORTS": None, "A": ""}})
    with pytest.raises(Refused, match="BMC_FW_DISABLE_PORTS"):
        triage.site_env(SimpleNamespace(name="x"))


def test_site_env_refuses_a_non_string(monkeypatch):
    import reapercli.lib.inventory as inv
    monkeypatch.setattr(inv, "_group_vars", lambda site, g: {"apply_triage_compose_env": {"A": ["et5b1"]}})
    with pytest.raises(Refused, match="A"):
        triage.site_env(SimpleNamespace(name="x"))


def test_guard_refuses_without_ui_dist():
    f = Fake()
    f.state.pop("dist")
    with pytest.raises(Refused, match="monitor/web/dist"):
        triage.guard(SH, ctx(f))


def test_guard_notes_a_ui_dist_older_than_the_ui_source():
    f = Fake()
    f.state.update(dist=1000, uisrc=2000)
    assert "warn" in triage.guard(SH, ctx(f)).note and "older" in triage.guard(SH, ctx(f)).note


def test_guard_ref_unresolvable_says_so():
    f = Fake(ref="", refok=0)
    with pytest.raises(Refused, match="cannot resolve"):
        triage.guard(SH, ctx(f), ref="zzz")


def test_verify_flags_a_restarting_container():
    out = "prod-server-1 prod-server:t running\nprod-api-1 prod-api:t restarting\n"
    assert triage.verify(out, COMP, "t") == ["prod-api-1: restarting, not running"]


def test_standby_images_must_resolve_to_the_tag():
    assert triage.standby_problems("prod-server:t\nprod-api:t\n", IMGS[:0] or ["prod-server:t", "prod-api:t"]) == []
    assert triage.standby_problems("prod-server:latest\n", ["prod-server:t"]) == \
        ["staged compose resolves prod-server:latest, want prod-server:t"]


def test_dormant_line_says_warn():
    sh = SiteHosts("braintree", "bang-gouda", ("bang-edam",),
                   (Bang("bang-gouda", "192.168.88.21", False), Bang("bang-edam", "192.168.88.22", True)), False)
    f = Fake()
    src = triage.guard(sh, ctx(f))
    assert "warn ship bang-edam: dormant, not shipped" in "\n".join(
        triage.prepare(sh, IMGS, ctx(f), src, {"TRIAGE_TAG": src.tag}, False))


def test_required_vars_ignores_comments():
    """The live compose's comment reads `(${VAR?}: unset stops compose...)`: YAML comments
    are never interpolated, so they must not demand a variable."""
    text = "      # rendered into .env (${VAR?}: unset stops compose)\n      - A=${A?}  # ${B?} too\n"
    assert triage.required_vars(text) == {"A"}


# ---------------------------------------------------------------- deferred minors (2026-11-09)
def test_guard_fetches_origin_before_comparing():
    f = Fake()
    triage.guard(SH, ctx(f))
    [cmd] = [c for _, c in f.log if "branch --show-current" in c]
    assert "git fetch" in cmd and cmd.index("git fetch") < cmd.index("rev-list --count")


def test_guard_warns_when_the_fetch_fails():
    f = Fake()
    f.state["fetched"] = 0
    assert "could not fetch origin/main" in triage.guard(SH, ctx(f)).note


LISTING = """prod-api 0123456789ab 2026-11-09 13:38:12 +0000 UTC
prod-api 704d5bac3ac1 2026-11-08 10:00:00 +0000 UTC
prod-api 1111aaaa2222 2026-11-01 10:00:00 +0000 UTC
prod-api 3333bbbb4444 2026-10-01 10:00:00 +0000 UTC
prod-api rollback-20261001 2026-09-01 10:00:00 +0000 UTC
prod-api latest 2026-08-01 10:00:00 +0000 UTC
prod-server 0123456789ab 2026-11-09 13:38:12 +0000 UTC
prod-server 1111aaaa2222 2026-11-01 10:00:00 +0000 UTC
prod-other 3333bbbb4444 2026-10-01 10:00:00 +0000 UTC
"""


def test_stale_images_keeps_the_live_tag_and_the_newest_previous():
    assert triage.stale_images(LISTING, COMP, "0123456789ab") == [
        "prod-api:1111aaaa2222", "prod-api:3333bbbb4444"]


def test_stale_images_keeps_a_newer_build_when_an_older_ref_is_live():
    # --triage-ref rolled back to 1111aaaa2222: the newer builds are the way forward again
    assert triage.stale_images(LISTING, COMP, "1111aaaa2222") == ["prod-api:3333bbbb4444",
                                                                  "prod-api:704d5bac3ac1"]
