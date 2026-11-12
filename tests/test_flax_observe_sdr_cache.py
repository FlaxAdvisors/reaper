"""flax_observe.sdr_cache (spec 2026-09-28-observe-bmc-load §3.1, fix round 1)."""
import json
import os
import subprocess

import pytest

from flax_observe import sdr_cache as sc

MAC = "98:03:9b:87:5e:18"
FULL = ("Chassis Power is on\n"
        "MB Inlet Temp    | 24 degrees C      | ok\n"
        "MB HSC In Power  | 141.60 Watts      | ok\n")
CACHED = "Chassis Power is on\nMB HSC In Power  | 141.600\n"


class Runner:
    """Fake runner: records every call; answers by the args it gets.

    fail_dump: writes partial garbage to the dump destination, then raises
        (simulates a killed/timed-out `sdr dump` -- fix round 1 item A).
    cached_error: (rc, output) -- the `sensor reading` cached call raises
        subprocess.CalledProcessError(rc, args, output=output) instead of
        returning `cached` (fix round 1 item B).
    """
    def __init__(self, cached=CACHED, full=FULL, fail_cached=False,
                fail_dump=False, cached_error=None, dump_records=None,
                stamp=None, stamp_error=False):
        self.calls, self.cached, self.full = [], cached, full
        self.dump_records = dump_records
        self.stamp, self.stamp_error = stamp, stamp_error
        self.fail_cached, self.fail_dump, self.cached_error = fail_cached, fail_dump, cached_error

    def __call__(self, host, user, password, args, cipher=None, sdr_cache=None, **kw):
        self.calls.append({"args": list(args), "cipher": cipher, "sdr_cache": sdr_cache,
                           "script": open(args[1]).read() if args[0] == "exec" else None})
        if args[:2] == ["sdr", "dump"]:
            if self.dump_records is not None and not self.fail_dump:
                # N SDR records: 5-byte header (id lo, id hi, ver, type, body len=0)
                open(args[2], "wb").write(b"".join(bytes([i & 0xff, i >> 8, 0x51, 0x01, 0])
                                                   for i in range(self.dump_records)))
                return ""
            open(args[2], "w").write("PARTIAL" if self.fail_dump else "binary")
            if self.fail_dump:
                raise RuntimeError("dump killed")
            return ""
        if args[:3] == ["raw", "0x0a", "0x20"]:
            if self.stamp_error:        # a BMC without Get SDR Repository Info
                raise subprocess.CalledProcessError(1, args, output=b"Unable to send RAW command\n")
            return self.stamp or ""
        script = self.calls[-1]["script"] or ""
        if "sensor reading" in script:
            if self.fail_cached:
                raise RuntimeError("stale cache")
            if self.cached_error is not None:
                rc, output = self.cached_error
                raise subprocess.CalledProcessError(rc, args, output=output)
            return self.cached
        return self.full

    def kinds(self):
        out = []
        for c in self.calls:
            script = c["script"] or ""
            if c["args"][:3] == ["raw", "0x0a", "0x20"]:
                continue                        # build-time stamp call: see stamp_calls()
            if c["args"][:2] == ["sdr", "dump"]:
                out.append("dump")
            elif "\nsdr" in "\n" + script:
                out.append("full")
            elif "sensor reading" in script:
                out.append("cached")
            else:
                out.append("power")            # power status only (no HSC row)
        return out


def stamp_calls(runner):
    return sum(1 for c in runner.calls if c["args"][:3] == ["raw", "0x0a", "0x20"])


def _call(tmp_path, runner, now=1000.0, port="et8b3", **kw):
    return sc.power_and_watts("172.17.8.103", ("root", "x"), MAC, port, cipher=17,
                              runner=runner, cache_dir=str(tmp_path / "c"),
                              reboot_dir=str(tmp_path / "r"), now=lambda: now, **kw)


def test_first_cycle_builds_then_steady_cycles_read_one_sensor(tmp_path):
    r = Runner()
    assert _call(tmp_path, r) == ("on", "141.60 W")
    assert r.kinds() == ["dump", "full"]
    meta = json.load(open(tmp_path / "c" / (MAC.replace(":", "") + ".json")))
    assert meta["name"] == "MB HSC In Power"
    r2 = Runner()
    assert _call(tmp_path, r2, now=1030.0) == ("on", "141.60 W")
    assert r2.kinds() == ["cached"]
    assert r2.calls[0]["cipher"] == 17
    assert r2.calls[0]["sdr_cache"].endswith(".sdr")
    assert 'sensor reading "MB HSC In Power"' in r2.calls[0]["script"]


