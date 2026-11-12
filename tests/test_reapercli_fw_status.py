"""reaper fw [site] status [bmc|bios] [<family>]: reaper's targets vs each bang's rendered
manifests, plus manifest write times and reader start times. Read-only."""
import json
import os
from pathlib import Path

import pytest

pytest.importorskip("reapercli")
from reapercli.__main__ import Context, main  # noqa: E402
from reapercli.commands import fw_status  # noqa: E402
from reapercli.lib.remote import Result  # noqa: E402

REAL = Path(__file__).resolve().parents[1]
GOUDA, EDAM = "bang-gouda", "bang-edam"
T0 = 1_700_000_000


class Bangs:
    """Answers the one probe per bang. `files[bang][key]` = (mtime, json obj) or None."""
    def __init__(self, unreachable=(), **over):
        bmc = {"tiogapass-openbmc": {"target_version": "flax-onetree-1.1.5"}, "leopard-ami": {"target_version": "14.00"}}
        host = {"tiogapass-bios": {"target_version": "TPO_P27F"}, "leopard-bios": {"target_version": "F06_3B22"}}
        post = [{"family": "tiogapass", "target": "TPO_P27F"}, {"family": "leopard", "target": "F06_3B22"}]
        base = {"bmc": (T0, bmc), "bios": (T0, host), "post": (T0, post)}
        self.files = {GOUDA: dict(base), EDAM: dict(base)}
        self.starts = {GOUDA: {r: T0 + 60 for r, _ in fw_status._READERS},
                       EDAM: {r: None for r, _ in fw_status._READERS}}
        self.unreachable, self.cmds = set(unreachable), []
        for k, v in over.items():
            setattr(self, k, v)

    def runner(self, argv, stdin, timeout):
        host, cmd = argv[-2].split("@")[1], argv[-1]
        self.cmds.append((host, cmd))
        if host in self.unreachable:
            return Result(255, "", "ssh: connect to host timed out")
        out = []
        for key, _, _ in fw_status._FILES:
            f = self.files[host].get(key)
            out.append(f"@@file {key} {f[0]}" if f else f"@@file {key}")
            if f:
                out.append(json.dumps(f[1], indent=2))
            out.append("")
        for r, t in self.starts[host].items():
            out.append(f"@@start {r} {t if t else 'down'}")
        if host == GOUDA:
            out.append("@@vip")
        return Result(0, "\n".join(out) + "\n", "")


def run(bangs, tmp_path, *args):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    (home / ".vault_pass_eindhoven").write_text("x")
    ctx = Context(root=REAL, home=home, hostname="orbit", runner=bangs.runner, play_runner=None)
    return main(["fw", "eindhoven", *args], ctx)


def row(out, *words):
    """Cells of the table line holding `words` in a row, from the first of them on
    (a section's first row carries the section label before the family)."""
    for line in out.splitlines():
        cells = line.split()
        for i in range(len(cells)):
            if cells[i:i + len(words)] == list(words):
                return cells[i:]
    raise AssertionError(f"no row {words} in:\n{out}")


@pytest.fixture(autouse=True)
def utc(monkeypatch):
    monkeypatch.setenv("TZ", "UTC")
    import time
    time.tzset()
    yield
    monkeypatch.delenv("TZ")
    time.tzset()


def test_versions_reaper_vs_each_bang_one_ssh_per_bang(tmp_path, capsys):
    b = Bangs()
    b.files[EDAM]["bmc"] = (T0, {"tiogapass-openbmc": {"target_version": "flax-onetree-1.1.4"}})
    assert run(b, tmp_path, "status") == 0
    out = capsys.readouterr().out
    assert "eindhoven  (VIP on bang-gouda)" in out
    reaper = fw_status.reaper_targets(fw_status._vars(type("S", (), {"inventory": REAL / "inventories/eindhoven"})))
    want = reaper["bmc"]["tiogapass-openbmc"]
    assert row(out, "tiogapass-openbmc") == ["tiogapass-openbmc", want, "flax-onetree-1.1.5",
                                                     "flax-onetree-1.1.4"]
    assert sorted(h for h, _ in b.cmds) == [EDAM, GOUDA]
    assert not any(w in c for _, c in b.cmds for w in ("rm ", "restart", "systemctl stop", "> /etc"))


def test_ns_for_a_family_configured_elsewhere_or_missing_on_a_bang(tmp_path, capsys):
    b = Bangs()
    run(b, tmp_path, "status", "bmc")
    out = capsys.readouterr().out
    # tiogapass-ami-oem lives only in braintree's inventory (with no target): ns everywhere here
    assert row(out, "tiogapass-ami-oem")[1:] == ["ns", "ns", "ns"]


def test_family_missing_from_a_bang_manifest_is_ns(tmp_path, capsys):
    b = Bangs()
    b.files[EDAM]["bios"] = (T0, {"leopard-bios": {"target_version": "F06_3B22"}})
    run(b, tmp_path, "status", "bios", "tiogapass-bios")
    out = capsys.readouterr().out
    assert row(out, "tiogapass-bios")[-1] == "ns"
    assert "leopard" not in out                           # family filter
    assert row(out, "tiogapass", "(tiogapass-bios)")          # the linked post entry is kept


