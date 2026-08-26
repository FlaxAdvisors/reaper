"""Pure view functions for the read-only /roles registry page.

DB-read only: this module never touches the filesystem (no /etc/flax reads,
no roles.d mount) and imports nothing from flax_classify — it only shapes
rows already fetched from the `roles` / `role_universe` tables (queries.py)
into template-ready structures. Kept import-free of flax_classify so
flax_control never needs a cross-package dependency for a read-only view.
"""

def _norm_port_token(port: str) -> str:
    """Ethernet6/1 / et6b1 -> et6/1. Mirrors
    flax_classify.vlan_policy._norm_rabbit_token — duplicated here (not
    imported) to keep flax_control's view layer free of a cross-package
    dependency on flax_classify for a read-only page."""
    p = port.strip().lower().replace("ethernet", "et")
    if not p.startswith("et"):
        return p
    rest = p[2:]
    if "/" in rest:
        a, b = rest.split("/", 1)
    elif "b" in rest:
        a, b = rest.split("b", 1)
    else:
        return p
    try:
        return "et" + str(int(a)) + "/" + str(int(b))
    except ValueError:
        return p


def build_registry(rows_roles, rows_universe) -> dict:
    """Shape (roles, role_universe) rows into the /roles page model.

    rows_roles: [(role, definition:dict, generation, loaded_at), ...]
    rows_universe: [(role, kind, switch, port), ...]
        kind in {"switch", "prefix", "port", "catch_all"}.

    Returns {"roles": [...], "catch_all_role": str|None}. Empty input ->
    {"roles": [], "catch_all_role": None} (registry not yet published).
    """
    claims_by_role: dict = {}
    for role, kind, switch, port in rows_universe:
        claims_by_role.setdefault(role, []).append((kind, switch, port))

    catch_all_role = None
    roles_out = []
    for role, definition, generation, loaded_at in rows_roles:
        claims = claims_by_role.get(role, [])
        switches = sorted({sw for kind, sw, _ in claims if kind == "switch"})
        prefixes = sorted({sw for kind, sw, _ in claims if kind == "prefix"})
        port_count = sum(1 for kind, _, _ in claims if kind == "port")
        is_catch_all = any(kind == "catch_all" for kind, _, _ in claims)
        if is_catch_all:
            catch_all_role = role

        summary = []
        if switches:
            summary.append("switches: " + ", ".join(switches))
        if prefixes:
            summary.append("prefixes: " + ", ".join(prefixes))
        if port_count:
            summary.append(f"ports: {port_count} claims")
        if is_catch_all:
            summary.append("catch-all")

        roles_out.append({
            "name": role,
            "generation": generation,
            "loaded_at": loaded_at,
            "universe_summary": summary,
            "capabilities": (definition or {}).get("capabilities") or {},
            "policy": (definition or {}).get("policy") or {},
            "record_keys": (definition or {}).get("record_keys") or [],
        })

    roles_out.sort(key=lambda r: r["name"])
    return {"roles": roles_out, "catch_all_role": catch_all_role}


def coverage(rows_universe, switch_ports) -> list:
    """Ports covered by NO explicit claim (would resolve via catch_all only).

    rows_universe: [(role, kind, switch, port), ...] — same shape as
    build_registry(). switch_ports: [(switch, port), ...] live ports, e.g.
    from switch_facts. Precedence mirrors role_registry.resolve_role: a port
    claim on the exact switch OR on the switch-agnostic '*geometry*' token
    covers it; a switch claim covers every port on that switch; a prefix
    claim covers every port on switches starting with that prefix. Anything
    left over is the operator's would-be-unassigned live view.
    """
    switch_claims = {sw for _, kind, sw, _ in rows_universe if kind == "switch"}
    prefix_claims = [sw for _, kind, sw, _ in rows_universe if kind == "prefix"]
    port_claims = {(sw, port) for _, kind, sw, port in rows_universe if kind == "port"}

    uncovered = []
    for switch, port in switch_ports:
        tok = _norm_port_token(port)
        if (switch, tok) in port_claims or ("*geometry*", tok) in port_claims:
            continue
        if switch in switch_claims:
            continue
        if any(switch.startswith(pref) for pref in prefix_claims):
            continue
        uncovered.append((switch, port))
    return uncovered
