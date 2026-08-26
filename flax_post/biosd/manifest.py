"""BIOS firmware manifest: platform -> {target, afulnx_url, bin_url, flags}.
Analog of flax_post.fwd.manifest, matched by a dmidecode-product substring."""
import json
import os


def load_bios_manifest(config_dir: str) -> list:
    path = os.path.join(config_dir, "bios-firmware-versions.json")
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    return data if isinstance(data, list) else []


class BiosMatcher:
    def __init__(self, entries: list):
        self.entries = entries or []

    def match(self, dmi_product: str) -> dict | None:
        p = dmi_product or ""
        for e in self.entries:
            m = e.get("dmi_match")
            if m and m in p:
                return e
        return None
