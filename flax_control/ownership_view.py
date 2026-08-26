"""Pure view functions for the /ownership roaming-ledger page.

Shapes mac_ownership_events rows (queries.py) only. Movement between roles
is NORMAL, wi-fi-like (spine spec) — the page stays visually calm about it;
the one fault signal is RAPID roaming: >= warn_threshold handoffs for a
single mac in 24h floats that mac into the "rapid roamers" strip.
"""


def build_ownership(event_rows, counts_24h, warn_threshold):
    events = [
        {"at": at, "mac": mac, "from_role": from_role, "to_role": to_role,
         "switch": switch, "port": port}
        for at, mac, from_role, to_role, switch, port in (event_rows or [])
    ]
    rapid = [{"mac": mac, "count": n} for mac, n in (counts_24h or [])
             if n >= warn_threshold]
    return {"rapid": rapid, "events": events,
            "warn_threshold": warn_threshold}
