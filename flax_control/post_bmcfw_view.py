"""Read-only post BMC/NIC/BIOS firmware fleet, merged from the post fw file
stores (sole-written by flax_post/fwd, mounted read-only). No DB, no HTTP —
same file-store pattern as bmcfw_view for triage."""
import json
import os
from pathlib import Path

FLAX_CONFIG_DIR = os.environ.get("FLAX_CONFIG_DIR", "/etc/flax")
_PHASE_BADGE = {"up_to_date": "g", "done": "g", "mlx-checked": "g", "needs_update": "w",
                "fault": "c", "flashing": "n", "monitoring": "n", "activating": "n"}


def _read(name):
    try:
        with open(Path(FLAX_CONFIG_DIR) / name) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def read_stores():
    return (_read("post_fw.json"), _read("post_nic_fw.json"), _read("post_bios_fw.json"))


def fleet_rows(bmc_store, nic_store, bios_store):
    bmc, nic, bios = bmc_store or {}, nic_store or {}, bios_store or {}
    ports = sorted(set(bmc) | set(nic) | set(bios))
    out = []
    for p in ports:
        b = bmc.get(p) or {}
        out.append({"port": p, "bmc_ip": b.get("bmc_ip"),
                    "bmc": bmc.get(p) or {}, "nic": nic.get(p) or {},
                    "bios": bios.get(p) or {}})
    return out


def badge_for(phase):
    return _PHASE_BADGE.get(phase, "n")


def last_updated(*stores):
    ts = [r.get("updated_at") for s in stores if s
          for r in s.values() if isinstance(r, dict) and r.get("updated_at")]
    return max(ts) if ts else None
