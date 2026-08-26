"""NIC firmware manifest: reported-PSID -> {dir, bin, target_version, target_psid}.
Keyed by the PSID the card reports (native MT_ and OEM FB_/HP_ overrides), so
lookup is a direct dict hit. Twin of flax_post.biosd.manifest, keyed by PSID."""
import json
import os


def load_nic_manifest(config_dir: str) -> dict:
    path = os.path.join(config_dir, "nic-firmware-versions.json")
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


class NicMatcher:
    def __init__(self, entries: dict):
        self.entries = entries or {}

    def match(self, psid: str) -> dict | None:
        return self.entries.get(psid) if psid else None
