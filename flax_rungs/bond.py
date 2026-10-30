"""ha-config: bond0 (active-backup) over the two PCIe fabric ports of the installed OS.

The ports are found BY MAC, never by name; the primary is the port LLDP saw on wedge A
-- not the P0 label (R05 is cross-cabled). Deploy decides the sides (live wedge LLDP,
else Qualify's ha-path: sides()); a port left admin-down by the installer is brought
up first (bring_up) so the wedges can see it.

bond0 is written as systemd-networkd units, NOT netplan (2026-10-30, R37): `netplan
generate` rewrites every /run/systemd/network/10-netplan-*.network, so the following
`networkctl reload` reconfigured the mgmt NIC enp94s0np0 too (journal: "Reconfiguring
with 10-netplan-enp94s0np0.network", "DHCPv6 lease lost", DHCPv4 re-acquired 4 s later
although `critical: true`) -- R37 lost mgmt 4-8 s on every apply. networkd keeps a
.network file whose stats did not change, so writing only our own units leaves the mgmt
link alone. In /etc/systemd/network/: 60-bond0.netdev, 60-bond0.network and one
60-bond0-slave-{a,b}.network per port matched by PermanentMACAddress (A = PrimarySlave);
the address is the device's A-side fabric formula (fabric.fabric_addr "A"). Then
`networkctl reload` + `reconfigure` of bond0 and its two slaves only (never `netplan
apply`, never a reload of anything we did not change). netplan must not also configure
those ports (its 10-netplan-* units sort first and would win): another /etc/netplan
file naming a port or its MAC refuses the rung; rackpost's own earlier
/etc/netplan/60-bond0.yaml is retired to /var/lib/flax/ with its generated units. Each
link's `Network File` is read back and must be ours.

The mgmt NIC (the mezz enp94s0np0, the one holding the host IP) is never in a unit:
a fabric MAC that resolves to it refuses the rung. The bang pings the host's mgmt IP
every 100 ms across the apply; any gap over MGMT_GAP_MS fails the rung. Verified from
/proc/net/bonding/bond0: active-backup, MII 100 ms, both slaves (by permanent MAC) up,
the active slave = the primary, and the address on bond0."""
import re
import shlex
import signal
import subprocess
import time

from . import hostssh
from .remote import unreachable
from .result import UNMEASURED, RungResult

NETPLAN = "/etc/netplan/60-bond0.yaml"       # rackpost's netplan bond before 2026-10-30
NETPLAN_RETIRED = "/var/lib/flax/60-bond0.yaml.retired"
UNIT_DIR = "/etc/systemd/network"
NETPLAN_RUN = "/run/systemd/network"
MGMT_IF = "enp94s0np0"
MIIMON = 100
PING_MS = 100
MGMT_GAP_MS = 10000       # ssh must survive; the networkd units no longer touch mgmt (R37 lost 4-8 s via netplan)
SETTLE_S = 30
HA_MODE = "active-backup"
# order.db rows from before 2026-10-30 carry a longer mode text and an interconnect note
_OLD_MODE = re.compile(r"\s*\(no MLAG[^)]*\)")
_OLD_NOTE = re.compile(r"MLAG|single-node failover")


def clean_ha(x):
    """Stored detail/ha_mode in today's HA wording: the old mode suffix cut, the old
    interconnect notes dropped (for serving rows written before the change)."""
    if isinstance(x, str):
        return _OLD_MODE.sub("", x)
    if isinstance(x, list):
        return [v for v in map(clean_ha, x) if not (isinstance(v, str) and _OLD_NOTE.search(v))]
    if isinstance(x, dict):
        return {k: clean_ha(v) for k, v in x.items()}
    return x


def _mac(s: str) -> str:
    m = re.sub(r"[^0-9a-f]", "", (s or "").lower())
    return ":".join(m[i:i + 2] for i in range(0, 12, 2)) if len(m) == 12 else ""


