"""HA rows: VIP, keepalived, and the Postgres pair (spec §7 infra).

The drift trap this watches for (runbooks/postgres-ha-and-db-updates.md): a VRRP
flap promotes the standby, which then silently diverges. Two primaries is the
loudest row here.
"""
from reapercli.lib.out import UNREACHABLE, Row

LAG_WARN = 16 * 2**20
SLOT_WARN, SLOT_FAIL = 2**30, 5 * 2**30


def _dead(f: dict) -> str:
    return f.get("error", "") if "error" in f else ""


def ha_rows(sh, facts: dict) -> list:
    site = sh.site
    rows = []

    def row(item, status, value="", host=""):
        rows.append(Row("ha", item, status, value, host, site))

    holders = [b for b, f in facts.items() if f.get("vip")]
    unknown = [b.name for b in sh.bangs if facts.get(b.name, {}).get("unreachable")]
    if not holders and unknown:     # the holder may be the bang reaper cannot ask
        row("vip", UNREACHABLE, f"unknown: cannot ask {', '.join(unknown)}")
    elif len(holders) == 1:
        h = holders[0]
        row("vip", "ok" if h == sh.primary else "warn",
            h if h == sh.primary else f"on standby {h} (declared primary {sh.primary})")
    else:
        row("vip", "FAIL", "no VIP holder" if not holders else f"VIP on {', '.join(holders)}")

    pg = {}
    for b in sh.bangs:
        f = facts.get(b.name, {})
        if f.get("dormant"):
            row("keepalived", "dormant", "dormant", b.name)
            continue
        if _dead(f):
            row("keepalived", UNREACHABLE if f.get("unreachable") else "FAIL", _dead(f), b.name)
            continue
        ka = (f.get("keepalived") or {}).get("state", "?")
        row("keepalived", "ok" if ka == "active" else "FAIL", ka, b.name)
        p = f.get("pg") or {}
        if "error" in p:
            row("pg-role", "FAIL", p["error"], b.name)
        else:
            pg[b.name] = p

    primaries = [b for b, p in pg.items() if not p.get("in_recovery")]
    for b, p in pg.items():
        role = "standby" if p.get("in_recovery") else "primary"
        split = role == "primary" and len(primaries) > 1
        row("pg-role", "FAIL" if split else "ok",
            f"primary (split: standby promoted; {len(primaries)} primaries)" if split else role, b)

    if pg and not primaries:
        row("pg-role", "FAIL", "no primary: every reachable postgres is in recovery")
    declared = sh.standby[0] if sh.standby else None
    if declared and facts.get(declared, {}).get("dormant"):
        row("replication", "warn", f"standby dormant, no HA ({declared})")
        _slot(row, sh.primary, pg.get(sh.primary))
        return rows
    if len(primaries) != 1:
        return rows
    prim_name = primaries[0]
    prim = pg[prim_name]
    _slot(row, prim_name, prim)          # always: a slot pinning WAL matters most when the standby is gone
    # the live standby is whichever other bang is in recovery (roles swap after a real failover)
    others = [b.name for b in sh.bangs if b.name != prim_name and not b.dormant]
    live = [b for b in others if b in pg and pg[b].get("in_recovery")]
    if not live:
        missing = [b for b in others if b not in pg]
        cut = [b for b in missing if b in unknown]
        row("replication", UNREACHABLE if cut and cut == missing else "FAIL",
            f"standby {', '.join(missing)}: " + ("unreachable" if cut == missing else "no facts")
            if missing else "no standby")
        return rows
    stby_name, stby = live[0], pg[live[0]]
    ip = next(x.mgmt_ip for x in sh.bangs if x.name == stby_name)
    sender = [r for r in prim.get("replication", []) if r.get("addr") == ip]
    ok = sender and sender[0].get("state") == "streaming" and stby.get("wal_receiver") == "streaming"
    row("replication", "ok" if ok else "FAIL",
        f"streaming to {stby_name}" if ok else f"sender={sender[0]['state'] if sender else 'none'} "
                                               f"receiver={stby.get('wal_receiver', 'n/a')}")
    lag = max(int(stby.get("replay_lag_bytes") or 0), int(sender[0]["lag_bytes"]) if sender else 0)
    row("lag", "ok" if lag <= LAG_WARN else "warn", f"{lag} B")
    heads = {b: p.get("alembic") for b, p in pg.items()}
    row("alembic", "ok" if len(set(heads.values())) == 1 else "FAIL",
        " ".join(sorted(set(map(str, heads.values())))))
    return rows


def _slot(row, host, p):
    if not p or p.get("in_recovery"):
        return
    b = int(p.get("slot_retained_bytes") or 0)
    status = "ok" if b <= SLOT_WARN else "warn" if b <= SLOT_FAIL else "FAIL"
    row("slot-wal", status, f"{b / 2**30:.2f} GiB retained", host)
