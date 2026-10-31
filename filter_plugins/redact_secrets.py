# filter_plugins/redact_secrets.py
"""Redact cleartext secrets out of collected device config before it lands in
files/configs/.

RULE ZERO: files/configs/<site>/<host>/config/ is committed. Anything a `reaper
fetch` writes there is published. Devices hand us their running-config with
passwords and SNMP communities in the clear, so the collect_* roles pipe every
config-tree write through this filter: each known secret is replaced with the
Jinja placeholder for the vault var that holds its real value.

The round trip is:

    fetch  device --> redact_secrets --> files/configs/.../config/  ({{ vault_x }})
    push   files/configs/.../config/ --> Jinja render --> device     (real value)

Push-side rendering is free: arista.eos.eos_config / cisco.ios.ios_config accept
a Jinja2 template as `src`, and apply_jumphost templates (not copies) ifcfg-*.

`reaper drift` runs collect_config.yml with --check --diff, so the filter must
apply on that path too -- it does, because it transforms the *content* of the
copy task rather than running as a separate task. Redacted-vs-redacted compares
clean; without it drift would report a permanent phantom diff on every secret
line.

Idempotent: a line already carrying a placeholder is left alone, so re-running
over an already-redacted tree is a no-op.

Vault vars carry a `vault_cfg_` prefix: these are CONFIG CONTENT, not connection
credentials. eindhoven already had a `vault_cisco_enable_password` (the password
ansible types to reach enable mode = the plaintext of `enable secret 8`); the
type-0 `enable password` line this filter redacts is a different, IOS-ignored
value. Conflating the two would have pushed the wrong password to a switch.

Deliberately NOT redacted (operator decision, 2026-08-26): password *hashes* --
Cisco type-8 (`enable secret 8 $8$...`), Cisco type-7, and Arista
`secret sha512 $6$...`. They are not directly usable, and a wrong vault value
pushed into a password line locks us out of a remote lab switch. Only cleartext
is in scope. If that changes, add rules here and vault vars to every site.

SONiC (2026-10-31): roles/sonic writes `show runningconfiguration all` -- a
config_db JSON object, not line config -- to config/config_db.json, through the
EXPLICIT `redact_sonic_config_db` filter (never the generic sniff below): it
extracts the JSON object from the output (leading noise such as a stray stderr
line is dropped; no object, or anything but whitespace after it, RAISES), then
replaces known secret fields by placeholders (SNMP_COMMUNITY's secret is the
KEY), gives any other field whose NAME looks secret a per-table/field
placeholder, and runs the FRR text in `bgpraw` through the line rules. Output is
re-serialised as SONiC does (indent 4, key order kept). A value counts as
already redacted only when it is EXACTLY `{{ vault_cfg_<name> }}`; a real secret
that merely contains `{{` is still replaced.

`redact_secrets` itself keeps a backstop: text whose first non-blank byte is `{`
takes the same JSON path (and raises if it does not parse).
"""
import json
import re

# Cisco sub-modes whose `password <x>` line is the vty/console password.
_LINE_BLOCK_RE = re.compile(r"^line\s+(con|vty|aux)\b")
# Any non-indented line ends the sub-mode.
_TOP_LEVEL_RE = re.compile(r"^\S")

# A value that is already a placeholder -- leave it alone (idempotency). Only
# the exact form _ph emits counts: a secret that merely contains `{{` is still
# a secret. _AT_PLACEHOLDER_RE is matched where a line rule's secret starts (the
# line rules capture \S+, i.e. only the `{{` of a placeholder), optionally
# behind the quote _quoted keeps.
_PLACEHOLDER_RE = re.compile(r"^\{\{ vault_cfg_\w+ \}\}$")
_AT_PLACEHOLDER_RE = re.compile(r"""['"]?\{\{ vault_cfg_\w+ \}\}""")

# Cisco password type digit. Types 5/7/8/9 are hashed or (7) reversibly
# encrypted; we only touch the untyped -- i.e. genuinely cleartext -- form,
# so redaction never changes the encoding the device expects on push.
_TYPED_RE = re.compile(r"^\d+$")