def test_watts_format_matches_the_full_sdr_parser(tmp_path):
    _call(tmp_path, Runner())
    r = Runner(cached="Chassis Power is off\nMB HSC In Power  | 11.800\n")
    assert _call(tmp_path, r, now=1030.0) == ("off", "11.80 W")


def test_board_without_hsc_row_never_walks_again(tmp_path):
    full_no_hsc = "Chassis Power is on\nMB Inlet Temp | 24 degrees C | ok\n"
    r = Runner(full=full_no_hsc)
    assert _call(tmp_path, r) == ("on", None)
    r2 = Runner(full=full_no_hsc, cached="Chassis Power is on\n")
    for i in range(5):
        assert _call(tmp_path, r2, now=1030.0 + 30 * i) == ("on", None)
    assert "full" not in r2.kinds() and "dump" not in r2.kinds()
    assert all("sensor reading" not in (c["script"] or "") for c in r2.calls)


def test_restart_reuses_the_cache_no_dump(tmp_path):
    _call(tmp_path, Runner())
    r = Runner()                                  # a new process, same /run/flax
    _call(tmp_path, r, now=5000.0)
    assert r.kinds() == ["cached"]


def test_stale_cache_rebuilds_at_most_every_600s(tmp_path):
    _call(tmp_path, Runner(), now=1000.0)         # built at 1000
    r = Runner(cached="Chassis Power is on\nMB HSC In Power  | na\n")
    assert _call(tmp_path, r, now=1100.0) == ("on", None)   # stale, too soon: no rebuild
    assert r.kinds() == ["cached"]
    r2 = Runner(cached="Chassis Power is on\nMB HSC In Power  | na\n")
    _call(tmp_path, r2, now=1700.0)               # >= 600 s after build: rebuild once
    assert r2.kinds() == ["cached", "dump", "full"]


def test_reboot_marker_newer_than_build_forces_rebuild(tmp_path):
    _call(tmp_path, Runner(), now=1000.0)
    os.makedirs(tmp_path / "r")
    marker = tmp_path / "r" / "et8b3"
    marker.write_text("")
    os.utime(marker, (2000.0, 2000.0))
    r = Runner()
    _call(tmp_path, r, now=2030.0)
    assert r.kinds() == ["dump", "full"]


def test_age_over_24h_rebuilds(tmp_path):
    _call(tmp_path, Runner(), now=1000.0)
    r = Runner()
    _call(tmp_path, r, now=1000.0 + 86400 + 1)
    assert r.kinds() == ["dump", "full"]


def test_new_mac_is_a_new_cache(tmp_path):
    _call(tmp_path, Runner())
    r = Runner()
    sc.power_and_watts("172.17.8.103", ("root", "x"), "aa:bb:cc:dd:ee:ff", "et8b3", cipher=17,
                       runner=r, cache_dir=str(tmp_path / "c"), reboot_dir=str(tmp_path / "r"),
                       now=lambda: 1030.0)
    assert r.kinds() == ["dump", "full"]


def test_runner_failure_is_unknown_none(tmp_path):
    _call(tmp_path, Runner())
    assert _call(tmp_path, Runner(fail_cached=True), now=1030.0) == ("unknown", None)


def test_no_mac_reads_the_old_way(tmp_path):
    r = Runner()
    assert sc.power_and_watts("1.2.3.4", ("u", "p"), None, "et8b3", runner=r,
                              cache_dir=str(tmp_path / "c"), reboot_dir=str(tmp_path / "r"),
                              now=lambda: 1000.0) == ("on", "141.60 W")
    assert r.kinds() == ["full"]


# ---------------------------------------------------------------------------
# Fix round 1 (spec §3.1 gaps: review findings A-E)
# ---------------------------------------------------------------------------

