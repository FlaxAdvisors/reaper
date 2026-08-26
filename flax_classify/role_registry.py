"""Role registry (spine migration phase 1, spec 2026-07-03-flax-spine-migration).

Loads /etc/flax/roles.d/<role>.json, validates universes, and resolves
(switch, port) -> role with precedence: port > switch > prefix > catch_all.
Strict on malformed input (RegistryError) -- a wrong registry must never
publish; missing dir returns {} so callers can fall back to legacy
phase_for (phase-1 deploy-order safety, removed in phase 3).
"""
import json
import logging
import os

from .vlan_policy import _norm_rabbit_token, load_phase_geometry

log = logging.getLogger("flax-classify.role_registry")

DEFAULT_ROLES_DIR = "/etc/flax/roles.d"
_UNIVERSE_KEYS = {"switches", "switch_prefixes", "ports", "ports_from", "catch_all"}


class RegistryError(ValueError):
    """Registry content is invalid; refuse to use/publish it."""


def load_role_dir(path):
    if not os.path.isdir(path):
        return {}
    defs = {}
    for name in sorted(os.listdir(path)):
        if not name.endswith(".json"):
            continue
        fpath = os.path.join(path, name)
        try:
            with open(fpath) as fh:
                data = json.load(fh)
        except (OSError, ValueError) as exc:
            raise RegistryError(f"{fpath}: unreadable/malformed ({exc})")
        if not isinstance(data, dict):
            raise RegistryError(f"{fpath}: top-level JSON must be an object")
        role = data.get("role")
        if role != name[:-len(".json")]:
            raise RegistryError(f"{fpath}: 'role' key {role!r} != filename stem")
        uni = data.get("universe")
        if not isinstance(uni, dict):
            raise RegistryError(f"{fpath}: missing 'universe' object")
        unknown = set(uni) - _UNIVERSE_KEYS
        if unknown:
            raise RegistryError(f"{fpath}: unknown universe keys {sorted(unknown)}")
        ports = {}
        for sw, plist in (uni.get("ports") or {}).items():
            ports[sw] = {_norm_rabbit_token(p) for p in plist}
        if uni.get("ports_from"):
            # Triage's port set is still sourced from the legacy geometry.json,
            # but filed SWITCH-SCOPED (under the switch each entry records, e.g.
            # rabbit-gouda) -- NOT a switch-agnostic bucket. Otherwise a triage
            # token hijacks the same port number on a post switch (rabbit-edam),
            # beating post's switch claim via port>switch precedence.
            for sw, toks in load_phase_geometry(uni["ports_from"]).items():
                ports.setdefault(sw, set()).update(toks)
        data["_ports"] = ports
        defs[role] = data
    return defs


def validate_roles(defs):
    seen_switch, seen_prefix, seen_port, catch_alls = {}, {}, {}, []
    for role, d in defs.items():
        uni = d["universe"]
        for sw in uni.get("switches") or []:
            if sw in seen_switch:
                raise RegistryError(f"switch {sw} claimed by {seen_switch[sw]} and {role}")
            seen_switch[sw] = role
        for pref in uni.get("switch_prefixes") or []:
            if pref in seen_prefix:
                raise RegistryError(f"prefix {pref} claimed by {seen_prefix[pref]} and {role}")
            seen_prefix[pref] = role
        for sw, toks in d["_ports"].items():
            for tok in toks:
                key = (sw, tok)
                if key in seen_port:
                    raise RegistryError(f"port {key} claimed by {seen_port[key]} and {role}")
                seen_port[key] = role
        if uni.get("catch_all"):
            catch_alls.append(role)
    if len(catch_alls) > 1:
        raise RegistryError(f"multiple catch_all roles: {catch_alls}")


def resolve_role(defs, switch, port):
    tok = _norm_rabbit_token(port)
    catch_all = None
    # port claims (most specific)
    for role, d in defs.items():
        for sw, toks in d["_ports"].items():
            if tok in toks and sw == switch:
                return role
    for role, d in defs.items():
        if switch in (d["universe"].get("switches") or []):
            return role
    for role, d in defs.items():
        for pref in d["universe"].get("switch_prefixes") or []:
            if switch.startswith(pref):
                return role
    for role, d in defs.items():
        if d["universe"].get("catch_all"):
            catch_all = role
    return catch_all


def _public_def(d):
    """Strip the private _ports index before it reaches jsonb storage."""
    return {k: v for k, v in d.items() if k != "_ports"}


def publish_roles(pool, defs):
    """Full-replace roles/role_universe from ``defs`` (load_role_dir output)
    in one transaction.

    Idempotent: if every role's stored definition (minus _ports) already
    equals ``defs``, skip the write and return the current generation
    unchanged. Otherwise bump to current-max + 1 and rewrite both tables.
    Flattens each role's universe into role_universe rows: switch claims
    ('switch', sw, NULL), prefix claims ('prefix', pref, NULL), port claims
    ('port', switch, tok), and a catch_all marker
    ('catch_all', NULL, NULL).
    """
    with pool.connection() as conn:
        with conn.transaction():
            cur = conn.execute("SELECT role, definition, generation FROM roles")
            existing = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
            if (set(existing) == set(defs)
                    and all(existing[r][0] == _public_def(d) for r, d in defs.items())):
                return max((g for _, g in existing.values()), default=0)
            gen = max((g for _, g in existing.values()), default=0) + 1
            conn.execute("DELETE FROM role_universe")
            conn.execute("DELETE FROM roles")
            for role, d in defs.items():
                conn.execute(
                    "INSERT INTO roles (role, definition, generation) VALUES (%s, %s, %s)",
                    (role, json.dumps(_public_def(d)), gen))
                uni = d["universe"]
                for sw in uni.get("switches") or []:
                    conn.execute("INSERT INTO role_universe (role, kind, switch) "
                                 "VALUES (%s,'switch',%s)", (role, sw))
                for pref in uni.get("switch_prefixes") or []:
                    conn.execute("INSERT INTO role_universe (role, kind, switch) "
                                 "VALUES (%s,'prefix',%s)", (role, pref))
                for sw, toks in d["_ports"].items():
                    for tok in sorted(toks):
                        conn.execute("INSERT INTO role_universe (role, kind, switch, port) "
                                     "VALUES (%s,'port',%s,%s)", (role, sw, tok))
                if uni.get("catch_all"):
                    conn.execute("INSERT INTO role_universe (role, kind) "
                                 "VALUES (%s,'catch_all')", (role,))
        return gen