# (regex, vault var). Each regex must capture: 1 = prefix, 2 = secret,
# 3 = suffix (may be empty).
_RULES = (
    # openSUSE ifcfg-wlan* on the bangs: WIRELESS_WPA_PSK='...'
    (re.compile(r"^(WIRELESS_WPA_PSK=)(\S*)(.*)$"), "vault_cfg_wlan_wpa_psk"),
    # Arista EOS running-config
    (re.compile(r"^(snmp-server community\s+)(\S+)(.*)$"), "vault_cfg_snmp_ro_community"),
    # Cumulus `net add` command dump
    (re.compile(r"^(net add snmp-server readonly-community\s+)(\S+)(.*)$"),
     "vault_cfg_snmp_ro_community"),
    # Cisco IOS global enable password (untyped only -- see _TYPED_RE)
    (re.compile(r"^(enable password\s+)(\S+)(.*)$"), "vault_cfg_cisco_enable_password"),
)

# Cisco line-block password, only applied inside a `line con|vty|aux` sub-mode.
_LINE_PASSWORD_RE = re.compile(r"^(\s+password\s+)(\S+)(.*)$")
_LINE_PASSWORD_VAR = "vault_cfg_cisco_vty_password"


def _quoted(original, placeholder):
    """Preserve the quoting style the device used (ifcfg uses single quotes)."""
    if len(original) >= 2 and original[0] == original[-1] and original[0] in "'\"":
        return "%s%s%s" % (original[0], placeholder, original[0])
    return placeholder


def _unquoted(original):
    if len(original) >= 2 and original[0] == original[-1] and original[0] in "'\"":
        return original[1:-1]
    return original


def _ph(var, secret=None):
    """Default replacement: the vault placeholder. `secret` is ignored here;
    sonic_config's display mask passes its own function, which uses it."""
    return "{{ %s }}" % var


def _apply(line, regex, var, ph=_ph):
    m = regex.match(line)
    if not m:
        return None
    prefix, secret, suffix = m.group(1), m.group(2), m.group(3)
    if (not secret or _AT_PLACEHOLDER_RE.match(line, m.start(2))
            or _TYPED_RE.match(secret)):
        return None
    return "%s%s%s" % (prefix, _quoted(secret, ph(var, _unquoted(secret))), suffix)


# --- SONiC config_db JSON -------------------------------------------------

# SNMP_COMMUNITY|<community> {"TYPE": "RO"|"RW"}: the community IS the key.
_SONIC_COMMUNITY_VAR = {"RO": "vault_cfg_snmp_ro_community",
                        "RW": "vault_cfg_snmp_rw_community"}

# (table, field) -> vault var, for every row of the table.
_SONIC_FIELDS = {
    ("SNMP_USER", "SNMP_USER_AUTH_PASSWORD"): "vault_cfg_snmp_user_auth_password",
    ("SNMP_USER", "SNMP_USER_ENCRYPTION_PASSWORD"): "vault_cfg_snmp_user_priv_password",
    ("TACPLUS", "passkey"): "vault_cfg_tacplus_passkey",
    ("TACPLUS_SERVER", "passkey"): "vault_cfg_tacplus_passkey",
    ("RADIUS", "passkey"): "vault_cfg_radius_passkey",
    ("RADIUS_SERVER", "passkey"): "vault_cfg_radius_passkey",
    ("LDAP", "bind_password"): "vault_cfg_ldap_bind_password",
    ("LDAP_SERVER", "bind_password"): "vault_cfg_ldap_bind_password",
    ("NTP_KEY", "value"): "vault_cfg_ntp_key",
    ("MACSEC_PROFILE", "primary_cak"): "vault_cfg_macsec_cak",
    ("MACSEC_PROFILE", "fallback_cak"): "vault_cfg_macsec_fallback_cak",
    ("BGP_NEIGHBOR", "password"): "vault_cfg_bgp_neighbor_password",
}

# Backstop for tables not listed above: a field NAMED like a secret.
_SONIC_SECRET_FIELD_RE = re.compile(
    r"(?i)(password|passwd|passkey|secret|psk$|private_key|auth_key|_cak$)")


def _sonic_var_name(table, field):
    return "vault_cfg_sonic_%s" % re.sub(r"[^a-z0-9]+", "_",
                                         ("%s_%s" % (table, field)).lower()).strip("_")


def _redact_sonic_row(table, row, ph=_ph):
    if not isinstance(row, dict):
        return row
    out = {}
    for field, value in row.items():
        var = _SONIC_FIELDS.get((table, field))
        if var is None and _SONIC_SECRET_FIELD_RE.search(field):
            var = _sonic_var_name(table, field)
        if (var and isinstance(value, str) and value
                and not _PLACEHOLDER_RE.match(value)):
            value = ph(var, value)
        out[field] = value
    return out