def test_failed_dump_never_corrupts_the_live_cache(tmp_path):
    """Item A: a dump that raises after writing partial data to its output
    path must never leave that partial data visible at the live <mac>.sdr --
    and the very next cycle (rate-limited, no meta/built yet) must answer
    without ever passing -S at all, since there is no cache to trust."""
    r = Runner(fail_dump=True)
    assert _call(tmp_path, r) == ("unknown", None)
    assert r.kinds() == ["dump"]
    live = tmp_path / "c" / (MAC.replace(":", "") + ".sdr")
    assert not live.exists()
    meta = json.load(open(tmp_path / "c" / (MAC.replace(":", "") + ".json")))
    assert "built" not in meta and "attempted" in meta

    r2 = Runner()
    assert _call(tmp_path, r2, now=1030.0) == ("on", None)
    assert r2.kinds() == ["power"]
    assert r2.calls[0]["sdr_cache"] is None
    assert not live.exists()


def test_failed_build_is_rate_limited_then_retried(tmp_path):
    """Item C: a build that fails must not be retried every cycle -- within
    rebuild_min_secs of the failed attempt, no dump/full walk runs at all
    (power-status-only fallback); past it, a build is attempted again."""
    r1 = Runner(fail_dump=True)
    assert _call(tmp_path, r1, now=1000.0) == ("unknown", None)
    assert r1.kinds() == ["dump"]

    r2 = Runner()
    assert _call(tmp_path, r2, now=1100.0) == ("on", None)
    assert r2.kinds() == ["power"]

    r3 = Runner()
    assert _call(tmp_path, r3, now=1600.0) == ("on", "141.60 W")
    assert r3.kinds() == ["dump", "full"]


def test_called_process_error_on_cached_read_is_treated_as_stale(tmp_path):
    """Item B: _default_ipmi_runner's check=True means a missing sensor
    reading raises subprocess.CalledProcessError, not a clean return. That
    must be treated exactly like a parsed 'na' -- stale, gated by the same
    600s rebuild rate limit -- not swallowed by the broad except."""
    _call(tmp_path, Runner(), now=1000.0)  # built at 1000

    r = Runner(cached_error=(1, b"Chassis Power is on\n"))
    assert _call(tmp_path, r, now=1100.0) == ("on", None)   # stale, too soon: no rebuild
    assert r.kinds() == ["cached"]

    r2 = Runner(cached_error=(1, b"Chassis Power is on\n"))
    assert _call(tmp_path, r2, now=1700.0) == ("on", "141.60 W")   # >= 600s: rebuild once
    assert r2.kinds() == ["cached", "dump", "full"]


def test_called_process_error_with_no_power_is_unknown(tmp_path):
    """Item B: if the CalledProcessError's output has no parseable power
    either (bytes or None), the whole read is a genuine failure -> unknown,
    not a staleness signal, and no rebuild is attempted."""
    _call(tmp_path, Runner(), now=1000.0)

    r = Runner(cached_error=(1, b"some garbage\n"))
    assert _call(tmp_path, r, now=1030.0) == ("unknown", None)
    assert r.kinds() == ["cached"]

    r2 = Runner(cached_error=(1, None))
    assert _call(tmp_path, r2, now=1060.0) == ("unknown", None)
    assert r2.kinds() == ["cached"]


def test_stale_rebuild_past_600s_whose_dump_fails_keeps_this_cycles_power(tmp_path):
    """Finding 2 (fix wave): _stale's rebuild call to _build was unwrapped --
    a dump failure during a late (>=600s) rebuild attempt raised straight
    through _stale and the outer except, discarding the power reading that
    THIS cycle's cached read had already parsed successfully (only the HSC
    watts reading was stale/"na"). Must return (power, None), not
    ("unknown", None)."""
    _call(tmp_path, Runner(), now=1000.0)         # built at 1000
    r = Runner(cached="Chassis Power is on\nMB HSC In Power  | na\n", fail_dump=True)
    assert _call(tmp_path, r, now=1700.0) == ("on", None)   # >=600s: rebuild attempted, dump fails
    assert r.kinds() == ["cached", "dump"]


def test_meta_with_non_numeric_built_forces_rebuild(tmp_path):
    """Item D: a corrupt/hand-edited meta with a non-numeric `built` must be
    treated as no meta at all, not trusted for freshness."""
    cache_dir = tmp_path / "c"
    os.makedirs(cache_dir)
    key = MAC.replace(":", "").lower()
    (cache_dir / (key + ".sdr")).write_bytes(b"stale-binary")
    (cache_dir / (key + ".json")).write_text(
        json.dumps({"name": "MB HSC In Power", "built": "x"}))

    r = Runner()
    assert _call(tmp_path, r, now=1000.0) == ("on", "141.60 W")
    assert r.kinds() == ["dump", "full"]


