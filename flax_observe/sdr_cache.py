"""Power + HSC watts from ONE cached SDR sensor (spec 2026-09-28-observe-bmc-load §3.1).

A full `ipmitool sdr` walk pinned a Tioga Pass BMC's CPU for 10-15 s of every
30 s observe cycle (measured 2026-09-28). The SDR is dumped once per BMC to
<cache_dir>/<mac>.sdr; each cycle then reads `power status` + one
`sensor reading "<name>"` in a single session with `-S <cache>` (~0.3 s).
<mac>.json = {"name": <HSC sensor name or null>, "built": <epoch of the
last SUCCESSFUL build>, "attempted": <epoch of the last build ATTEMPT,
successful or not>}.

Fix round 1 (2026-09-28, review findings against spec §3.1):
- A: the SDR dump is written to `<mac>.sdr.tmp` and only `os.replace`d onto
  the live `<mac>.sdr` after BOTH the dump and the confirming full walk
  succeed -- a killed/truncated dump never corrupts the live cache that a
  still-"valid" meta would otherwise trust for up to 24 h.
- B: `_default_ipmi_runner` runs with `check=True`, so a non-zero rc (e.g.
  `sensor reading` of a name the live SDR no longer has) raises
  `subprocess.CalledProcessError`, not a clean return -- the cached read is
  wrapped to catch it, parse power from `e.output` and treat "power parsed,
  no reading" as the same staleness signal as a literal "na" value.
- C: a build that keeps failing (dead BMC, bad creds) no longer repeats
  every cycle -- each build attempt (success or fail) is recorded as
  `attempted` before the dump runs, and no new attempt starts within
  `rebuild_min_secs` of the last one; while gated, a still-usable cache is
  read normally, and lacking one, a single `power status`-only session
  answers the cycle.
- D: `_load_meta` rejects a non-numeric `built` (and `attempted`, if
  present) as an invalid/corrupt meta file, forcing a fresh build attempt.
- E: the HSC-row matcher is shared with `bmc_probe._parse_watts_from_ipmi_output`
  via `bmc_probe._is_hsc_power_row` instead of being duplicated here.
"""
import json
import os
import subprocess
import tempfile
import time

from .bmc_probe import (
    _is_hsc_power_row,
    _parse_power_from_ipmi_output,
    _parse_watts_from_ipmi_output,
)

CACHE_DIR = "/run/flax/sdr-cache"
REBOOT_DIR = "/run/flax/bmc-reboot"
REBUILD_MIN_SECS = 600
MAX_AGE_SECS = 86400


def _paths(cache_dir, bmc_mac):
    key = bmc_mac.replace(":", "").lower()
    return (os.path.join(cache_dir, key + ".sdr"), os.path.join(cache_dir, key + ".json"))