# ---------------------------------------------------------------- inputs
def sides(links: list[dict], ifmac: dict[str, str]) -> tuple[dict, str | None]:
    """({"A": {"mac", "link", "ifname"}, "B": {...}}, error) from Qualify's ha-path
    detail["links"] (seen = "<wedge> <iface>", ifname = live-image name) and the live
    image's ifname -> MAC. The side is the wedge LLDP saw, not the link's label."""
    out, errs = {}, []
    for l in links or []:
        if not l.get("seen") or not l.get("ifname"):
            continue
        side = l["seen"].split()[0]
        mac = _mac(ifmac.get(l["ifname"], ""))
        if side not in ("A", "B"):
            errs.append(f"{l.get('link')}: seen on {side}, not wedge A/B")
        elif not mac:
            errs.append(f"{l.get('link')}: no MAC for {l['ifname']}")
        elif side in out:
            errs.append(f"two ports seen on wedge {side}")
        else:
            out[side] = {"mac": mac, "link": l.get("link"), "ifname": l["ifname"]}
    for side in ("A", "B"):
        if side not in out:
            errs.append(f"no port seen on wedge {side}")
    if out.get("A") and out.get("B") and out["A"]["mac"] == out["B"]["mac"]:
        errs.append("both sides carry the same MAC")
    return out, ("; ".join(errs) or None)


def unit_files(names: dict[str, str], macs: dict[str, str], cidr: str) -> dict[str, str]:
    """names/macs: side -> installed kernel name / permanent MAC; primary = side A.
    file name -> content, for UNIT_DIR (the same keys as netplan rendered them)."""
    head = "# rackpost Deploy (ha-config): bond0 active-backup, primary = the wedge-A port\n"
    out = {"60-bond0.netdev": f"""{head}[NetDev]
Name=bond0
Kind=bond

[Bond]
Mode=active-backup
MIIMonitorSec={MIIMON}ms
""", "60-bond0.network": f"""{head}[Match]
Name=bond0

[Network]
LinkLocalAddressing=ipv6
Address={cidr}
IPv6AcceptRA=no
ConfigureWithoutCarrier=yes
"""}
    for s in ("A", "B"):
        out[slave_unit(s)] = f"""{head}# {names[s]} (wedge {s})
[Match]
PermanentMACAddress={macs[s]}

[Network]
LinkLocalAddressing=no
IPv6AcceptRA=no
Bond=bond0
""" + ("PrimarySlave=true\n" if s == "A" else "")
    return out


def slave_unit(side: str) -> str:
    return f"60-bond0-slave-{side.lower()}.network"


def parse_netfiles(text: str) -> dict[str, str]:
    """link -> its Network File from the apply script's `NETFILE <link> <path>` lines."""
    out = {}
    for line in text.splitlines():
        f = line.split()
        if len(f) >= 2 and f[0] == "NETFILE":
            out[f[1]] = f[2] if len(f) > 2 else ""
    return out


# ---------------------------------------------------------------- parsers
def parse_links(text: str) -> dict[str, str]:
    """ifname -> PERMANENT MAC from `ip -o link` (an enslaved port carries the bond's
    MAC as link/ether and its own as `permaddr`)."""
    out = {}
    for line in text.splitlines():
        m = re.match(r"^\d+:\s+([^:@\s]+)[@:]", line)
        if not m:
            continue
        p = re.search(r"permaddr\s+([0-9a-fA-F:]{17})", line)
        e = re.search(r"link/ether\s+([0-9a-fA-F:]{17})", line)
        if p or e:
            out[m.group(1)] = (p or e).group(1).lower()
    return out


def addr_iface(text: str, ip: str) -> str | None:
    """The interface of `ip -4 -o addr show` holding ip."""
    for line in text.splitlines():
        m = re.match(r"^\d+:\s+(\S+)\s+inet\s+([\d.]+)/", line)
        if m and m.group(2) == ip:
            return m.group(1)
    return None


def parse_bonding(text: str) -> dict | None:
    """/proc/net/bonding/bond0 -> {mode, primary, active, miimon, slaves: {name:
    {mii, perm}}}; None when it is not a bonding file."""
    if "Bonding Mode:" not in text:
        return None
    top, slaves, cur = {}, {}, None
    for line in text.splitlines():
        k, _, v = line.partition(":")
        k, v = k.strip(), v.strip()
        if k == "Slave Interface":
            cur = slaves.setdefault(v, {"mii": None, "perm": None})
        elif cur is not None:
            if k == "MII Status":
                cur["mii"] = v
            elif k == "Permanent HW addr":
                cur["perm"] = _mac(v)
        else:
            top[k] = v
    mode = top.get("Bonding Mode", "")
    return {"mode": "active-backup" if "active-backup" in mode else mode,
            "primary": (top.get("Primary Slave") or "").split(" ")[0] or None,
            "active": (top.get("Currently Active Slave") or None),
            "miimon": int(top["MII Polling Interval (ms)"])
            if top.get("MII Polling Interval (ms)", "").isdigit() else None,
            "mii": top.get("MII Status"), "slaves": slaves}