# ---------------------------------------------------------------------------
# Fix round 2 (re-review: A-E ADDRESSED; the gated cached read still lost
# CalledProcessError's power reading -- confirmed as new Important breakage)
# ---------------------------------------------------------------------------

def test_reboot_marker_plus_failed_rebuild_keeps_old_live_cache(tmp_path):
    """Minor: 'existing live cache + reboot marker + dump fails' must never
    overwrite/corrupt the live .sdr -- item A's tmp-then-replace protection
    holds even when the cache being protected already has real (not just
    absent) prior content."""
    _call(tmp_path, Runner(), now=1000.0)              # live cache built with real content
    live = tmp_path / "c" / (MAC.replace(":", "") + ".sdr")
    assert live.read_bytes() == b"binary"

    os.makedirs(tmp_path / "r")
    marker = tmp_path / "r" / "et8b3"
    marker.write_text("")
    os.utime(marker, (2000.0, 2000.0))

    r = Runner(fail_dump=True)
    assert _call(tmp_path, r, now=2030.0) == ("unknown", None)
    assert r.kinds() == ["dump"]
    assert live.read_bytes() == b"binary"              # untouched -- not "PARTIAL"


def test_gated_reboot_marker_case_never_reads_the_preflash_cache(tmp_path):
    """Finding 3 (fix wave, supersedes the fix-round-2 version of this test):
    cache built at 1000; reboot marker at 2000 (a flash may have renumbered
    the SDR); at 2030 the rebuild's dump fails (attempted=2030, live cache
    unchanged, per the test above); at 2060 the cycle is gated (< 600s since
    the failed attempt). While gated AND the reboot marker is newer than the
    live cache's `built`, the gated path must NOT trust the pre-flash cache
    for up to 600s -- it must do a power-status-only read (no `-S`, no
    `sensor reading`) instead, exactly like the no-cache-yet gated fallback.
    Must still return the power that DID succeed, and never trigger a build
    (kinds has no 'dump')."""
    _call(tmp_path, Runner(), now=1000.0)
    os.makedirs(tmp_path / "r")
    marker = tmp_path / "r" / "et8b3"
    marker.write_text("")
    os.utime(marker, (2000.0, 2000.0))
    _call(tmp_path, Runner(fail_dump=True), now=2030.0)   # rebuild attempted, fails

    r = Runner()
    assert _call(tmp_path, r, now=2060.0) == ("on", None)
    assert r.kinds() == ["power"]
    assert r.calls[0]["sdr_cache"] is None
    assert 'sensor reading' not in (r.calls[0]["script"] or "")


# ---------------------------------------------------------------------------
# SDR Repository Info stamp (TODO-09-28 item 7): the BMC renumbers its dynamic
# sensors whenever the sensor set changes (et7b3 2026-09-29: HSC 0x17 -> 0x11,
# a cached `-S` read returned 1805.400 W with the host off). One extra
# `raw 0x0a 0x20` per cycle detects the change; the cached reading is then
# never trusted.
# ---------------------------------------------------------------------------

STAMP_A = " 51 68 00 ff ff 9d 7b bb 6a 99 7b bb 6a 83\n"   # 104 records
STAMP_B = " 51 64 00 ff ff a0 7b bb 6a a0 7b bb 6a 83\n"   # 100 records, later add/erase
CACHED_A = STAMP_A + CACHED


def _meta(tmp_path):
    return json.load(open(tmp_path / "c" / (MAC.replace(":", "") + ".json")))


def test_build_records_the_sdr_repository_stamp(tmp_path):
    r = Runner(stamp=STAMP_A, dump_records=104)
    assert _call(tmp_path, r) == ("on", "141.60 W")
    assert _meta(tmp_path)["sdr_stamp"] == [104, 0x6abb7b9d, 0x6abb7b99]
    assert stamp_calls(r) == 1
    assert all("raw 0x0a 0x20" not in (c["script"] or "") for c in r.calls)   # never in the walk


