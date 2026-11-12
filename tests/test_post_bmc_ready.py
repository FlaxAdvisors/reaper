"""bmc_ready.sh -- post.sh never talks to a BMC that is not there.

eindhoven 2026-10-02: update_mellanox.sh cold-reset the BMC (it does after it
turns a NIC's UEFI option on), post.sh slept 5 s and walked into the
inventory. `ipmitool fru` hung 50 s, every later read came back empty, and
the final `ipmitool chassis power off` was refused, so the blade stayed on.

Runs the REAL bmc_ready.sh under bash with a fake ipmitool whose answer
depends on a fake boot clock: the BMC is "down" between DOWN_FROM and
DOWN_UNTIL. Every `sleep` advances that clock instead of waiting.
"""
import os
import subprocess


import pathlib as _pl
REAPER = _pl.Path(__file__).resolve().parents[1]

POST = REAPER / "roles" / "apply_pxe_payloads" / "files" / "post"

IPMITOOL = r'''#!/bin/bash
echo "ipmitool $*" >> "$CALLS"
t=$(cat "$CLOCK")
if [ "$1 $2 $3" = "mc reset cold" ]; then exit ${RESET_RC:-0}; fi
if [ "$t" -ge "${DOWN_FROM:-999999}" ] && [ "$t" -lt "${DOWN_UNTIL:-0}" ]; then exit 1; fi
# ipmid answers mc info a while before the BMC's state services are up.
if [ "$1" = chassis ] && [ "$t" -ge "${DOWN_UNTIL:-0}" ] && [ "$t" -lt "${CHASSIS_FROM:-0}" ]; then
    echo "Command not supported in present state" >&2; exit 1
fi
exit 0
'''
SLEEP = '#!/bin/bash\necho $(( $(cat "$CLOCK") + ${1%.*} )) > "$CLOCK"\n'
LOGGER = '#!/bin/bash\necho "%s $*" >> "$CALLS"\nexit %d\n'


class Rig:
    def __init__(self, tmp):
        self.tmp = tmp
        self.bin = tmp / "bin"
        self.bin.mkdir()
        self.calls = tmp / "calls.log"
        self.calls.write_text("")
        self.clock = tmp / "clock"
        self.clock.write_text("1000\n")
        self.mark = tmp / "run" / "bmc-reset-requested"
        for name, body in (("ipmitool", IPMITOOL), ("sleep", SLEEP),
                           ("systemctl", LOGGER % ("systemctl", 1)),
                           ("modprobe", LOGGER % ("modprobe", 0))):
            p = self.bin / name
            p.write_text(body)
            p.chmod(0o755)

    def run(self, script, **env):
        e = dict(os.environ, PATH="%s:%s" % (self.bin, os.environ["PATH"]),
                 CALLS=str(self.calls), CLOCK=str(self.clock),
                 BMC_NOW_CMD="cat %s" % self.clock, BMC_RESET_MARK=str(self.mark))
        e.update({k: str(v) for k, v in env.items()})
        notes = self.tmp / "notes.log"
        full = ('ps_note() { echo "note $1" >> "%s"; }\n'
                'ps_set() { echo "set $1=$2" >> "%s"; }\n'
                'source "%s"\n%s\n' % (notes, notes, POST / "bmc_ready.sh", script))
        p = subprocess.run(["bash", "-c", full], env=e, capture_output=True, text=True, timeout=60)
        self.notes = notes.read_text() if notes.exists() else ""
        return p

    def now(self):
        return int(self.clock.read_text())

    def ipmi(self):
        return [l for l in self.calls.read_text().splitlines() if l.startswith("ipmitool")]


def test_syntax_and_no_errexit():
    assert subprocess.run(["bash", "-n", str(POST / "bmc_ready.sh")]).returncode == 0
    import re
    assert not re.search(r"^\s*set\s+-[a-zA-Z]*[eu]", (POST / "bmc_ready.sh").read_text(), re.M)


def test_healthy_bmc_passes_at_once(tmp_path):
    r = Rig(tmp_path)
    p = r.run('bmc_gate "x"; echo "rc=$? ok=$bmc_ok"')
    assert "rc=0 ok=1" in p.stdout
    assert r.ipmi() == ["ipmitool mc info", "ipmitool chassis status"]
    assert r.now() == 1000 and r.notes == ""          # no wait, no banner noise


