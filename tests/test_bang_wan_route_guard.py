"""roles/apply_bang_router/files/bang-wan-route-guard against a fake `ip`.

lavergne 2026-10-28: replugging bang-rhythm's wifi dongle re-created wlan0 and
wicked installed its DHCP default route with no metric, ahead of corp (100),
although ifcfg-wlan0 says DHCLIENT_ROUTE_PRIORITY='200'. The guard puts a
backup WAN's default route back at its configured metric.
"""
import os
import subprocess

import pytest

REAPER = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir)
GUARD = os.path.join(REAPER, "roles/apply_bang_router/files/bang-wan-route-guard")

# The fake keeps the default-route table in a file, one `ip route` line each.
FAKE_IP = r'''#!/bin/sh
t="$FAKE_ROUTES"
echo "ip $*" >> "$FAKE_LOG"
case "$*" in
  "-4 route show default dev "*)
    # real iproute2 drops "dev X" from the lines and leaves a trailing space
    dev="${*##* }"; grep " dev $dev " "$t" | sed "s/ dev $dev / /; s/\$/ /" || true ;;
  "route add default via "*)
    gw=$5; dev=$7; m=$(echo "$*" | sed -n 's/.* metric \([0-9]*\).*/\1/p')
    if grep -q "via $gw dev $dev .*metric $m\$" "$t"; then echo "RTNETLINK answers: File exists" >&2; exit 2; fi
    echo "default via $gw dev $dev proto dhcp metric $m" >> "$t" ;;
  "route del default via "*)
    gw=$5; dev=$7; m=$(echo "$*" | sed -n 's/.* metric \([0-9]*\).*/\1/p')
    if [ "$m" = 0 ]; then pat="^default via $gw dev $dev proto [a-z]*\$"; else pat="^default via $gw dev $dev .*metric $m\$"; fi
    grep -q "$pat" "$t" || { echo "RTNETLINK answers: No such process" >&2; exit 2; }
    grep -v "$pat" "$t" > "$t.new"; mv "$t.new" "$t" ;;
  *) echo "unexpected: $*" >&2; exit 9 ;;
esac
'''


def _run(tmp_path, routes, *args):
    (tmp_path / "bin").mkdir(exist_ok=True)
    ip = tmp_path / "bin" / "ip"
    ip.write_text(FAKE_IP)
    ip.chmod(0o755)
    for name in ("logger",):
        f = tmp_path / "bin" / name
        f.write_text('#!/bin/sh\necho "logger $*" >> "$FAKE_LOG"\n')
        f.chmod(0o755)
    t = tmp_path / "routes"
    t.write_text("".join(r + "\n" for r in routes))
    log = tmp_path / "log"
    log.write_text("")
    env = dict(os.environ, PATH=f"{tmp_path/'bin'}:/usr/bin:/bin",
               FAKE_ROUTES=str(t), FAKE_LOG=str(log))
    p = subprocess.run(["sh", GUARD, *args], env=env, capture_output=True, text=True)
    return p, t.read_text().splitlines(), log.read_text()


CORP = "default via 192.168.100.1 dev corp proto static metric 100"


def test_unmetered_backup_route_is_moved_to_its_metric(tmp_path):
    p, routes, log = _run(tmp_path, [CORP, "default via 192.168.100.1 dev wlan0 proto dhcp"],
                          "wlan0:200")
    assert p.returncode == 0, p.stderr
    assert sorted(routes) == sorted([CORP, "default via 192.168.100.1 dev wlan0 proto dhcp metric 200"])
    assert "wlan0 default via 192.168.100.1 metric 0 -> 200" in log


def test_wrong_nonzero_metric_is_moved_too(tmp_path):
    p, routes, _ = _run(tmp_path, ["default via 10.0.0.1 dev wlan0 proto dhcp metric 50"], "wlan0:200")
    assert p.returncode == 0
    assert routes == ["default via 10.0.0.1 dev wlan0 proto dhcp metric 200"]


def test_already_right_is_a_noop(tmp_path):
    ok = "default via 192.168.100.1 dev wlan0 proto dhcp metric 200"
    p, routes, log = _run(tmp_path, [CORP, ok], "wlan0:200")
    assert p.returncode == 0 and routes == [CORP, ok]
    assert "route add" not in log and "route del" not in log and "logger" not in log


def test_both_present_drops_only_the_wrong_one(tmp_path):
    ok = "default via 192.168.100.1 dev wlan0 proto dhcp metric 200"
    p, routes, _ = _run(tmp_path, [ok, "default via 192.168.100.1 dev wlan0 proto dhcp"], "wlan0:200")
    assert p.returncode == 0 and routes == [ok]


def test_no_route_on_the_iface_is_a_noop(tmp_path):
    p, routes, log = _run(tmp_path, [CORP], "wlan0:200")
    assert p.returncode == 0 and routes == [CORP]
    assert "route add" not in log


def test_other_ifaces_are_never_touched(tmp_path):
    corp0 = "default via 192.168.100.1 dev corp proto dhcp"
    p, routes, _ = _run(tmp_path, [corp0, "default via 192.168.100.1 dev wlan0 proto dhcp"], "wlan0:200")
    assert corp0 in routes


def test_several_pairs(tmp_path):
    p, routes, _ = _run(tmp_path, ["default via 1.1.1.1 dev a proto dhcp",
                                   "default via 2.2.2.2 dev b proto dhcp metric 7"], "a:100", "b:200")
    assert p.returncode == 0
    assert sorted(routes) == ["default via 1.1.1.1 dev a proto dhcp metric 100",
                              "default via 2.2.2.2 dev b proto dhcp metric 200"]


@pytest.mark.parametrize("bad", ["wlan0", "wlan0:", ":200", "wlan0:2x0"])
def test_malformed_pair_is_a_usage_error_and_touches_nothing(tmp_path, bad):
    p, routes, log = _run(tmp_path, ["default via 192.168.100.1 dev wlan0 proto dhcp"], bad)
    assert p.returncode == 2
    assert "route add" not in log and "route del" not in log
