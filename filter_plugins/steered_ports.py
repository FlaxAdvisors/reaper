"""Keep apply_config.yml off the access VLANs flax-reconcile owns.

flax-reconcile steers the access VLAN of every port named in the site's geometry
files (geometry.json triage, post-geometry.json post, turtle-geometry.json), minus
no-steer-ports.json. A committed running-config only snapshots whatever VLAN
reconcile had set when it was fetched, so pushing it would revert live steering.

  steered_ports(services_dir, switch)          -> sorted port names on that switch
                                                  (EOS ports in Ethernet<P>/<S> form)
  eos_without_steered_vlans(text, ports)       -> EOS config minus `switchport access
                                                  vlan` in those ports' stanzas; with
                                                  eos_config replace: line, the live
                                                  value stays
  cumulus_keep_steered_vlans(desired, live, ports)
                                               -> interfaces file whose steered
                                                  stanzas carry the live bridge-access
                                                  line (the push writes the whole file)

Port names follow flax_reconcile/portname.py (et6b1 -> Ethernet6/1; swp6 unchanged).
Self-contained on purpose: ansible loads this file outside the flax packages.
"""
import json
import os
import re

_GEOMETRY_FILES = ("geometry.json", "post-geometry.json", "turtle-geometry.json")


def _to_arista(port):
    m = re.match(r"^et(\d+)b(\d+)$", port)
    return "Ethernet%s/%s" % m.groups() if m else port


def _rows(path):
    """Rows of a geometry file; missing file -> none (reconcile reads it the same way)."""
    if not os.path.exists(path):
        return []
    with open(path) as f:
        data = json.load(f)
    return data.get("slots", []) if isinstance(data, dict) else data


def steered_ports(services_dir, switch):
    ports = set()
    for name in _GEOMETRY_FILES:
        for row in _rows(os.path.join(services_dir, name)):
            if row.get("switch") == switch and row.get("port"):
                ports.add(_to_arista(row["port"]))
    no_steer = {_to_arista(r["port"]) for r in _rows(os.path.join(services_dir, "no-steer-ports.json"))
                if r.get("switch") == switch and r.get("port")}
    return sorted(ports - no_steer)


def eos_without_steered_vlans(text, ports):
    steered = set(ports)
    out, cur = [], None
    for line in text.splitlines(keepends=True):
        m = re.match(r"^interface (\S+)\s*$", line)
        if m:
            cur = m.group(1)
        elif not line.startswith(" "):
            cur = None
        elif cur in steered and re.match(r"^\s+switchport access vlan \d+\s*$", line):
            continue
        out.append(line)
    return "".join(out)


def _iface_access(text):
    """{iface: its bridge-access line} for each `iface <name>` stanza that has one."""
    found, cur = {}, None
    for line in text.splitlines():
        m = re.match(r"^iface (\S+)", line)
        if m:
            cur = m.group(1)
        elif not line.startswith(" "):
            cur = None
        elif cur and line.strip().startswith("bridge-access "):
            found[cur] = line.rstrip()
    return found


def cumulus_keep_steered_vlans(desired, live, ports):
    live_access = _iface_access(live.replace("\r", ""))
    steered = set(ports)
    out, cur = [], None
    for line in desired.replace("\r", "").splitlines(keepends=True):
        m = re.match(r"^iface (\S+)", line)
        if m:
            cur = m.group(1)
        elif not line.startswith(" "):
            cur = None
        elif cur in steered and cur in live_access and line.strip().startswith("bridge-access "):
            line = live_access[cur] + "\n"
        out.append(line)
    return "".join(out)


def crlf_to_lf(text):
    """Raw-module output arrives with CRLF line ends. Done here, not in Jinja: a
    '\\r' literal is either left as two characters (folded YAML) or normalised to
    '\\n' by Jinja's lexer (double-quoted YAML), so replace('\\r', '') never works."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


class FilterModule:
    def filters(self):
        return {
            "steered_ports": steered_ports,
            "eos_without_steered_vlans": eos_without_steered_vlans,
            "cumulus_keep_steered_vlans": cumulus_keep_steered_vlans,
            "crlf_to_lf": crlf_to_lf,
        }