def slave_by_mac(b: dict, mac: str) -> str | None:
    return next((n for n, s in (b or {}).get("slaves", {}).items() if s["perm"] == mac), None)


def parse_ping(text: str, interval_ms: int = PING_MS) -> dict:
    """{sent, received, lost, max_gap_ms}: max_gap_ms = the longest run of unanswered
    sequence numbers x the interval (a run reaching the end counts too)."""
    got = {int(m) for m in re.findall(r"bytes from .*?icmp_seq=(\d+)", text)}
    m = re.search(r"(\d+) packets transmitted", text)
    sent = int(m.group(1)) if m else max(got, default=0)
    run = worst = 0
    for seq in range(1, sent + 1):
        run = 0 if seq in got else run + 1
        worst = max(worst, run)
    rec = len(got & set(range(1, sent + 1))) if sent else 0
    return {"sent": sent, "received": rec, "lost": sent - rec, "max_gap_ms": worst * interval_ms}


# ---------------------------------------------------------------- bang-side ping
class _Ping:
    def __init__(self, ip, interval_ms):
        self.p = subprocess.Popen(["ping", "-n", "-i", str(interval_ms / 1000), ip],
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    def stop(self) -> str:
        try:
            self.p.send_signal(signal.SIGINT)
            out, _ = self.p.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            self.p.kill()
            out, _ = self.p.communicate()
        return out or ""


def bang_ping(ip: str, interval_ms: int = PING_MS):
    """Start pinging ip from the bang; .stop() -> ping's output."""
    return _Ping(ip, interval_ms)


# ---------------------------------------------------------------- remote reads
_STATUS = "cat /proc/net/bonding/bond0 2>&1\necho ===ADDR\nip -4 -o addr show dev bond0 2>&1\n"


def status(host_ip, user, pw, *, runner=subprocess.run) -> tuple[int, dict | None, str, str]:
    """(rc, parsed bond0 or None, bond0's IPv4 addr text, raw stdout)."""
    rc, out, err = hostssh.run(host_ip, user, pw, _STATUS, runner=runner)
    proc, _, addr = out.partition("===ADDR")
    return rc, (parse_bonding(proc) if rc == 0 else None), addr, out


def _bond_ok(b, addr_text, names, cidr) -> str | None:
    """None when bond0 is what ha-config wants, else why not."""
    if not b:
        return "no /proc/net/bonding/bond0"
    if b["mode"] != "active-backup":
        return f"mode {b['mode']!r}, not active-backup"
    if b["miimon"] != MIIMON:
        return f"MII polling {b['miimon']} ms, not {MIIMON}"
    for s in ("A", "B"):
        if names[s] not in b["slaves"]:
            return f"{names[s]} (wedge {s}) not a slave"
        if b["slaves"][names[s]]["mii"] != "up":
            return f"{names[s]} (wedge {s}) MII {b['slaves'][names[s]]['mii']}"
    if b["active"] != names["A"]:
        return f"active slave {b['active']}, not the primary {names['A']}"
    if f" {cidr.split('/')[0]}/" not in addr_text:
        return f"{cidr} not on bond0"
    return None


# ---------------------------------------------------------------- the rung
def _res(verdict, why, det, rows, arts) -> RungResult:
    extra = {"reason": why} if verdict == UNMEASURED else {}
    err = {"error": why} if verdict != "pass" else {}
    return RungResult(verdict, {**det, "headline": why, "rows": rows, **err, **extra}, arts)


def _names(host_ip, user, pw, macs, runner) -> tuple[dict | None, tuple | None]:
    """({side: installed kernel name}, None) for the fabric MACs, or (None, (verdict, why));
    a MAC on the mgmt NIC (enp94s0np0 / the host IP's link) refuses."""
    rc, out, err = hostssh.run(host_ip, user, pw,
                               "ip -o link show\necho ===ADDR\nip -4 -o addr show\n",
                               runner=runner)
    if unreachable(rc):
        return None, (UNMEASURED, f"{host_ip} did not answer ssh (rc={rc}): "
                                  f"{hostssh.tail('', err, 200)}")
    if rc != 0:
        return None, ("fail", f"reading links failed (rc={rc}): {hostssh.tail(out, err, 200)}")
    links_t, _, addr_t = out.partition("===ADDR")
    byname = parse_links(links_t)
    mgmt = {MGMT_IF, addr_iface(addr_t, host_ip)} - {None}
    names = {}
    for s, m in macs.items():
        hit = [n for n, x in byname.items() if x == m and not n.startswith("bond")]
        if not hit:
            return None, ("fail", f"wedge-{s} port {m} not on the installed OS")
        if set(hit) & mgmt:
            return None, ("fail", f"refusing: wedge-{s} MAC {m} is the mgmt NIC "
                                  f"{'/'.join(sorted(set(hit) & mgmt))}")
        names[s] = hit[0]
    return names, None


def bring_up(host_ip: str, user: str, pw: str, macs: list[str], *,
             runner=subprocess.run) -> tuple[dict | None, str | None]:
    """`ip link set <port> up` for each fabric port, found by permanent MAC (never the
    mgmt NIC): an installer leaves unconfigured ports admin-down, and a down port sends
    no LLDP for the wedges to see. ({mac: name}, None) or (None, why)."""
    names, bad = _names(host_ip, user, pw, {m: m for m in macs}, runner)
    if bad:
        return None, bad[1].replace("wedge-", "fabric ")
    script = "".join(f"ip link set dev {shlex.quote(n)} up\n" for n in names.values())
    rc, out, err = hostssh.run(host_ip, user, pw, script + "echo UP\n", runner=runner)
    if rc != 0 or "UP" not in out:
        return None, f"ip link set up failed (rc={rc}): {hostssh.tail(out, err, 200)}"
    return names, None


def _apply_script(names, macs, units) -> str:
    q = shlex.quote
    pat = "|".join(re.escape(x) for x in (names["A"], names["B"], macs["A"], macs["B"]))
    retire = " ".join(q(f"{NETPLAN_RUN}/10-netplan-{n}") for n in
                      ("bond0.netdev", "bond0.network", f"{names['A']}.network",
                       f"{names['B']}.network"))
    writes = "".join(f"""cat > {q(f'{UNIT_DIR}/.{f}.new')} <<'FLAX_BOND_EOF'
{body}FLAX_BOND_EOF
mv -f {q(f'{UNIT_DIR}/.{f}.new')} {q(f'{UNIT_DIR}/{f}')}
""" for f, body in units.items())
    links = " ".join(q(x) for x in ("bond0", names["A"], names["B"]))
    return f"""umask 022
other=$(grep -lisE {q(pat)} /etc/netplan/*.yaml 2>/dev/null | grep -vxF {q(NETPLAN)})
if [ -n "$other" ]; then echo "NETPLANDEF $other"; exit 3; fi
if [ -e {q(NETPLAN)} ]; then
  mkdir -p {q(NETPLAN_RETIRED.rsplit('/', 1)[0])} && mv -f {q(NETPLAN)} {q(NETPLAN_RETIRED)}
  rm -f {retire}
  echo RETIRED
fi
mkdir -p {q(UNIT_DIR)}
{writes}networkctl reload && timeout 120 networkctl reconfigure {links} && echo APPLIED
# networkd configures the links asynchronously after reconfigure: poll (<= 20 s) until each
# reports one of our files (read once, right away, bond0 still showed "n/a" on all five nodes)
for l in {links}; do
  f=; for i in $(seq 1 40); do
    f=$(networkctl status "$l" --no-pager 2>/dev/null | sed -n 's/^ *Network File: //p')
    case "$f" in {q(UNIT_DIR)}/*) break ;; esac; sleep 0.5
  done
  echo "NETFILE $l $f"
done
"""


def configure(host_ip: str, user: str, pw: str, side: dict, cidr: str, *,
              runner=subprocess.run, pinger=bang_ping, sleep=time.sleep,
              clock=time.monotonic, settle_s: int = SETTLE_S) -> RungResult:
    """side: A/B -> {"mac", "link", ...} (Deploy's sides); cidr: the A-side fabric
    address. One RungResult; artifacts = the units, bond0 status, the mgmt ping."""
    macs = {s: side[s]["mac"] for s in ("A", "B")}
    det = {"ha_mode": HA_MODE, "address": cidr, "macs": macs, "names": None,
           "mgmt_ping": None, "bond": None,
           "notes": [f"primary = the port LLDP saw on wedge A ({side['A'].get('link')}, "
                     f"{macs['A']}); never the mgmt NIC {MGMT_IF}",
                     f"written as systemd-networkd units in {UNIT_DIR} (no netplan generate: "
                     "the mgmt NIC's config is never rewritten)"]}
    rows, arts = [], {}
    # 1. the installed OS's names for the fabric MACs; refuse anything mgmt
    names, bad = _names(host_ip, user, pw, macs, runner)
    if bad:
        return _res(bad[0], bad[1], det, rows, arts)
    det["names"] = names
    rows += [["primary (wedge A)", f"{names['A']} {macs['A']}"],
             ["backup (wedge B)", f"{names['B']} {macs['B']}"],
             ["bond0", f"active-backup, miimon {MIIMON} ms, {cidr}"]]
    units = unit_files(names, macs, cidr)
    arts.update(units)
    # 2. write the units + reload/reconfigure bond0 and its slaves while the bang pings
    # the mgmt IP. Never `netplan generate/apply`: they rewrite/reconfigure the mgmt NIC.
    ping = pinger(host_ip)
    try:
        sleep(1)
        rc, out, err = hostssh.run(host_ip, user, pw, _apply_script(names, macs, units),
                                   timeout=180, runner=runner)
        sleep(2)
    finally:
        ptext = ping.stop()
    arts["mgmt-ping.txt"] = ptext
    pg = parse_ping(ptext)
    det["mgmt_ping"] = pg
    rows.append(["mgmt ping across apply",
                 f"max gap {pg['max_gap_ms']} ms, {pg['lost']}/{pg['sent']} lost"])
    m = re.search(r"^NETPLANDEF (.+)$", out, re.M)
    if m:
        return _res("fail", f"netplan also configures {names['A']}/{names['B']} "
                            f"({m.group(1).strip()}): its units would win over bond0's; "
                            "nothing written", det, rows, arts)
    if "RETIRED" in out:
        det["notes"].append(f"rackpost's earlier {NETPLAN} retired to {NETPLAN_RETIRED}")
    if "APPLIED" not in out:
        why = ("ssh session dropped during the bond apply" if unreachable(rc)
               else f"networkctl reload/reconfigure failed (rc={rc}): {hostssh.tail(out, err, 300)}")
        return _res("fail", why, det, rows, arts)
    want = {"bond0": "60-bond0.network", names["A"]: slave_unit("A"), names["B"]: slave_unit("B")}
    got = parse_netfiles(out)
    for link, f in want.items():
        if got.get(link) != f"{UNIT_DIR}/{f}":
            return _res("fail", f"{link} is configured by {got.get(link) or 'no network file'}, "
                                f"not {UNIT_DIR}/{f}", det, rows, arts)
    if not pg["received"] or pg["max_gap_ms"] > MGMT_GAP_MS:
        return _res("fail", f"mgmt {host_ip} lost {pg['max_gap_ms']} ms across the bond apply "
                    f"(> {MGMT_GAP_MS} ms)" if pg["received"]
                    else f"mgmt {host_ip} never answered the bang's ping", det, rows, arts)
    # 3. verify bond0
    t0, why, raw = clock(), "not read", ""
    while True:
        rc, b, addr, raw = status(host_ip, user, pw, runner=runner)
        if unreachable(rc):
            why = f"{host_ip} did not answer ssh reading bond0 (rc={rc})"
        else:
            det["bond"] = b
            why = _bond_ok(b, addr, names, cidr)
        if why is None or clock() - t0 >= settle_s:
            break
        sleep(1)
    arts["bond0.txt"] = raw
    if why:
        return _res("fail", f"bond0: {why}", det, rows, arts)
    rows.append(["active slave", f"{names['A']} (primary)"])
    return _res("pass", f"bond0 {cidr} active {names['A']} (wedge A)", det, rows, arts)
