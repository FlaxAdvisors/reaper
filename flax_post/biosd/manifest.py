"""BIOS firmware manifest: family -> {target, afulnx_url, bin_url, flags}.
Analog of flax_post.fwd.manifest. The platform is NOT matched here: the DMI
Product Name is resolved to a family by the site family map
(/etc/flax/family-map, the same one observe and inventory use) and the
manifest entry names that family."""
import json
import os
import re

from ..observe import family_map_live
from ..observe.family_map import match_family

_DMI_PRODUCT = re.compile(r"^\s*Product Name:\s*(.*\S)", re.MULTILINE)


def dmi_product(dmi_system_out: str) -> str | None:
    """The `Product Name:` of `dmidecode -t system` output."""
    m = _DMI_PRODUCT.search(dmi_system_out or "")
    return m.group(1) if m else None


def load_bios_manifest(config_dir: str) -> list:
    path = os.path.join(config_dir, "bios-firmware-versions.json")
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    return data if isinstance(data, list) else []


class BiosMatcher:
    def __init__(self, entries: list, family_map=family_map_live.current):
        self.entries = entries or []
        self._family_map = family_map       # callable: the map reloads when it changes

    def match(self, dmi_system_out: str) -> dict | None:
        family = match_family(self._family_map() or {}, dmi_product(dmi_system_out))
        if family is None:
            return None
        for e in self.entries:
            if e.get("family") == family:
                return e
        return None