def test_waits_for_a_silent_bmc_and_says_so(tmp_path):
    r = Rig(tmp_path)
    p = r.run('bmc_gate "the inventory"; echo "rc=$? ok=$bmc_ok"', DOWN_FROM=0, DOWN_UNTIL=1100)
    assert "rc=0 ok=1" in p.stdout
    assert r.now() >= 1100
    assert "note BMC not answering -- waiting for it before the inventory (0s of 900s)" in r.notes
    assert "set bmc=not answering" in r.notes
    assert r.notes.rstrip().endswith("set bmc=")       # banner BMC line cleared on success


def test_mc_info_alone_is_not_ready(tmp_path):
    # 12:24:20 on et6b1: ipmid was back, the power-off was still refused.
    r = Rig(tmp_path)
    p = r.run('bmc_gate "the power off"; echo "rc=$?"', CHASSIS_FROM=1060)
    assert "rc=0" in p.stdout and r.now() >= 1060


def test_one_good_answer_between_failures_is_not_ready(tmp_path):
    r = Rig(tmp_path)
    plan = tmp_path / "plan"
    # fail, pass (both probes), fail, then pass for good
    (r.bin / "ipmitool").write_text(
        '#!/bin/bash\necho "ipmitool $*" >> "$CALLS"\n'
        'n=$(cat "%s" 2>/dev/null || echo 0); echo $((n+1)) > "%s"\n'
        'case $n in 0|3) exit 1;; esac; exit 0\n' % (plan, plan))
    p = r.run('bmc_gate "x"; echo "rc=$?"')
    assert "rc=0" in p.stdout
    # probes: [mc fail] [mc,chassis ok] [mc fail] [mc,chassis ok] [mc,chassis ok]
    assert len(r.ipmi()) == 8


def test_reset_request_waits_for_down_then_up(tmp_path):
    # The BMC keeps answering for ~20 s after `mc reset cold`: that is the
    # OLD boot, not "ready".
    r = Rig(tmp_path)
    p = r.run('bmc_reset_cold; echo "reset=$?"; bmc_gate "the inventory"; echo "rc=$?"',
              DOWN_FROM=1020, DOWN_UNTIL=1120, CHASSIS_FROM=1140)
    assert "reset=0" in p.stdout and "rc=0" in p.stdout
    assert r.now() >= 1140
    assert "BMC reset requested -- waiting for it to restart" in r.notes
    assert not r.mark.exists()                          # consumed


def test_reset_that_never_goes_down_passes_after_the_grace(tmp_path):
    r = Rig(tmp_path)
    p = r.run('bmc_reset_cold; bmc_gate "x"; echo "rc=$?"')
    assert "rc=0" in p.stdout
    assert 1090 <= r.now() < 1120


def test_failed_reset_leaves_no_marker(tmp_path):
    r = Rig(tmp_path)
    p = r.run('bmc_reset_cold; echo "reset=$?"; bmc_gate "x"; echo "rc=$?"', RESET_RC=1)
    assert "reset=1" in p.stdout and "rc=0" in p.stdout
    assert r.now() == 1000 and not r.mark.exists()


def test_silent_bmc_gets_the_host_ipmi_driver_restarted(tmp_path):
    r = Rig(tmp_path)
    r.run('bmc_gate "x"', DOWN_FROM=0, DOWN_UNTIL=1400)
    calls = r.calls.read_text()
    assert "systemctl restart openipmi" in calls and "systemctl restart ipmi" in calls
    assert "modprobe ipmi_si" in calls                  # both services failed -> modules
    restarts = calls.count("systemctl restart openipmi")
    assert 2 <= restarts <= 4                           # every 120 s of silence, not every probe


def test_gives_up_after_the_budget_and_reports_it(tmp_path):
    r = Rig(tmp_path)
    p = r.run('bmc_gate "the ipmitool reads"; echo "rc=$? ok=$bmc_ok"',
              DOWN_FROM=0, DOWN_UNTIL=999999, BMC_WAIT_MAX=120)
    assert "rc=1 ok=0" in p.stdout
    assert 1120 <= r.now() <= 1130
    assert "skipping the ipmitool reads" in r.notes and "set bmc=UNREACHABLE" in r.notes


def test_no_ipmi_on_the_system_never_waits(tmp_path):
    r = Rig(tmp_path)
    p = r.run('ipmigood=0; bmc_gate "x"; echo "rc=$? ok=$bmc_ok"', DOWN_FROM=0, DOWN_UNTIL=999999)
    assert "rc=1 ok=0" in p.stdout and r.now() == 1000 and r.ipmi() == []


