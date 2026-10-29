"""Fake order context for the headless UI check (`RACKPOST_FAKE=1 python -m flax_rack serve`).

Never used in production. The real run_discover runs against a canned mgmt-switch
FDB synthesised from the geometry (locally-administered 02:... MACs, BMC = host+2),
an in-memory Kea and an always-up BMC. C37's host MAC is held by a "manual" Kea
row, so C37 fails its reserve rung and everything else passes. One stray MAC
sits on the first mgmt port no slot or uplink claims, so the unexpected-MAC
alert renders too.
"""
import subprocess

from .creds import Creds
from .kea_client import KeaConflict
from .stages import OrderContext

FAIL_POS = "C37"
def _mac(ou: int, col, n: int) -> str:
    return f"02:fa:{ou:02x}:{col or 0:02x}:00:{n:02x}"


def canned_fdb(g) -> tuple[str, set[str]]:
    """(SONiC `show mac` text, MACs the fake Kea refuses)."""
    mgmt = {s.id for s in g.switches if s.role == "mgmt"}
    rows, refuse = [], set()
    for d in g.devices:
        for name, l in sorted(d.links.items()):
            if l.switch not in mgmt:
                continue
            if name == "mezz0":
                host, bmc = _mac(d.ou, d.col, 0x10), _mac(d.ou, d.col, 0x12)
                rows += [(host, l.port), (bmc, l.port)]
                if d.pos == FAIL_POS:
                    refuse.add(host.replace(":", ""))
            elif name.startswith("iom"):
                rows.append((_mac(d.ou, d.col, 0x20 + int(name[3:])), l.port))
    claimed = {l.port for d in g.devices for l in d.links.values() if l.switch in mgmt}
    claimed |= {p for s in g.switches if s.id in mgmt for p in s.uplink_ports}
    stray = next((p for p in range(1, 49) if p not in claimed), None)
    if stray is not None:
        rows.append(("02:fa:ff:ff:00:01", stray))
    lines = ["  No.    Vlan  MacAddress         Port        Type",
             "-----  ------  -----------------  ----------  -------"]
    for i, (mac, port) in enumerate(rows, 1):
        lines.append(f"{i:5d}    1000  {mac.upper()}  Ethernet{port - 1:<4d} Dynamic")
    lines.append(f"Total number of entries {len(rows)}")
    return "\n".join(lines) + "\n", refuse


class FakeKea:
    def __init__(self, refuse=()):
        self.rows: dict[str, dict] = {}
        self.refuse = set(refuse)

    def upsert(self, source, mac, ipv4, hostname, vid):
        if mac in self.refuse:
            raise KeaConflict("mac held by a non-order (manual) row")
        self.rows[mac] = {"source": source, "ipv4": ipv4, "hostname": hostname, "vid": vid}

    def delete_source(self, source):
        gone = [m for m, r in self.rows.items() if r["source"] == source]
        for m in gone:
            del self.rows[m]
        return len(gone)

    def list(self, source):
        return [dict(r, mac=m) for m, r in self.rows.items() if r["source"] == source]


_KEAS: dict[str, FakeKea] = {}


def fake_context_factory(rack_dir, g, db) -> OrderContext:
    text, refuse = canned_fdb(g)
    kea = _KEAS.setdefault(g.kea_source, FakeKea(refuse))

    def runner(argv, **kw):
        out = ""
        if argv[0] == "sshpass":
            out = text
        elif argv[0] == "ipmitool":
            out = "Chassis Power is on\n"
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")

    return OrderContext(rack_dir, g, db, kea, Creds(("u", "p"), [("u", "p")]), runner)