def test_steady_cycle_reads_the_stamp_in_the_same_session(tmp_path):
    _call(tmp_path, Runner(stamp=STAMP_A, dump_records=104))
    r = Runner(cached=CACHED_A)
    assert _call(tmp_path, r, now=1030.0) == ("on", "141.60 W")
    assert r.kinds() == ["cached"]
    assert "raw 0x0a 0x20" in r.calls[0]["script"]


def test_stamp_change_discards_the_reading_and_rebuilds(tmp_path):
    """The et7b3 case: the SDR was renumbered, so the cached name now points
    at another sensor and reads 1805.4. That value must never be returned."""
    _call(tmp_path, Runner(stamp=STAMP_A, dump_records=104), now=1000.0)
    r = Runner(cached=STAMP_B + "Chassis Power is off\nMB HSC In Power  | 1805.400\n",
               full="Chassis Power is off\nMB HSC In Power  | 11.80 Watts | ok\n",
               stamp=STAMP_B, dump_records=100)
    assert _call(tmp_path, r, now=1700.0) == ("off", "11.80 W")
    assert r.kinds() == ["cached", "dump", "full"]
    assert _meta(tmp_path)["sdr_stamp"][0] == 100


def test_stamp_change_inside_the_rate_limit_returns_power_only(tmp_path):
    _call(tmp_path, Runner(stamp=STAMP_A, dump_records=104), now=1000.0)
    r = Runner(cached=STAMP_B + "Chassis Power is off\nMB HSC In Power  | 1805.400\n")
    assert _call(tmp_path, r, now=1100.0) == ("off", None)
    assert r.kinds() == ["cached"]


def test_cache_from_before_the_stamp_rebuilds_once(tmp_path):
    cache_dir = tmp_path / "c"
    os.makedirs(cache_dir)
    key = MAC.replace(":", "").lower()
    (cache_dir / (key + ".sdr")).write_bytes(b"old-binary")
    (cache_dir / (key + ".json")).write_text(
        json.dumps({"name": "MB HSC In Power", "built": 100.0, "attempted": 100.0}))
    r = Runner(stamp=STAMP_A, dump_records=104)
    assert _call(tmp_path, r, now=1000.0) == ("on", "141.60 W")
    assert r.kinds() == ["dump", "full"]
    r2 = Runner(cached=CACHED_A)
    _call(tmp_path, r2, now=1030.0)
    assert r2.kinds() == ["cached"]


def test_answer_without_a_stamp_keeps_trusting_the_cache(tmp_path):
    """A BMC that doesn't answer the stamp must not be rebuilt every 600 s."""
    _call(tmp_path, Runner(stamp=STAMP_A, dump_records=104), now=1000.0)
    r = Runner(cached=CACHED)
    assert _call(tmp_path, r, now=1700.0) == ("on", "141.60 W")
    assert r.kinds() == ["cached"]


def test_dump_that_disagrees_with_the_stamp_is_rebuilt(tmp_path):
    """The SDR changed during the dump: its record count differs from the
    stamp read right after it, so the pair must not be trusted."""
    _call(tmp_path, Runner(stamp=STAMP_A, dump_records=100), now=1000.0)
    r = Runner(cached=CACHED_A)
    assert _call(tmp_path, r, now=1030.0) == ("on", None)     # not trusted, rate-limited
    assert r.kinds() == ["cached"]
    r2 = Runner(cached=CACHED_A, stamp=STAMP_A, dump_records=104)
    assert _call(tmp_path, r2, now=1700.0) == ("on", "141.60 W")
    assert r2.kinds() == ["cached", "dump", "full"]


def test_bmc_that_rejects_the_stamp_keeps_todays_behaviour(tmp_path):
    """ipmitool exec returns rc=1 if ANY command in the session fails
    (verified on et7b3 2026-09-29), so a BMC without Get SDR Repository Info
    must never get it inside a session: the build asks separately, and on
    failure the cycles never send it."""
    r = Runner(stamp_error=True)
    assert _call(tmp_path, r) == ("on", "141.60 W")
    assert r.kinds() == ["dump", "full"]
    assert _meta(tmp_path)["sdr_stamp"] is None
    r2 = Runner()
    assert _call(tmp_path, r2, now=1030.0) == ("on", "141.60 W")
    assert r2.kinds() == ["cached"]
    assert "raw 0x0a 0x20" not in r2.calls[0]["script"]