def _is_epoch(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _load_meta(meta_path):
    """The full meta dict, or None if missing/corrupt -- including a
    non-numeric `built` or (if present) `attempted` (fix round 1 item D)."""
    try:
        with open(meta_path) as f:
            meta = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(meta, dict) or "built" not in meta or not _is_epoch(meta["built"]):
        return None
    if "attempted" in meta and not _is_epoch(meta["attempted"]):
        return None
    return meta


def _last_attempt(meta_path):
    """Epoch of the last recorded build ATTEMPT, tolerant of a meta file
    that has no successful build yet -- only `attempted`, from a build that
    started the dump and then failed before writing `built` (fix round 1
    item C). Independent of `_load_meta`'s stricter "usable cache" check."""
    try:
        with open(meta_path) as f:
            meta = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(meta, dict):
        return None
    a = meta.get("attempted")
    return a if _is_epoch(a) else None


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
        if len(parts) >= 2 and _is_hsc_power_row(parts[0], parts[1]):
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


def _record_attempt(meta_path, prior_meta, t):
    """Write `attempted = t`, keeping any existing name/built from
    `prior_meta` (which may be None -- no meta yet, or an invalid one)."""
    meta = dict(prior_meta) if isinstance(prior_meta, dict) else {}
    meta["attempted"] = t
    os.makedirs(os.path.dirname(meta_path), exist_ok=True)
    tmp = meta_path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(meta, f)
    os.replace(tmp, meta_path)


def _build(ip, login, cache, meta_path, cipher, runner, t, prior_meta):
    """sdr dump + one full walk: this cycle's answer and the sensor name.

    The dump goes to `<cache>.tmp`; only once it AND the confirming full
    walk both succeed does `<cache>.tmp` replace the live `<cache>` and does
    `built` get written. A failed build leaves the live cache file (if any)
    exactly as it was -- a truncated/partial dump is never visible at the
    live path (fix round 1 item A). The attempt itself is recorded first,
    before either network call, so a build that raises still rate-limits
    the next attempt (fix round 1 item C).
    """
    _record_attempt(meta_path, prior_meta, t)
    os.makedirs(os.path.dirname(cache), exist_ok=True)
    tmp_cache = cache + ".tmp"
    try:
        runner(ip, login[0], login[1], ["sdr", "dump", tmp_cache], cipher=cipher)
        full = _exec(runner, ip, login, ["power status", "sdr"], cipher=cipher)
    except Exception:
        try:
            os.unlink(tmp_cache)
        except FileNotFoundError:
            pass
        raise
    os.replace(tmp_cache, cache)
    meta = {"name": _hsc_name(full), "built": t, "attempted": t}
    tmp_meta = meta_path + ".tmp"
    with open(tmp_meta, "w") as f:
        json.dump(meta, f)
    os.replace(tmp_meta, meta_path)
    return _parse_power_from_ipmi_output(full), _parse_watts_from_ipmi_output(full)


def _gated_build(ip, login, cache, meta_path, cipher, runner, t, meta, rebuild_min_secs):
    """Build the cache -- unless a build was already attempted within
    `rebuild_min_secs`, in which case fall back instead of hammering a BMC
    that's already failing to answer (fix round 1 item C): a still-usable
    cache (meta known + cache file present) is read normally; lacking one,
    a single `power status`-only session (no sdr walk) answers the cycle.
    """
    last_attempt = _last_attempt(meta_path)
    if last_attempt is not None and t - last_attempt < rebuild_min_secs:
        if meta is not None and os.path.exists(cache):
            name = meta.get("name")
            lines = ["power status"] + (['sensor reading "%s"' % name] if name else [])
            text = _exec(runner, ip, login, lines, cipher=cipher, sdr_cache=cache)
            pwr = _parse_power_from_ipmi_output(text)
            watts = _reading(text, name) if name else None
            return pwr, watts
        text = _exec(runner, ip, login, ["power status"], cipher=cipher)
        return _parse_power_from_ipmi_output(text), None
    return _build(ip, login, cache, meta_path, cipher, runner, t, meta)


def _stale(ip, login, cache, meta_path, cipher, runner, t, meta, rebuild_min_secs, pwr):
    """The cached read gave no usable watts reading (parsed 'na', or a
    CalledProcessError with no reading for `name`). Rebuild if the attempt
    rate limit allows; otherwise just answer with this cycle's power --
    no extra runner call (fix round 1 items B and C)."""
    last_attempt = _last_attempt(meta_path)
    if last_attempt is not None and t - last_attempt < rebuild_min_secs:
        return pwr, None
    return _build(ip, login, cache, meta_path, cipher, runner, t, meta)


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
            return _gated_build(ip, login, cache, meta_path, cipher, runner, t, meta,
                                rebuild_min_secs)
        name = meta.get("name")
        lines = ["power status"] + (['sensor reading "%s"' % name] if name else [])
        try:
            text = _exec(runner, ip, login, lines, cipher=cipher, sdr_cache=cache)
        except subprocess.CalledProcessError as e:
            out = e.output
            if isinstance(out, bytes):
                out = out.decode("utf-8", errors="replace")
            elif out is None:
                out = ""
            pwr = _parse_power_from_ipmi_output(out)
            if pwr == "unknown":
                return "unknown", None
            return _stale(ip, login, cache, meta_path, cipher, runner, t, meta,
                          rebuild_min_secs, pwr)
        pwr = _parse_power_from_ipmi_output(text)
        watts = _reading(text, name) if name else None
        if name and watts is None and pwr != "unknown":
            return _stale(ip, login, cache, meta_path, cipher, runner, t, meta,
                          rebuild_min_secs, pwr)
        return pwr, watts
    except Exception:
        return "unknown", None
