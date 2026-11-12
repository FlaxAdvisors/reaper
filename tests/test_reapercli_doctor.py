"""reaper doctor (toolchain parity, spec §3.1) and reaper completion."""
from pathlib import Path

import pytest

pytest.importorskip("reapercli")
from reapercli.__main__ import Context, main  # noqa: E402
from reapercli.commands.doctor import compare, pins  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
PINS = pins(ROOT)


def by_item(rows):
    return {r.item: r for r in rows}


def test_pins_read_from_repo_files():
    req = (ROOT / "requirements-ansible.txt").read_text()
    assert f"ansible-core=={PINS['ansible-core']}" in req.split()
    assert PINS["python"] == "3.12"
    assert PINS["arista.eos"] == "10.1.1"


def test_wrong_core_fails_naming_both():
    r = by_item(compare({"ansible-core": "2.20.1"}, PINS))["ansible-core"]
    assert r.status == "FAIL" and "2.20.1" in r.value and PINS["ansible-core"] in r.value


def test_python_matches_on_major_minor():
    assert by_item(compare({"python": "3.12.13"}, PINS))["python"].status == "ok"


def test_missing_pin_fails():
    assert by_item(compare({}, PINS))["cisco.ios"].status == "FAIL"


def test_extra_collections_are_one_info_row():
    found = {**PINS, "python": "3.12.13", "awx.awx": "24.6.1", "cisco.iosxr": "10.3.0"}
    rows = compare(found, PINS)
    extra = [r for r in rows if r.status == "info"]
    assert len(extra) == 1 and "2" in extra[0].value
    assert {r.status for r in rows} == {"ok", "info"}


def test_completion_lists_commands_and_sites(tmp_path, capsys):
    main(["completion"], Context(root=ROOT, home=tmp_path, hostname="orbit"))
    out = capsys.readouterr().out
    assert "status" in out and "eindhoven" in out and "complete -F" in out
    assert "pushids" not in out


def test_ssh_libraries_are_doctor_pins():
    assert "ansible-pylibssh" in PINS and "paramiko" in PINS


def test_completion_has_phase2_commands(tmp_path, capsys):
    main(["completion"], Context(root=ROOT, home=tmp_path, hostname="orbit"))
    out = capsys.readouterr().out
    for word in ("deploy", "migrate", "restart", "build", "ship"):
        assert word in out
    deploy = [l for l in out.splitlines() if l.strip().startswith("deploy)")]
    assert deploy and "flax_core" in deploy[0] and "flax_post" in deploy[0] and "keepalived" not in deploy[0]


def test_completion_has_phase3_words(tmp_path, capsys):
    main(["completion"], Context(root=ROOT, home=tmp_path, hostname="orbit"))
    out = capsys.readouterr().out
    ha = next(l for l in out.splitlines() if l.strip().startswith("ha)"))
    for word in ("quiesce", "arm", "keepalived-roll", "reseed", "--yes", "--confirm"):
        assert word in ha
    net = next(l for l in out.splitlines() if l.strip().startswith("net)"))
    for word in ("fetch", "drift", "plan", "push", "setup"):
        assert word in net
    push = next(l for l in out.splitlines() if l.strip().startswith("push)"))
    assert "--stage" in push and "--yes" in push


def test_completion_has_diag(tmp_path, capsys):
    main(["completion"], Context(root=ROOT, home=tmp_path, hostname="orbit"))
    out = capsys.readouterr().out
    diag = next(l for l in out.splitlines() if l.strip().startswith("diag)"))
    for word in ("--since", "--window", "kea", "post", "flax_core", "triage"):
        assert word in diag


def test_python_pin_comes_from_the_builder_and_rejects_the_old_311():
    """doctor's interpreter pin is toolchain-venv.sh's PYVER (one source); a 3.11
    toolchain left over from before the 3.12 move is a FAIL, not a quiet ok."""
    r = by_item(compare({"python": "3.11.15"}, PINS))["python"]
    assert r.status == "FAIL" and "3.11.15" in r.value and "3.12" in r.value