def _redact_sonic_communities(rows, ph=_ph):
    out = {}
    used = set(rows)                # placeholders already in the input
    for name, row in rows.items():
        if _PLACEHOLDER_RE.match(name):
            out[name] = row
            continue
        kind = (row.get("TYPE", "RO") if isinstance(row, dict) else "RO").upper()
        base = _SONIC_COMMUNITY_VAR.get(kind, "vault_cfg_snmp_ro_community")
        var, n = base, 1
        while _ph(var) in used:     # a second community of the same TYPE
            n += 1
            var = "%s_%d" % (base, n)
        used.add(_ph(var))
        out[ph(var, name)] = row
    return out


def _redact_sonic(text, ph=_ph):
    try:
        db = json.loads(text)
    except ValueError as e:
        raise ValueError("redact_secrets: JSON-looking config did not parse "
                         "(refusing to pass it through unredacted): %s" % e)
    if not isinstance(db, dict):
        raise ValueError("redact_secrets: JSON config is not an object")
    return json.dumps(_redact_sonic_db(db, ph), indent=4) + "\n"


# Where a config_db object may start: a `{` opening a line.
_JSON_START_RE = re.compile(r"^[ \t]*\{", re.M)


def _extract_config_db(text):
    """The config_db object in `text`, which may carry leading noise (a stray
    stderr line merged by raw's tty). Tries each line-opening `{` in order; the
    first that decodes to an object must be followed by whitespace only.
    RAISES when there is no object: nothing passes through unparsed."""
    if not isinstance(text, str):
        raise ValueError("redact_sonic_config_db: config_db is not text")
    dec = json.JSONDecoder()
    for m in _JSON_START_RE.finditer(text):
        start = m.end() - 1
        try:
            db, end = dec.raw_decode(text, start)
        except ValueError:
            continue
        if not isinstance(db, dict):
            continue
        if text[end:].strip():
            raise ValueError("redact_sonic_config_db: trailing data after the "
                             "config_db object (refusing to guess)")
        return db
    raise ValueError("redact_sonic_config_db: no config_db JSON object found "
                     "(refusing to pass it through unredacted)")


def redact_sonic_config_db(text):
    """SONiC config_db filter for roles/sonic: extract the JSON object (see
    _extract_config_db), redact it field-wise, return SONiC-format JSON text."""
    return json.dumps(_redact_sonic_db(_extract_config_db(text)), indent=4) + "\n"


def _redact_sonic_db(db, ph=_ph):
    """Redact a parsed config_db dict; returns a new dict, key order kept."""
    out = {}
    for table, rows in db.items():
        if table == "SNMP_COMMUNITY" and isinstance(rows, dict):
            out[table] = _redact_sonic_communities(rows, ph)
        elif table == "bgpraw" and isinstance(rows, str):
            out[table] = _redact(rows, ph)
        elif isinstance(rows, dict):
            out[table] = {k: _redact_sonic_row(table, r, ph) for k, r in rows.items()}
        else:
            out[table] = rows
    return out


def redact_secrets(text):
    """Return `text` with every known cleartext secret replaced by its
    `{{ vault_* }}` placeholder. Non-string input is returned unchanged.
    A JSON object (SONiC config_db) is redacted field-wise; see _redact_sonic."""
    return _redact(text, _ph)


def _redact(text, ph):
    """redact_secrets with a pluggable replacement: ph(var, secret) -> str."""
    if not isinstance(text, str) or not text:
        return text
    if text.lstrip().startswith("{"):
        return _redact_sonic(text, ph)

    out = []
    in_line_block = False
    for line in text.splitlines():
        if _LINE_BLOCK_RE.match(line):
            in_line_block = True
        elif _TOP_LEVEL_RE.match(line):
            in_line_block = False

        replaced = None
        for regex, var in _RULES:
            replaced = _apply(line, regex, var, ph)
            if replaced is not None:
                break
        if replaced is None and in_line_block:
            replaced = _apply(line, _LINE_PASSWORD_RE, _LINE_PASSWORD_VAR, ph)

        out.append(replaced if replaced is not None else line)

    result = "\n".join(out)
    if text.endswith("\n"):
        result += "\n"
    return result


class FilterModule(object):
    def filters(self):
        return {"redact_secrets": redact_secrets,
                "redact_sonic_config_db": redact_sonic_config_db}
