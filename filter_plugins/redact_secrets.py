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
"""
import re

# Cisco sub-modes whose `password <x>` line is the vty/console password.
_LINE_BLOCK_RE = re.compile(r"^line\s+(con|vty|aux)\b")
# Any non-indented line ends the sub-mode.
_TOP_LEVEL_RE = re.compile(r"^\S")

# A value that is already a placeholder -- leave it alone (idempotency).
_PLACEHOLDER_RE = re.compile(r"\{\{")

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


def _apply(line, regex, var):
    m = regex.match(line)
    if not m:
        return None
    prefix, secret, suffix = m.group(1), m.group(2), m.group(3)
    if not secret or _PLACEHOLDER_RE.search(secret) or _TYPED_RE.match(secret):
        return None
    return "%s%s%s" % (prefix, _quoted(secret, "{{ %s }}" % var), suffix)


def redact_secrets(text):
    """Return `text` with every known cleartext secret replaced by its
    `{{ vault_* }}` placeholder. Non-string input is returned unchanged."""
    if not isinstance(text, str) or not text:
        return text

    out = []
    in_line_block = False
    for line in text.splitlines():
        if _LINE_BLOCK_RE.match(line):
            in_line_block = True
        elif _TOP_LEVEL_RE.match(line):
            in_line_block = False

        replaced = None
        for regex, var in _RULES:
            replaced = _apply(line, regex, var)
            if replaced is not None:
                break
        if replaced is None and in_line_block:
            replaced = _apply(line, _LINE_PASSWORD_RE, _LINE_PASSWORD_VAR)

        out.append(replaced if replaced is not None else line)

    result = "\n".join(out)
    if text.endswith("\n"):
        result += "\n"
    return result


class FilterModule(object):
    def filters(self):
        return {"redact_secrets": redact_secrets}
