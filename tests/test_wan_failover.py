"""files/services/_common/wan-failover against a fake `ip`.

bang-siesta 2026-11-10: the first site to run the daemon (primary eth0 = a static
default via bang-fiesta's mgmt, backup wlan0). IPv4 `ip route replace` keys on
the metric, so demoting the primary to 300 ADDED a second route and left the
metric-100 one winning: failover never happened.
"""
import os
import subprocess

REAPER = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir)
DAEMON = os.path.join(REAPER, "files/services/_common/wan-failover")

# Default-route table in a file; `replace` keys on (dev, metric) like the kernel.
FAKE_IP = r'''#!/bin/sh
t="$FAKE_ROUTES"
case "$*" in
  "-4 route show default dev "*)
    dev="${*##* }"; grep " dev $dev " "$t" | sed "s/ dev $dev / /" || true ;;
  "route replace default via "*)
    gw=$5; dev=$7; m=$9
    grep -v "dev $dev .*metric $m\$" "$t" > "$t.new"; mv "$t.new" "$t"
    echo "default via $gw dev $dev proto static metric $m" >> "$t"
    sort -t' ' -k9n "$t" -o "$t" ;;
  "route del default via "*)
    gw=$5; dev=$7; m=$9
    grep -q "via $gw dev $dev .*metric $m\$" "$t" || { echo "RTNETLINK answers: No such process" >&2; exit 2; }
    grep -v "via $gw dev $dev .*metric $m\$" "$t" > "$t.new"; mv "$t.new" "$t" ;;
  *) echo "unexpected: $*" >&2; exit 9 ;;
esac
'''


def _run(tmp_path, routes, call):
    (tmp_path / "bin").mkdir(exist_ok=True)
    ip = tmp_path / "bin" / "ip"
    ip.write_text(FAKE_IP)
    ip.chmod(0o755)
    rt = tmp_path / "routes"
    rt.write_text("".join(r + "\n" for r in routes))
    env = dict(os.environ, PATH=f"{tmp_path / 'bin'}:{os.environ['PATH']}", FAKE_ROUTES=str(rt),
               WAN_FAILOVER_PRIMARY_IFACE="eth0", WAN_FAILOVER_BACKUP_IFACE="wlan0")
    out = subprocess.run(["bash", "-c", f'source "{DAEMON}"; {call}'], env=env,
                         capture_output=True, text=True, timeout=10)
    assert out.returncode == 0, out.stderr
    return out.stdout.strip(), [line for line in rt.read_text().splitlines() if line]


STATIC = "default via 192.168.88.21 dev eth0 proto static metric 100"
WLAN = "default via 10.12.40.1 dev wlan0 proto dhcp metric 200"


def test_demote_leaves_one_primary_route_at_the_new_metric(tmp_path):
    _, routes = _run(tmp_path, [STATIC, WLAN], "set_primary_metric 192.168.88.21 300")
    eth0 = [r for r in routes if " dev eth0 " in r]
    assert eth0 == ["default via 192.168.88.21 dev eth0 proto static metric 300"]
    assert WLAN in routes                                  # the backup is never touched


def test_a_stray_second_metric_is_seen_as_drift(tmp_path):
    out, _ = _run(tmp_path, [STATIC, WLAN, "default via 192.168.88.21 dev eth0 proto static metric 300"],
                  "current_metric eth0")
    assert out == "100 300"                                # != "300": the loop reconciles it away


def test_sourcing_does_not_start_the_loop(tmp_path):
    out, _ = _run(tmp_path, [STATIC], "echo sourced")
    assert out == "sourced"