def test_post_entry_without_family_shown_by_platform(tmp_path, capsys):
    b = Bangs()
    b.files[GOUDA]["post"] = (T0, [{"platform": "Wiwynn Tioga Pass", "target": "TPC_P26C"}])
    run(b, tmp_path, "status", "bios")
    out = capsys.readouterr().out
    assert row(out, "[Wiwynn", "Tioga", "Pass]")[3:] == ["ns", "TPC_P26C", "ns"]
    assert row(out, "tiogapass", "(tiogapass-bios)")[3:] == ["ns", "TPO_P27F"]   # gouda: not there


def test_times_and_reader_started_before_its_manifest_is_starred(tmp_path, capsys):
    b = Bangs()
    b.files[GOUDA]["bios"] = (T0 + 3600, b.files[GOUDA]["bios"][1])
    run(b, tmp_path, "status")
    out = capsys.readouterr().out
    assert row(out, "host", "manifest")[2:4] == ["2023-11-14", "23:13"]
    assert row(out, "prod-bios_fw-1")[1:] == ["2023-11-14", "22:14", "*", "down"]
    assert row(out, "prod-bmc_fw-1")[1:] == ["2023-11-14", "22:14", "down"]           # bmc manifest older
    assert row(out, "flax-stack-flax-control-1")[3] == "*"                             # reads both
    assert row(out, "flax-post-biosd")[1:] == ["2023-11-14", "22:14", "down"]


def test_lane_filter_shows_only_that_lanes_manifests_and_readers(tmp_path, capsys):
    run(Bangs(), tmp_path, "status", "bmc")
    out = capsys.readouterr().out
    assert "host manifest" not in out and "prod-bios_fw-1" not in out and "flax-post-biosd" not in out
    assert "bmc manifest" in out and "flax-post-fwd" in out


def test_unreachable_bang_reads_question_mark_and_fails(tmp_path, capsys):
    assert run(Bangs(unreachable={EDAM}), tmp_path, "status", "bmc") == 1
    out = capsys.readouterr().out
    assert "note  bang-edam unreachable" in out
    assert row(out, "tiogapass-openbmc")[-1] == "?"
    assert row(out, "prod-bmc_fw-1")[-1] == "?"


def test_missing_manifest_file(tmp_path, capsys):
    b = Bangs()
    b.files[EDAM]["bmc"] = None
    run(b, tmp_path, "status", "bmc")
    out = capsys.readouterr().out
    assert row(out, "tiogapass-openbmc")[-1] == "?"
    assert row(out, "bmc", "manifest")[-1] == "missing"


def test_bare_fw_is_status_and_unknown_family_refused(tmp_path, capsys):
    assert run(Bangs(), tmp_path) == 0
    assert "written" in capsys.readouterr().out
    assert run(Bangs(), tmp_path, "status", "bmc", "nosuch") != 0


def test_parse_probe_against_the_real_shell(tmp_path):
    """The probe command itself, run by bash on fake files (no docker/systemctl here: all down)."""
    import shutil
    import subprocess
    if not shutil.which("bash"):
        pytest.skip("no bash")
    cmd = fw_status.probe_cmd("10.9.9.9")
    for _, _, f in fw_status._FILES:
        cmd = cmd.replace(f, str(tmp_path / Path(f).name))
    (tmp_path / "bmc-firmware-versions.json").write_text('{"a": {"target_version": "1"}}')
    os.utime(tmp_path / "bmc-firmware-versions.json", (T0, T0))
    out = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True,
                         env={"PATH": "/usr/bin:/bin"}).stdout
    p = fw_status.parse_probe(out)
    assert p["files"]["bmc"] == {"mtime": T0, "data": {"a": {"target_version": "1"}}}
    assert p["files"]["bios"] == {"mtime": None, "data": None}
    assert set(p["starts"]) == {r for r, _ in fw_status._READERS} and p["vip"] is False


def test_completion_offers_status():
    from reapercli.commands.completion import SUB  # noqa: F401
    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        main(["completion"], Context(root=REAL, home=Path("/nonexistent"), hostname="orbit"))
    fw_line = next(line for line in buf.getvalue().splitlines() if line.strip().startswith("fw)"))
    assert "status" in fw_line



def test_rackpost_reads_per_run_never_starred(tmp_path, capsys):
    b = Bangs()
    b.starts[EDAM]["flax-rackpost"] = T0 - 3600          # older than every manifest, still not stale
    run(b, tmp_path, "status", "bios")
    out = capsys.readouterr().out
    assert row(out, "flax-rackpost")[1:] == ["2023-11-14", "22:14", "(reads", "per", "run)",
                                             "2023-11-14", "21:13", "(reads", "per", "run)"]
    assert "nic" not in out
