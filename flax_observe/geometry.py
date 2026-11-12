"""Geometry loader for flax-observe.

Lifted verbatim from scripts/switchportrecond.py:load_geometry.
Contract: /etc/flax/geometry.json is a JSON list of dicts. Each entry must
have 'port' and 'ou'. The 'switch' key is required unless a default is
supplied (single-switch deploys can omit it).
"""
import json


class ConfigError(Exception):
    pass


def load_geometry(path: str, default_switch_name: str | None = None) -> list[dict]:
    with open(path) as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ConfigError(f"{path}: expected list")
    out = []
    for e in data:
        if "port" not in e or "ou" not in e:
            raise ConfigError(f"{path}: entry missing 'port' or 'ou': {e!r}")
        switch = e.get("switch") or default_switch_name
        if not switch:
            raise ConfigError(
                f"{path}: entry {e!r} has no 'switch' and no default given"
            )
        out.append({"port": e["port"], "ou": e["ou"], "switch": switch})
    return out
