"""ha-config: bond0 (active-backup) over the two PCIe fabric ports of the installed OS.

The ports are found BY MAC (Qualify's ha-path links + the live image's MACs), never by
name; the primary is the port LLDP saw on wedge A -- not the P0 label (R05 is
cross-cabled). /etc/netplan/60-bond0.yaml (renderer networkd) keys each port by the
kernel name its MAC carries on the installed OS (so it merges over an installer entry
for the same name, e.g. `dhcp4: true`) and matches it by `macaddress`; no set-name.
The bond's address is the device's A-side fabric formula (fabric.fabric_addr "A").
`netplan generate && netplan apply`; a generate failure restores the previous file.

The mgmt NIC (the mezz enp94s0np0, the one holding the host IP) is never in the file:
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

NETPLAN = "/etc/netplan/60-bond0.yaml"
MGMT_IF = "enp94s0np0"
MIIMON = 100
PING_MS = 100
MGMT_GAP_MS = 1000
SETTLE_S = 30
HA_MODE = "active-backup (no MLAG peer link)"


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


def netplan_yaml(names: dict[str, str], macs: dict[str, str], cidr: str) -> str:
    """names/macs: side -> installed kernel name / MAC. Primary = side A."""
    eth = "".join(f"""    {names[s]}:
      match:
        macaddress: "{macs[s]}"
      dhcp4: false
      dhcp6: false
      accept-ra: false
""" for s in ("A", "B"))
    return f"""# rackpost Deploy (ha-config): bond0 active-backup, primary = the wedge-A port
network:
  version: 2
  renderer: networkd
  ethernets:
{eth}  bonds:
    bond0:
      interfaces: [{names['A']}, {names['B']}]
      addresses: [{cidr}]
      dhcp4: false
      dhcp6: false
      accept-ra: false
      parameters:
        mode: active-backup
        mii-monitor-interval: {MIIMON}
        primary: {names['A']}
"""


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


def configure(host_ip: str, user: str, pw: str, side: dict, cidr: str, *,
              runner=subprocess.run, pinger=bang_ping, sleep=time.sleep,
              clock=time.monotonic, settle_s: int = SETTLE_S) -> RungResult:
    """side: sides()[0] (A/B -> {"mac", "link", "ifname"}); cidr: the A-side fabric
    address. One RungResult; artifacts = the YAML, bond0 status, the mgmt ping."""
    macs = {s: side[s]["mac"] for s in ("A", "B")}
    det = {"ha_mode": HA_MODE, "address": cidr, "macs": macs, "names": None,
           "mgmt_ping": None, "bond": None,
           "notes": [f"primary = the port LLDP saw on wedge A ({side['A'].get('link')}, "
                     f"{macs['A']}); never the mgmt NIC {MGMT_IF}"]}
    rows, arts = [], {}
    # 1. the installed OS's names for the fabric MACs; refuse anything mgmt
    rc, out, err = hostssh.run(host_ip, user, pw,
                               "ip -o link show\necho ===ADDR\nip -4 -o addr show\n",
                               runner=runner)
    if unreachable(rc):
        return _res(UNMEASURED, f"{host_ip} did not answer ssh (rc={rc}): "
                    f"{hostssh.tail('', err, 200)}", det, rows, arts)
    if rc != 0:
        return _res("fail", f"reading links failed (rc={rc}): {hostssh.tail(out, err, 200)}",
                    det, rows, arts)
    links_t, _, addr_t = out.partition("===ADDR")
    byname = parse_links(links_t)
    mgmt = {MGMT_IF, addr_iface(addr_t, host_ip)} - {None}
    names = {}
    for s in ("A", "B"):
        hit = [n for n, m in byname.items() if m == macs[s] and not n.startswith("bond")]
        if not hit:
            return _res("fail", f"wedge-{s} port {macs[s]} not on the installed OS",
                        det, rows, arts)
        if set(hit) & mgmt:
            return _res("fail", f"refusing: wedge-{s} MAC {macs[s]} is the mgmt NIC "
                        f"{'/'.join(sorted(set(hit) & mgmt))}", det, rows, arts)
        names[s] = hit[0]
    det["names"] = names
    rows += [["primary (wedge A)", f"{names['A']} {macs['A']}"],
             ["backup (wedge B)", f"{names['B']} {macs['B']}"],
             ["bond0", f"active-backup, miimon {MIIMON} ms, {cidr}"]]
    yaml = netplan_yaml(names, macs, cidr)
    arts["60-bond0.yaml"] = yaml
    # 2. write + generate + apply while the bang pings the mgmt IP
    q = shlex.quote(NETPLAN)
    script = f"""umask 077
[ -e {q} ] && cp -p {q} {q}.prev
cat > {q}.new <<'FLAX_BOND_EOF'
{yaml}FLAX_BOND_EOF
chmod 600 {q}.new && mv -f {q}.new {q}
if ! netplan generate; then
  if [ -e {q}.prev ]; then mv -f {q}.prev {q}; else rm -f {q}; fi
  netplan generate
  echo GENFAIL; exit 3
fi
rm -f {q}.prev
timeout 120 netplan apply && echo APPLIED
"""
    ping = pinger(host_ip)
    try:
        sleep(1)
        rc, out, err = hostssh.run(host_ip, user, pw, script, timeout=180, runner=runner)
        sleep(2)
    finally:
        ptext = ping.stop()
    arts["mgmt-ping.txt"] = ptext
    pg = parse_ping(ptext)
    det["mgmt_ping"] = pg
    rows.append(["mgmt ping across apply",
                 f"max gap {pg['max_gap_ms']} ms, {pg['lost']}/{pg['sent']} lost"])
    if "GENFAIL" in out:
        return _res("fail", f"netplan generate rejected 60-bond0.yaml (previous restored): "
                    f"{hostssh.tail('', err, 300)}", det, rows, arts)
    if "APPLIED" not in out:
        why = ("ssh session dropped during netplan apply" if unreachable(rc)
               else f"netplan apply failed (rc={rc}): {hostssh.tail(out, err, 300)}")
        return _res("fail", why, det, rows, arts)
    if not pg["received"] or pg["max_gap_ms"] > MGMT_GAP_MS:
        return _res("fail", f"mgmt {host_ip} lost {pg['max_gap_ms']} ms across netplan apply "
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
