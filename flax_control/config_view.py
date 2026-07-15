"""Config file readers for the /config page.

Security contract:
  - Only reads files in FLAX_CONFIG_DIR (default /etc/flax).
  - credentials.json / credentials-bmc.json / credentials-host.json are NEVER
    read -- they are not mounted into the container. credentials_reference()
    is a static lookup returning key names only.
  - site_env_switches() enforces an explicit allowlist; unknown keys are dropped.
"""
import json
import logging
import os
from pathlib import Path

from flax_reconcile.config import DEFAULTS as _RECONCILE_DEFAULTS

log = logging.getLogger("flax-control.config_view")

# Overridden in tests via monkeypatch.
FLAX_CONFIG_DIR = os.environ.get("FLAX_CONFIG_DIR", "/etc/flax")


def _path(name: str) -> Path:
    return Path(FLAX_CONFIG_DIR) / name


def _read_json(name: str):
    """Return parsed JSON from FLAX_CONFIG_DIR/name, or None if absent/malformed."""
    try:
        with open(_path(name)) as f:
            return json.load(f)
    except OSError:
        return None
    except json.JSONDecodeError as exc:
        log.warning("malformed JSON in %s: %s", _path(name), exc)
        return None


# ---------------------------------------------------------------------------
# reconcile_tunables
# ---------------------------------------------------------------------------

def reconcile_tunables() -> list[dict]:
    """Return one row per reconcile knob: key, default, current, source.

    Reads FLAX_CONFIG_DIR/reconcile.json. Missing file -> all defaults.
    Unknown keys in the file are silently ignored (forward-compat).
    """
    raw = _read_json("reconcile.json") or {}
    rows = []
    for key, default in _RECONCILE_DEFAULTS.items():
        if key in raw:
            current = raw[key]
            source = "reconcile.json"
        else:
            current = default
            source = "(default)"
        rows.append({"key": key, "default": default, "current": current,
                     "source": source})
    return rows


# ---------------------------------------------------------------------------
# topology_files
# ---------------------------------------------------------------------------

_TOPOLOGY_META = [
    {
        "name": "geometry.json",
        "drives": "triage/post phase + observable ports (ou/port mapping)",
        "deployed_by": "apply_lease_agent",
    },
    {
        "name": "vlans.json",
        "drives": "(family, phase) -> vid policy + vid -> parent iface",
        "deployed_by": "apply_lease_agent",
    },
    {
        "name": "switches.json",
        "drives": "switch inventory (name/driver/host) -- read by switch-sense, written by reconcile",
        "deployed_by": "apply_lease_agent",
    },
    {
        "name": "no-steer-ports.json",
        "drives": "ports excluded from VLAN steering (uplinks, customer ports)",
        "deployed_by": "apply_lease_agent",
    },
    {
        "name": "turtle-geometry.json",
        "drives": "Cumulus turtle OOB-mgmt geometry (swp port -> DUT-BMC slot)",
        "deployed_by": "apply_lease_agent",
    },
    {
        "name": "bmc-only-families.json",
        "drives": "families observed BMC-only (no host NIC) -- read by flax-observe/classify",
        "deployed_by": "apply_lease_agent",
    },
]


def topology_files() -> list[dict]:
    """Return one entry per topology file with metadata and parsed data.

    Each entry: {path, drives, deployed_by, present, data}.
    Missing files: present=False, data=None.
    Malformed files: present=True, data=None (file exists but could not be parsed).
    """
    result = []
    for meta in _TOPOLOGY_META:
        name = meta["name"]
        p = _path(name)
        present = p.exists()
        data = _read_json(name) if present else None
        result.append({
            "path": str(p),
            "drives": meta["drives"],
            "deployed_by": meta["deployed_by"],
            "present": present,
            "data": data,
        })
    return result


def bmc_fw_manifest() -> dict:
    """Return the BMC firmware target manifest entry for the config page.

    Same shape as a topology_files() entry: {path, drives, deployed_by,
    present, data}. Missing -> present=False, data=None.
    """
    name = "bmc-firmware-versions.json"
    p = _path(name)
    present = p.exists()
    return {
        "path": str(p),
        "drives": "BMC firmware target version + flash artifact per platform "
                  "-- read by the bmc_fw worker",
        "deployed_by": "apply_bmc_fw_manifest",
        "present": present,
        "data": _read_json(name) if present else None,
    }


