"""Power + HSC watts from ONE cached SDR sensor (spec 2026-09-28-observe-bmc-load §3.1).

A full `ipmitool sdr` walk pinned a Tioga Pass BMC's CPU for 10-15 s of every
30 s observe cycle (measured 2026-09-28). The SDR is dumped once per BMC to
<cache_dir>/<mac>.sdr; each cycle then reads `power status` + one
`sensor reading "<name>"` in a single session with `-S <cache>` (~0.3 s).
<mac>.json = {"name": <HSC sensor name or null>, "built": <epoch>}.
"""
import json
import os
import tempfile
import time

from .bmc_probe import _parse_power_from_ipmi_output, _parse_watts_from_ipmi_output

CACHE_DIR = "/run/flax/sdr-cache"
REBOOT_DIR = "/run/flax/bmc-reboot"
REBUILD_MIN_SECS = 600
MAX_AGE_SECS = 86400


def _paths(cache_dir, bmc_mac):
    key = bmc_mac.replace(":", "").lower()
    return (os.path.join(cache_dir, key + ".sdr"), os.path.join(cache_dir, key + ".json"))


def _load_meta(meta_path):
    try:
        with open(meta_path) as f:
            meta = json.load(f)
        return meta if isinstance(meta, dict) and "built" in meta else None
    except (OSError, ValueError):
        return None


def _mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


def _exec(runner, ip, login, lines, **kw):
    fd, script = tempfile.mkstemp(prefix="ipmi-cmds-", suffix=".txt")
    try:
        with os.fdopen(fd, "w") as f:
            f.write("".join(l + "\n" for l in lines))
        return runner(ip, login[0], login[1], ["exec", script], **kw)
    finally:
        try:
            os.unlink(script)
        except FileNotFoundError:
            pass


def _hsc_name(full_text):
    """The NAME of the row _parse_watts_from_ipmi_output would pick, or None."""
    for line in full_text.splitlines():
        parts = line.split("|")
        if len(parts) >= 2 and "hsc" in parts[0].lower() and "power" in parts[0].lower() \
                and "Watts" in parts[1]:
            return parts[0].strip()
    return None


def _reading(text, name):
    """'NN.NN W' from `sensor reading "<name>"` output ('<name> | 141.600'), or None."""
    for line in text.splitlines():
        parts = line.split("|")
        if len(parts) >= 2 and parts[0].strip() == name:
            try:
                return "%.2f W" % float(parts[1].strip())
            except ValueError:
                return None
    return None


def _build(ip, login, cache, meta_path, cipher, runner, now):
    """sdr dump + one full walk: this cycle's answer and the sensor name."""
    os.makedirs(os.path.dirname(cache), exist_ok=True)
    runner(ip, login[0], login[1], ["sdr", "dump", cache], cipher=cipher)
    full = _exec(runner, ip, login, ["power status", "sdr"], cipher=cipher)
    meta = {"name": _hsc_name(full), "built": now()}
    tmp = meta_path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(meta, f)
    os.replace(tmp, meta_path)
    return _parse_power_from_ipmi_output(full), _parse_watts_from_ipmi_output(full)


def power_and_watts(ip, login, bmc_mac, port, *, cipher=None, runner=None,
                    cache_dir=CACHE_DIR, reboot_dir=REBOOT_DIR, now=time.time,
                    rebuild_min_secs=REBUILD_MIN_SECS, max_age_secs=MAX_AGE_SECS):
    if runner is None:
        from .ipmi import _default_ipmi_runner as runner
    try:
        if not bmc_mac:
            full = _exec(runner, ip, login, ["power status", "sdr"], cipher=cipher)
            return _parse_power_from_ipmi_output(full), _parse_watts_from_ipmi_output(full)
        cache, meta_path = _paths(cache_dir, bmc_mac)
        meta = _load_meta(meta_path)
        t = now()
        marker = _mtime(os.path.join(reboot_dir, port)) if port else None
        valid = (meta is not None and os.path.exists(cache)
                 and t - meta["built"] < max_age_secs
                 and not (marker is not None and marker > meta["built"]))
        if not valid:
            return _build(ip, login, cache, meta_path, cipher, runner, now)
        name = meta.get("name")
        lines = ["power status"] + (['sensor reading "%s"' % name] if name else [])
        text = _exec(runner, ip, login, lines, cipher=cipher, sdr_cache=cache)
        pwr = _parse_power_from_ipmi_output(text)
        watts = _reading(text, name) if name else None
        if name and watts is None and pwr != "unknown" \
                and t - meta["built"] >= rebuild_min_secs:
            return _build(ip, login, cache, meta_path, cipher, runner, now)
        return pwr, watts
    except Exception:
        return "unknown", None
