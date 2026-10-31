# filter_plugins/sonic_config.py
"""Push-side half of the SONiC config_db round trip (playbooks/apply_config.yml).

    fetch  switch --> redact_secrets --> config/config_db.json   ({{ vault_cfg_x }})
    push   config/config_db.json --> sonic_render --> switch     (real value)

Why not Jinja, as EOS/IOS get via `src:`: config_db.json is JSON, and
 - Task-1 redaction puts a placeholder inside a KEY (the SNMP_COMMUNITY name);
 - a vault value spliced in as text breaks the JSON if it holds `"` or `\\`;
 - config_db can legitimately carry `{{` / `{%` (bgpraw, banners, descriptions),
   which Jinja would try to evaluate.
So the file is parsed, and only the exact `{{ vault_cfg_<name> }}` placeholders
redact_secrets emits are substituted, in keys and string values, at any depth.
Every other byte is data. A placeholder with no vault value RAISES (fail closed),
as does anything still looking like `{{ vault_cfg_...` after substitution.

sonic_config_diff compares a running config_db to the rendered one and returns
a diff that is safe to print or write to .check_result.json: both sides are
masked with redact_secrets' own rules -- a secret equal to its vault value shows
as the placeholder, any other secret as `<vault_cfg_x sha256:...>` (hash, never
the value). `set` = leaves `config load` will write; `unset` = leaves only the
switch has, which `config load` (a merge) does NOT remove.

Filters:
  sonic_placeholders(text)              -> sorted vault var names referenced
  sonic_render(text, {var: value})      -> rendered config_db JSON text
  sonic_config_diff(running, rendered, {var: value})
      -> {changed, diff, set, unset, changed_ports}
"""
import difflib
import hashlib
import importlib.util
import json
import os
import re

_spec = importlib.util.spec_from_file_location(
    "_reaper_redact_secrets",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "redact_secrets.py"))
_redact_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_redact_mod)

_PLACEHOLDER_RE = re.compile(r"\{\{\s*(vault_cfg_[A-Za-z0-9_]+)\s*\}\}")
_LEFTOVER_RE = re.compile(r"\{\{\s*vault_cfg_")
_PORT_RE = re.compile(r"^(Ethernet\d+|PortChannel\d+|eth\d+)$")


def _load(doc, what):
    if isinstance(doc, dict):
        return doc
    if not isinstance(doc, str):
        raise ValueError("sonic_config: %s is not JSON text" % what)
    try:
        db = json.loads(doc.replace("\r", ""))
    except ValueError as e:
        raise ValueError("sonic_config: %s did not parse as JSON: %s" % (what, e))
    if not isinstance(db, dict):
        raise ValueError("sonic_config: %s is not a JSON object" % what)
    return db


def _walk(obj, fn):
    """Apply fn to every key and string value, keeping key order."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            nk = fn(k) if isinstance(k, str) else k
            if nk in out:
                raise ValueError("sonic_config: two keys render to the same "
                                 "name (check the vault values behind %r)" % k)
            out[nk] = _walk(v, fn)
        return out
    if isinstance(obj, list):
        return [_walk(v, fn) for v in obj]
    if isinstance(obj, str):
        return fn(obj)
    return obj


def sonic_placeholders(doc):
    names = set()

    def grab(s):
        names.update(_PLACEHOLDER_RE.findall(s))
        return s
    _walk(_load(doc, "config_db"), grab)
    return sorted(names)


def sonic_render(doc, values):
    values = values or {}

    def sub(s):
        def one(m):
            name = m.group(1)
            if name not in values or values[name] is None:
                raise ValueError("sonic_render: no vault value for %s" % name)
            return str(values[name])
        out = _PLACEHOLDER_RE.sub(one, s)
        if _LEFTOVER_RE.search(out):
            raise ValueError("sonic_render: unrecognised vault placeholder in %r"
                             % s[:80])
        return out
    return json.dumps(_walk(_load(doc, "config_db"), sub), indent=4) + "\n"


def _mask(db, values):
    values = values or {}

    def ph(var, secret):
        if secret is not None and values.get(var) is not None \
                and str(values[var]) == secret:
            return "{{ %s }}" % var
        digest = hashlib.sha256((secret or "").encode()).hexdigest()[:12]
        return "<%s sha256:%s>" % (var, digest)
    return json.loads(_redact_mod._redact(json.dumps(db), ph))


def _leaves(db):
    out = {}
    for table, rows in db.items():
        if not isinstance(rows, dict):
            out[(table,)] = rows
            continue
        for key, row in rows.items():
            if not isinstance(row, dict):
                out[(table, key)] = row
                continue
            if not row:
                out[(table, key)] = {}
            for field, value in row.items():
                out[(table, key, field)] = value
    return out


def _path(p):
    """TABLE|key field -- SONiC keys already use `|`, so the field goes after a space."""
    return "|".join(p[:2]) + (" " + p[2] if len(p) > 2 else "")


def _ports(paths):
    ports = set()
    for p in paths:
        if len(p) > 1:
            ports.update(part for part in p[1].split("|") if _PORT_RE.match(part))
    return sorted(ports)


def sonic_config_diff(running, rendered, values=None):
    run = _mask(_load(running, "running config_db"), values)
    want = _mask(_load(rendered, "rendered config_db"), values)
    lr, lw = _leaves(run), _leaves(want)
    set_paths = sorted(p for p in lw if p not in lr or lr[p] != lw[p])
    unset_paths = sorted(p for p in lr if p not in lw)
    diff = "".join(difflib.unified_diff(
        json.dumps(run, indent=4, sort_keys=True).splitlines(True),
        json.dumps(want, indent=4, sort_keys=True).splitlines(True),
        "running", "rendered"))
    return {
        "changed": bool(set_paths or unset_paths),
        "diff": diff,
        "set": [_path(p) for p in set_paths],
        "unset": [_path(p) for p in unset_paths],
        "changed_ports": _ports(set_paths + unset_paths),
    }


class FilterModule(object):
    def filters(self):
        return {
            "sonic_placeholders": sonic_placeholders,
            "sonic_render": sonic_render,
            "sonic_config_diff": sonic_config_diff,
        }