# ---------------------------------------------------------------------------
# classifier_dirs (macmath/ + family-map/ — directories of per-file rules)
# ---------------------------------------------------------------------------

_CLASSIFIER_DIRS = [
    {
        "name": "macmath",
        "drives": "per-vid MAC-math classifier rules (NN.json per vlan) -- read by flax-observe/switch-sense",
    },
    {
        "name": "family-map",
        "drives": "device family product-name regexes (one .txt per family) -- read by flax-discover",
    },
]


def classifier_dirs() -> list[dict]:
    """List the per-file contents of the macmath/ + family-map/ config dirs.

    Each entry: {name, path, drives, present, files:[{name, data, raw}]}.
    Files are read as text and ALSO JSON-parsed (data=None when not JSON, e.g.
    family-map's .txt regex files — raw still carries the content for display).
    Files are name-sorted; a missing dir is present=False with files=[].
    """
    out = []
    for meta in _CLASSIFIER_DIRS:
        d = _path(meta["name"])
        present = d.is_dir()
        files = []
        if present:
            for fp in sorted(d.iterdir()):
                if not fp.is_file():
                    continue
                try:
                    raw = fp.read_text()
                except OSError:
                    raw = None
                try:
                    data = json.loads(raw) if raw is not None else None
                except (ValueError, TypeError):
                    data = None
                files.append({"name": fp.name, "data": data, "raw": raw})
        out.append({
            "name": meta["name"],
            "path": str(d),
            "drives": meta["drives"],
            "present": present,
            "files": files,
        })
    return out


# ---------------------------------------------------------------------------
# site_env_switches
# ---------------------------------------------------------------------------

_SITE_ENV_ALLOWLIST = {
    "MGMT_VIP",
    "BANG_DHCP_SERVER",
    "BANG_RECONCILE",
    "PRIMARY_RABBIT",
    "TURTLE_TYPE",
}


def site_env_switches() -> list[dict]:
    """Parse FLAX_CONFIG_DIR/site.env and return ONLY the allowlisted keys.

    Returns [{key, value}] for each allowlisted key present in the file.
    Missing file -> [].  Unknown keys are silently dropped.
    """
    try:
        text = _path("site.env").read_text()
    except OSError:
        return []

    result = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key in _SITE_ENV_ALLOWLIST:
            result.append({"key": key, "value": value.strip()})
    return result


# ---------------------------------------------------------------------------
# credentials_reference -- STATIC, no file reads
# ---------------------------------------------------------------------------

# This is deliberately hardcoded. The credential files are NOT mounted into
# the container. This reference exists only so operators know what keys exist
# and where to find them, without ever exposing values.
_CREDENTIALS_REFERENCE = [
    {
        "path": "/etc/flax/credentials.json",
        "description": "Switch CLI credentials (EOS + Cisco IOS) and OpenBMC credentials",
        "key_names": ["eosuser", "eospass", "cisco_user", "cisco_pass",
                      "obmcuser", "obmcpass"],
        "note": "vault-managed -- edit via `ansible-vault`; values never surfaced here.",
    },
    {
        "path": "/etc/flax/credentials-bmc.json",
        "description": "List of BMC credential pairs",
        "key_names": ["bmcuser", "bmcpass"],
        "note": "vault-managed -- edit via `ansible-vault`; values never surfaced here.",
    },
    {
        "path": "/etc/flax/credentials-host.json",
        "description": "List of host (SSH) credential pairs",
        "key_names": ["user", "pass"],
        "note": "vault-managed -- edit via `ansible-vault`; values never surfaced here.",
    },
]


def credentials_reference() -> list[dict]:
    """Return static reference for credential files. NO file reads occur.

    Returns key names and vault-management notes. Values are never surfaced.
    """
    return list(_CREDENTIALS_REFERENCE)