def test_works_without_post_status(tmp_path):
    # update_mellanox.sh sources it with no ps_* functions defined.
    r = Rig(tmp_path)
    e = dict(os.environ, PATH="%s:%s" % (r.bin, os.environ["PATH"]), CALLS=str(r.calls),
             CLOCK=str(r.clock), BMC_NOW_CMD="cat %s" % r.clock, BMC_RESET_MARK=str(r.mark),
             DOWN_FROM="0", DOWN_UNTIL="1050")
    p = subprocess.run(["bash", "-c", 'source "%s"; bmc_gate x; echo "rc=$?"' % (POST / "bmc_ready.sh")],
                       env=e, capture_output=True, text=True, timeout=60)
    assert "rc=0" in p.stdout and "bmc_ready: BMC not answering" in p.stdout


# --- the call sites ----------------------------------------------------------

POST_SH = (POST / "post.sh").read_text()
MLX = (POST / "update_mellanox.sh").read_text()


def _idx(needle, start=0, text=POST_SH):
    i = text.find(needle, start)
    assert i >= 0, "missing: %r" % needle
    return i


def test_update_mellanox_never_resets_the_bmc():
    """Operator ruling 2026-10-08: the UEFI option needs only the NIC reset.
    The BMC reset under a running host left et24b2's SOL garbled."""
    assert "domstfwreset $devhere" in MLX              # the NIC reset stays
    for needle in ("mc reset", "bmc_reset_cold", "needbmcreset", "bmc_ready.sh"):
        assert needle not in MLX, needle


def test_post_sh_waits_inside_the_nic_stage():
    nic = _idx("./update_mellanox.sh")
    gate = _idx("bmc_gate", nic)
    assert gate < _idx("ps_done", nic)                  # still the nicfw stage on the banner
    assert gate < _idx('ps_begin inventory')


def test_every_ipmitool_call_in_post_sh_is_behind_a_gate():
    """No in-band IPMI command runs unless a bmc_gate passed for it: either
    on the same line (`bmc_gate ... && ipmitool`), or inside an `if` block
    whose condition is a gate or the gate's result (bmcready)."""
    lines = POST_SH.splitlines()
    guards = []                                         # stack of "is this if gated"
    prev = ""                                           # the previous code line
    for n, line in enumerate(lines, 1):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if s.startswith("if ") or s.startswith("elif "):
            gated = "bmc_gate" in s or "$bmcready -eq 1" in s
            if s.startswith("elif "):
                guards[-1] = gated
            else:
                guards.append(gated)
        elif s == "else":
            guards[-1] = False
        elif s == "fi" or s.startswith("fi "):
            guards.pop()
        if ("ipmitool " in s or "./setipmiuser.sh" in s) and not s.startswith(("echo", "ps_")):
            # `bmc_gate ... || break` on the line before is the retry loop's gate.
            assert any(guards) or "bmc_gate" in s or "bmc_gate" in prev, \
                "ungated IPMI at post.sh:%d: %s" % (n, s)
        prev = s


def test_power_off_is_gated_retried_and_falls_back_to_shutdown():
    tail = POST_SH[_idx("ps_begin poweroff"):]
    assert tail.index("bmc_gate") < tail.index("ps_finish POWER-OFF")       # the wait is visible
    loop = tail[tail.index("for try in 1 2 3"):tail.index("else")]
    assert loop.index("bmc_gate") < loop.index("ipmitool chassis power off")
    assert "offok=1" in loop and "shutdown -h now" in loop


def test_unreachable_bmc_is_recorded_in_the_dump():
    block = POST_SH[_idx("bmcready=0"):_idx("ipmitool fru 2>&1 > $logdir/ipmitool_fru.txt", _idx("bmcready=0"))]
    assert "bmc_unreachable.txt" in block


def test_budget_covers_the_slowest_bmc_seen(tmp_path):
    # et6b4 2026-10-02: in-band IPMI answered 398 s after the cold reset.
    r = Rig(tmp_path)
    p = r.run('bmc_reset_cold; bmc_gate "the inventory"; echo "rc=$? ok=$bmc_ok"',
              DOWN_FROM=1020, DOWN_UNTIL=1398)
    assert "rc=0 ok=1" in p.stdout and 1398 <= r.now() < 1440
