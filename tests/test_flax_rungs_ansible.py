import json, os, stat, subprocess, types, pytest
pytest.importorskip("flax_rungs")
from flax_rungs import ansible as A

RECAP = """
PLAY [Install packages, utilities, and configure flax user PRN] ****

TASK [Gathering Facts] ****
ok: [10.36.37.51]
fatal: [10.36.37.53]: UNREACHABLE! => {"changed": false, "msg": "Failed to connect to the host via ssh: ssh: connect to host 10.36.37.53 port 22: No route to host", "unreachable": true}
fatal: [10.36.5.101]: UNREACHABLE! => {"changed": false, "msg": "Invalid/incorrect password: Permission denied, please try again.", "unreachable": true}
fatal: [10.36.5.51]: FAILED! => {"msg": "boom"}

PLAY RECAP *********************************************************************
10.36.37.51                : ok=14   changed=9    unreachable=0    failed=0    skipped=9    rescued=0    ignored=0
10.36.37.53                : ok=0    changed=0    unreachable=1    failed=0    skipped=0    rescued=0    ignored=0
10.36.5.101                : ok=0    changed=0    unreachable=1    failed=0    skipped=0    rescued=0    ignored=0
10.36.5.51                 : ok=3    changed=1    unreachable=0    failed=1    skipped=0    rescued=0    ignored=0

"""

HOSTS = {"C37": "10.36.37.51", "R37": "10.36.37.53", "R05": "10.36.5.101",
         "L05": "10.36.5.51", "L37": "10.36.37.99"}


class Runner:
    def __init__(self, stdout=RECAP, rc=4, exc=None):
        self.stdout, self.rc, self.exc, self.calls = stdout, rc, exc, []

    def __call__(self, argv, **kw):
        secret = next(a[1:] for a in argv if a.startswith("@"))
        st = os.stat(secret)
        self.calls.append({"argv": argv, "kw": kw, "mode": stat.S_IMODE(st.st_mode),
                           "secret": json.load(open(secret)), "path": secret,
                           "inv": open(argv[argv.index("-i") + 1]).read()})
        if self.exc:
            raise self.exc
        return types.SimpleNamespace(returncode=self.rc,
                                     stdout=self.stdout + "\nleaked SECRETPW\n", stderr="")


def test_parse_recap():
    r = A.parse_recap(RECAP)
    assert r["10.36.37.51"] == {"ok": 14, "changed": 9, "unreachable": 0, "failed": 0,
                                "skipped": 9, "rescued": 0, "ignored": 0}
    assert r["10.36.37.53"]["unreachable"] == 1
    assert r["10.36.5.51"]["failed"] == 1
    assert A.parse_recap("no recap here") == {}


def test_verdicts_per_host(tmp_path):
    run = Runner()
    out = A.flax_user(HOSTS, "flax", "SECRETPW", tmp_path, runner=run)
    assert out["C37"].verdict == "pass"
    assert out["R37"].verdict == "unmeasured"            # no route: nothing judged
    assert out["R05"].verdict == "fail"                  # refused login is a real fail
    assert "flax" in out["R05"].detail["error"]
    assert out["L05"].verdict == "fail"
    assert out["L37"].verdict == "fail" and "not in PLAY RECAP" in out["L37"].detail["error"]
    art = out["C37"].artifacts[A.ARTIFACT]
    assert "PLAY RECAP" in art and "SECRETPW" not in art


def test_password_never_in_argv_or_env_and_file_0600_removed(tmp_path):
    run = Runner(rc=0)
    A.flax_user({"C37": "10.36.37.51"}, "flax", "SECRETPW", tmp_path, runner=run)
    c = run.calls[0]
    assert not any("SECRETPW" in a for a in c["argv"])
    assert "SECRETPW" not in " ".join(f"{k}={v}" for k, v in c["kw"]["env"].items())
    assert c["mode"] == 0o600
    # every alias: ansible takes the ssh password from the last one defined, so a lone
    # ansible_password loses to group_vars' ansible_ssh_pass
    for k in ("ansible_password", "ansible_ssh_pass", "ansible_ssh_password",
              "ansible_become_password", "ansible_become_pass"):
        assert c["secret"][k] == "SECRETPW", k
    assert c["argv"][:2] == ["ansible-playbook", A.PLAYBOOK]
    assert c["kw"]["cwd"] == "/opt/flax/ansible"
    assert "[nodes]" in c["inv"] and "10.36.37.51 ansible_user=flax" in c["inv"]
    assert "SECRETPW" not in c["inv"]
    assert not os.path.exists(c["path"])
    assert [p.name for p in tmp_path.iterdir()] == [A.INVENTORY]


@pytest.mark.parametrize("exc", [subprocess.TimeoutExpired(["x"], 5), OSError("no such file"),
                                 RuntimeError("boom")])
def test_secret_removed_on_error(tmp_path, exc):
    run = Runner(exc=exc)
    if isinstance(exc, RuntimeError):
        with pytest.raises(RuntimeError):
            A.flax_user({"C37": "1.2.3.4"}, "flax", "p", tmp_path, runner=run)
    else:
        out = A.flax_user({"C37": "1.2.3.4"}, "flax", "p", tmp_path, runner=run)
        assert out["C37"].verdict == "fail"
    assert not os.path.exists(run.calls[0]["path"])
    assert [p.name for p in tmp_path.iterdir()] == [A.INVENTORY]


def test_no_hosts_runs_nothing(tmp_path):
    run = Runner()
    assert A.flax_user({}, "flax", "p", tmp_path, runner=run) == {}
    assert run.calls == []
