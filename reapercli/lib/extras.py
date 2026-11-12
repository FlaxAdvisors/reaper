"""Site-level report sections from the VIP holder: firing Prometheus alerts, kea
counts, and switch-sense's last read per switch (spec §7.1, per site).

One extra ssh call; never a DB tunnel (psql runs inside the postgres container)."""
import json

from reapercli.lib.out import Row

SWITCH_WARN, SWITCH_FAIL = 600, 3600

SCRIPT = r"""
echo '##ALERTS'; curl -s -m 5 localhost:9090/api/v1/alerts; echo
echo '##SQL'
docker exec flax-stack-postgres-1 psql -U postgres -d flax -tA \
  -c "select 'lease4', count(*) from kea.lease4 where state = 0 and expire > now()" \
  -c "select 'hosts', count(*) from kea.hosts" \
  -c "select 'switch', switch, reachable, extract(epoch from now() - polled_at)::int from switch_facts order by 2"
"""


def parse_extras(text: str, site: str) -> list:
    parts = dict(p.split("\n", 1) if "\n" in p else (p, "") for p in text.split("##")[1:])
    rows = []
    try:
        alerts = json.loads(parts.get("ALERTS", "").strip() or "{}")["data"]["alerts"]
        # A site's Prometheus federates the others: keep this site's alerts (or unlabelled
        # ones), one row per alertname, with how many fired and on which bangs.
        firing = [a.get("labels", {}) for a in alerts if a.get("state") == "firing"
                  and a.get("labels", {}).get("site", site) == site]
        groups = {}
        for lab in firing:
            groups.setdefault(lab.get("alertname", "?"), []).append(lab)
        for name, labs in sorted(groups.items()):
            bangs = sorted({l.get("bang") or l.get("instance", "") for l in labs} - {""})
            rows.append(Row("alerts", f"alert {name}",
                            "FAIL" if any(l.get("severity") == "critical" for l in labs) else "warn",
                            f"x{len(labs)} {', '.join(bangs)}".strip(), site=site))
        if not firing:
            rows.append(Row("alerts", "alerts", "ok", "none firing", site=site))
    except (ValueError, KeyError, TypeError):
        rows.append(Row("alerts", "alerts", "warn", "prometheus did not answer", site=site))
    for line in parts.get("SQL", "").splitlines():
        f = line.split("|")
        if f[0] == "lease4" and len(f) == 2:
            rows.append(Row("kea", "kea leases v4", "info", f"{f[1]} active", site=site))
        elif f[0] == "hosts" and len(f) == 2:
            rows.append(Row("kea", "kea reservations", "info", f"{f[1]} rows", site=site))
        elif f[0] == "switch" and len(f) == 4:
            age = int(f[3] or -1)
            status = ("FAIL" if f[2] != "t" or age < 0 or age > SWITCH_FAIL
                      else "warn" if age > SWITCH_WARN else "ok")
            rows.append(Row("switches", f"switch {f[1]}", status,
                            f"{'reachable' if f[2] == 't' else 'UNREACHABLE'}, read {age}s ago", site=site))
    return rows
