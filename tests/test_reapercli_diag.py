"""reaper diag (Phase 4): collect read-only, line up, write a bundle. Interpreting it is
a conversation, not the tool (spec 2026-11-08-reaper-diag-design)."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

pytest.importorskip("reapercli")
from reapercli.lib.components import load_components  # noqa: E402
from reapercli.lib.sites import ScopeError, load_sites  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SITES = load_sites(ROOT)
COMPS = load_components(ROOT, SITES)
EST = timezone(timedelta(hours=-5))
NOW = datetime(2026, 11, 9, 10, 0, tzinfo=EST)
H = timedelta(hours=1)


# ---- window ---------------------------------------------------------------------------

def test_relative_since():
    from reapercli.lib.diagwin import parse_since
    assert parse_since("2h", NOW) == NOW - 2 * H
    assert parse_since("3d", NOW) == NOW - timedelta(days=3)
    assert parse_since("30m", NOW) == NOW - timedelta(minutes=30)


def test_clock_since_is_local_today():
    from reapercli.lib.diagwin import parse_since
    assert parse_since("08:30", NOW) == NOW.replace(hour=8, minute=30)


def test_yesterday_and_date():
    from reapercli.lib.diagwin import parse_since
    assert parse_since("yesterday 14:00", NOW) == datetime(2026, 11, 8, 14, 0, tzinfo=EST)
    assert parse_since("2026-11-07 09:15", NOW) == datetime(2026, 11, 7, 9, 15, tzinfo=EST)


def test_future_since_is_usage_error():
    from reapercli.lib.diagwin import parse_since
    with pytest.raises(ScopeError, match="future"):
        parse_since("14:00", NOW)
    with pytest.raises(ScopeError):
        parse_since("last tuesday", NOW)


def test_default_window_is_12h():
    from reapercli.lib.diagwin import Window, window
    assert window(None, None, NOW) == Window(NOW - 12 * H, NOW, False)


def test_since_beyond_72h_is_clamped():
    from reapercli.lib.diagwin import window
    w = window("5d", None, NOW)
    assert w.start == NOW - 72 * H and w.clamped is True and w.end == NOW


def test_window_length_bounds_end():
    from reapercli.lib.diagwin import window
    assert window("yesterday 14:00", "30m", NOW).end == datetime(2026, 11, 8, 14, 30, tzinfo=EST)
    assert window("1h", "3h", NOW).end == NOW


# ---- service words --------------------------------------------------------------------

def test_resolve_words():
    from reapercli.lib.diagscope import resolve
    assert resolve(["kea"], COMPS) == ["kea"]
    core = sorted(n for n, c in COMPS.items() if c.group == "flax_core")
    assert core and resolve(["flax_core"], COMPS) == core
    assert resolve(["post"], COMPS) == sorted(set(resolve(["post-loose"], COMPS)) | set(resolve(["post-rack"], COMPS)))
    assert resolve([], COMPS) == sorted(COMPS)
    with pytest.raises(ScopeError, match="kea"):
        resolve(["nope"], COMPS)


# ---- bang-side collector ------------------------------------------------------------------

def _win():
    from reapercli.lib.diagwin import window
    return window("2h", None, NOW)


def _sh(site="eindhoven"):
    from reapercli.lib.inventory import site_hosts
    return site_hosts(SITES[site], ROOT, "orbit")


def test_remote_spec_for_kea():
    from reapercli.lib.diagscope import remote_spec
    w = _win()
    s = remote_spec(["kea"], COMPS, _sh(), w)
    assert "kea-dhcp4" in s["units"] and "kea-dhcp4" in s["containers"]
    assert s["kea"] and s["extras"] and not s["drain"]
    assert s["since"] == int(w.start.timestamp()) and s["until"] == int(w.end.timestamp())
    assert s["cap"] == 5_000_000 and s["vip"] == "192.168.88.10" and "##SQL" in s["extras_script"]


def test_remote_spec_triage_has_compose_and_drain():
    from reapercli.lib.diagscope import remote_spec
    s = remote_spec(["triage"], COMPS, _sh(), _win())
    assert s["compose"][0]["dir"] == "/etc/flax/triage" and s["drain"]
    assert not s["kea"] and not s["extras"]


ALWAYS = ("host.uptime", "host.boots", "host.df", "host.free", "host.failed", "host.ipaddr", "host.ss",
          "kernel.journal", "keepalived.active", "keepalived.journal", "docker.ps", "docker.daemon",
          "docker.events", "deployed", "pg.facts", "pg.log")


def _remote(monkeypatch, answers=None):
    import reapercli.diag_remote as dr
    cmds = []

    def run(cmd, timeout=60):
        cmds.append(cmd)
        for key, val in (answers or {}).items():
            if key in cmd:
                return val
        return 0, "", ""
    monkeypatch.setattr(dr, "run", run)
    return dr, cmds


def test_remote_sources_named_and_bounded(monkeypatch):
    from reapercli.lib.diagscope import remote_spec
    spec = remote_spec(["kea", "triage", "flax_observe"], COMPS, _sh(), _win())
    dr, cmds = _remote(monkeypatch, {"docker ps -a --format '{{.Names}}'": (0, "kea-dhcp4\nprod-api-1\n", "")})
    got = dr.gather(spec)
    for name in ALWAYS:
        assert name in got["sources"], name
    assert "unit.flax-observe.journal" in got["sources"] and "container.prod-api-1.logs" in got["sources"]
    assert "kea.status4" in got["sources"] and "triage.drain" in got["sources"]
    events = [c for c in cmds if "docker events" in c]
    assert events and all("--until" in c for c in events)
    import re
    writes = re.compile(r"\b(systemctl|docker)\s+(restart|start|stop|kill|rm)\b|compose\s+(up|down|restart)"
                        r"|\b(INSERT|UPDATE|DELETE|DROP|TRUNCATE)\b|\brm\s|fwdrain")
    for c in cmds:
        assert not writes.search(c), c


def test_absent_container_is_not_a_failure(monkeypatch):
    from reapercli.lib.diagscope import remote_spec
    spec = remote_spec(["kea"], COMPS, _sh(), _win())
    dr, cmds = _remote(monkeypatch, {"docker ps -a --format '{{.Names}}'": (0, "kea-dhcp4\n", "")})
    got = dr.gather(spec)["sources"]
    assert got["container.kea-dhcp6.logs"].get("absent") is True
    assert not [c for c in cmds if "docker logs" in c and "kea-dhcp6" in c]
    assert got["kea.status6"].get("absent") is True and got["kea.status4"].get("absent") is not True


def test_noise_is_filtered_on_the_bang(monkeypatch):
    from reapercli.lib.diagscope import remote_spec
    dr, cmds = _remote(monkeypatch)
    dr.gather(remote_spec([], COMPS, _sh(), _win()))
    (kernel,) = [c for c in cmds if "_TRANSPORT=kernel" in c]
    assert "--grep" in kernel and "oom" in kernel.lower()
    (events,) = [c for c in cmds if "docker events" in c]
    assert "--filter event=die" in events and "exec" not in events


def test_collect_reports_failures_per_bang():
    from reapercli.lib.diagscope import collect, remote_spec
    from reapercli.lib.remote import Result
    spec = remote_spec(["kea"], COMPS, _sh(), _win())

    def runner(argv, stdin, timeout):
        assert "SPEC = " in stdin and "gather" in stdin
        if "bang-edam" in argv[-2]:
            return Result(255, "", "ssh: connect to host bang-edam port 22: timed out")
        return Result(0, json.dumps({"now": 1, "sources": {}}), "")
    got = collect(_sh(), spec, runner=runner)
    assert "error" in got["bang-edam"] and "timed out" in got["bang-edam"]["error"]
    assert got["bang-gouda"] == {"now": 1, "sources": {}}


# ---- events, folding, reach ----------------------------------------------------------------

UTC = timezone.utc
T0 = datetime(2026, 11, 9, 13, 0, tzinfo=UTC)          # 08:00 EST, inside _win() (08:00-10:00 EST)


def _us(dt):
    return str(int(dt.timestamp() * 1_000_000))


def jline(dt, msg, prio="6", ident="Keepalived_vrrp", unit="keepalived.service"):
    return json.dumps({"__REALTIME_TIMESTAMP": _us(dt), "PRIORITY": prio, "SYSLOG_IDENTIFIER": ident,
                       "_SYSTEMD_UNIT": unit, "MESSAGE": msg})


def src(out, rc=0, truncated=False):
    return {"cmd": "x", "rc": rc, "out": out, "err": "", "truncated": truncated}


def evs(sources):
    from reapercli.lib.timeline import events
    return events("bang-gouda", sources, _win())


def test_keepalived_state_change_is_an_event_veth_noise_is_not():
    out = "\n".join([jline(T0, "(VI_1) Entering MASTER STATE"), jline(T0, "Unknown interface veth1a2b deleted"),
                     jline(T0, "Stopped LVS and VRRP High Availability Monitor.", ident="systemd")])
    got = evs({"keepalived.journal": src(out)})
    assert [e.text for e in got] == ["(VI_1) Entering MASTER STATE", "Stopped LVS and VRRP High Availability Monitor."]
    assert got[0].source == "keepalived" and got[0].t == T0 and got[0].bang == "bang-gouda"


def test_journal_message_as_byte_list_decodes():
    msg = list("Entering BACKUP STATE \xff".encode("latin-1"))
    (e,) = evs({"keepalived.journal": src(jline(T0, msg))})
    assert e.text.startswith("Entering BACKUP STATE")


def test_unit_lifecycle_and_warning_lines():
    out = "\n".join([jline(T0, "Started Flax observe.", ident="systemd", unit="init.scope"),
                     jline(T0, "polled 12 bmcs", prio="6", ident="flax-observe"),
                     jline(T0, "bmc et9b1 timed out", prio="3", ident="flax-observe")])
    got = evs({"unit.flax-observe.journal": src(out)})
    assert [e.text for e in got] == ["Started Flax observe.", "bmc et9b1 timed out"]
    assert {e.source for e in got} == {"flax-observe"}


def test_kernel_lines_are_events():
    (e,) = evs({"kernel.journal": src(jline(T0, "Out of memory: Killed process 4242 (python3)", ident="kernel"))})
    assert e.source == "kernel"


def test_docker_events_skip_exec_and_keep_die_with_exit():
    lines = [json.dumps({"Action": "exec_die", "time": int(T0.timestamp()), "Actor": {"Attributes": {"name": "x"}}}),
             json.dumps({"Action": "die", "time": int(T0.timestamp()), "timeNano": int(T0.timestamp()) * 10**9,
                         "Actor": {"Attributes": {"name": "flax-stack-flax-control-1", "exitCode": "137"}}})]
    (e,) = evs({"docker.events": src("\n".join(lines))})
    assert e.text == "flax-stack-flax-control-1 die exit=137" and e.source == "docker"


def test_container_log_error_lines_with_timestamp():
    out = ("2026-11-09T13:00:01.574550700Z 2026-11-09 13:00:01.574 INFO  [kea-dhcp4.commands] status-get\n"
           "2026-11-09T13:00:02.100000000Z 2026-11-09 13:00:02.100 ERROR [kea-dhcp4.dhcpsrv] lease db gone\n")
    (e,) = evs({"container.kea-dhcp4.logs": src(out)})
    assert e.source == "kea-dhcp4" and "lease db gone" in e.text
    assert e.t == datetime(2026, 11, 9, 13, 0, 2, 100000, tzinfo=UTC)


def test_pg_promote_is_an_event():
    out = "2026-11-09T13:00:00.000000000Z 2026-11-09 13:00:00.000 GMT [77] LOG:  received promote request\n"
    (e,) = evs({"pg.log": src(out)})
    assert e.source == "postgres" and "promote" in e.text


def test_deploy_record_in_window_only():
    recs = (json.dumps({"component": "flax_post", "commit": "674c988afce", "user": "dbahi", "from": "orbit",
                        "at": "2026-11-09T13:00:00Z"}, indent=1) + "\n"
            + json.dumps({"component": "kea", "commit": "aaaaaaa", "user": "x", "from": "y",
                          "at": "2026-11-01T00:00:00Z"}, indent=1))
    (e,) = evs({"deployed": src(recs)})
    assert e.text == "deploy flax_post 674c988 by dbahi from orbit" and e.source == "deploy"


def test_boot_in_window_is_an_event():
    boots = ("IDX BOOT ID                          FIRST ENTRY                 LAST ENTRY\n"
             "  0 aa5d88e7ccf84b67b7b2fa5c4129b518 Mon 2026-11-09 13:30:00 GMT Mon 2026-11-09 14:59:00 GMT\n")
    (e,) = evs({"host.boots": src(boots)})
    assert e.source == "boot" and e.t == datetime(2026, 11, 9, 13, 30, tzinfo=UTC)


def test_events_outside_window_dropped():
    old = datetime(2026, 11, 8, 1, 0, tzinfo=UTC)
    assert evs({"keepalived.journal": src(jline(old, "Entering MASTER STATE"))}) == []


def test_fold_masks_numbers_and_counts():
    from reapercli.lib.timeline import Event, fold
    many = [Event(T0 + timedelta(seconds=i), "bang-gouda", "flax-observe", f"retry {i} failed") for i in range(1, 701)]
    other = Event(T0 + timedelta(seconds=5), "bang-edam", "keepalived", "Entering BACKUP STATE")
    got = fold(many + [other])
    assert len(got) == 2 and got[0].count == 700 and got[0].last == T0 + timedelta(seconds=700)
    assert got[0].text == "retry 1 failed" and got[1] is other


def test_reach_reports_late_journal_truncation_and_container_start():
    from reapercli.lib.timeline import reach
    boots = "IDX ...\n  0 aa Mon 2026-11-09 13:30:00 GMT Mon 2026-11-09 14:59:00 GMT\n"
    inspect = json.dumps([{"Created": "2026-11-09T12:00:00.123456789Z", "State": {"StartedAt": "2026-11-09T13:30:00.5Z"}}])
    got = reach("bang-gouda", {"host.boots": src(boots), "keepalived.journal": src(""),
                               "container.kea-dhcp4.inspect": src(inspect), "container.kea-dhcp4.logs": src("", truncated=True),
                               "docker.daemon": src("Fri 2026-10-09 12:19:54 GMT\n"), "docker.events": src("")}, _win())
    assert ("journal", datetime(2026, 11, 9, 13, 30, tzinfo=UTC), False) in got
    assert ("kea-dhcp4", datetime(2026, 11, 9, 12, 0, 0, 123456, tzinfo=UTC), True) in got
    assert not [r for r in got if r[0] == "docker"]          # the daemon started before the window


# ---- bundle and command ---------------------------------------------------------------------

def _bang(holder: bool):
    ka = jline(T0 + timedelta(minutes=5 if holder else 1), "(VI_1) Entering MASTER STATE" if holder
               else "(VI_1) Entering BACKUP STATE")
    die = json.dumps({"Action": "die", "timeNano": int((T0 + timedelta(minutes=3)).timestamp()) * 10**9,
                      "Actor": {"Attributes": {"name": "flax-stack-flax-control-1", "exitCode": "137"}}})
    pgf = {"in_recovery": "f" if holder else "t", "alembic": "042000000001",
           "replication": "192.168.88.22|streaming|0" if holder else "", "slots": "", "wal_receiver":
           "" if holder else "streaming", "replay_lag_bytes": "0", "standby_signal": not holder}
    s = {"host.ipaddr": src("2: br0    inet 192.168.88.10/24 scope global" if holder else "2: br0 inet 192.168.88.22/24"),
         "host.boots": src("IDX\n  0 aa Tue 2026-11-03 08:42:33 GMT Mon 2026-11-09 14:59:00 GMT\n"),
         "host.failed": src("flax-observe.service loaded failed failed Flax observe\n" if holder else ""),
         "host.df": src("Filesystem 1024-blocks Used Available Capacity Mounted on\n/dev/sda2 100 90 10 90% /\n"),
         "host.ss": src("LISTEN 0 4096 0.0.0.0:22 0.0.0.0:*\n"),
         "keepalived.active": src("active\n"), "keepalived.journal": src(ka),
         "docker.ps": src(json.dumps({"Names": "flax-stack-flax-control-1", "State": "restarting", "Status": "Restarting (137)"})),
         "docker.events": src(die if holder else ""), "docker.daemon": src("Fri 2026-10-09 12:19:54 GMT\n"),
         "pg.facts": src(json.dumps(pgf)),
         "deployed": src(json.dumps({"component": "kea", "commit": "213dea2aaaa", "user": "dbahi", "from": "orbit",
                                     "at": "2026-11-09T13:02:00Z"}))}
    return {"now": int(T0.timestamp()), "sources": s}


def _ctx(tmp_path, collected):
    from reapercli.__main__ import Context
    from reapercli.lib.remote import Result
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    (home / ".vault_pass_eindhoven").write_text("x\n")

    def runner(argv, stdin, timeout):
        name = argv[-2].replace("flax@", "")
        d = collected.get(name)
        return Result(255, "", "ssh: timed out") if d is None else Result(0, json.dumps(d), "")
    return Context(root=ROOT, home=home, hostname="orbit", runner=runner)


def _run(tmp_path, collected, argv=("eindhoven", "kea")):
    from reapercli.commands.diag import run
    ctx = _ctx(tmp_path, collected)
    rc = run(list(argv), ctx, tz=EST, now=NOW)
    (d,) = list((ctx.home / "reaper-reports" / "eindhoven").glob("diag-*/"))
    return rc, d


def test_bundle_layout(tmp_path):
    import tarfile
    rc, d = _run(tmp_path, {"bang-gouda": _bang(True), "bang-edam": _bang(False)})
    assert rc == 0
    assert (d / "summary.txt").exists() and (d / "raw" / "bang-gouda" / "keepalived.journal.txt").exists()
    assert "Entering MASTER STATE" in (d / "raw" / "bang-gouda" / "keepalived.journal.txt").read_text()
    tar = d.parent / (d.name + ".tar.gz")
    assert tar.exists() and any(n.endswith("summary.txt") for n in tarfile.open(tar).getnames())


def test_summary_order_and_zone(tmp_path):
    rc, d = _run(tmp_path, {"bang-gouda": _bang(True), "bang-edam": _bang(False)})
    text = (d / "summary.txt").read_text()
    assert "UTC-05:00" in text.split("NOW")[0]
    assert text.index("NOW") < text.index("TIMELINE")
    tl = text.split("TIMELINE", 1)[1]
    order = [tl.index(s) for s in ("08:01:00  bang-edam", "08:02:00  bang-gouda  deploy",
                                   "08:03:00  bang-gouda  docker", "08:05:00  bang-gouda  keepalived")]
    assert order == sorted(order)
    now = text.split("NOW", 1)[1].split("TIMELINE")[0]
    assert "VIP holder: bang-gouda" in now and "flax-observe" in now and "restarting" in now and "90%" in now
    assert "streaming" in now


def test_failed_bang_still_writes_and_exits_1(tmp_path):
    rc, d = _run(tmp_path, {"bang-gouda": _bang(True)})
    assert rc == 1
    head = (d / "summary.txt").read_text().split("NOW")[0]
    assert "bang-edam" in head and "timed out" in head
    assert (d / "raw" / "bang-gouda" / "pg.facts.txt").exists()


def test_failed_source_listed_and_exits_1(tmp_path):
    g = _bang(True)
    g["sources"]["docker.events"] = {"cmd": "", "rc": 124, "out": "", "err": "docker not answering", "truncated": False}
    rc, d = _run(tmp_path, {"bang-gouda": g, "bang-edam": _bang(False)})
    assert rc == 1 and "docker.events" in (d / "summary.txt").read_text().split("NOW")[0]
    assert (d / "raw" / "bang-gouda" / "docker.events.txt").read_text().startswith("# rc=124")


def test_bundle_never_inside_reaper_root(tmp_path, monkeypatch):
    import reapercli.commands.diag as diag
    from reapercli.lib.guards import Refused
    monkeypatch.setattr(diag, "report_dir", lambda ctx, site, now: ROOT / "reports" / site / "x")
    ctx = _ctx(tmp_path, {"bang-gouda": _bang(True), "bang-edam": _bang(False)})
    with pytest.raises(Refused, match="secrets"):
        diag.run(["eindhoven"], ctx, tz=EST, now=NOW)
    assert not (ROOT / "reports").exists()


def test_cli_usage(tmp_path, capsys):
    from reapercli.__main__ import main
    ctx = _ctx(tmp_path, {})
    assert main(["diag", "eindhoven", "nope"], ctx) == 2
    assert "unknown service 'nope'" in capsys.readouterr().err
    assert main(["diag", "eindhoven", "--since", "tomorrow"], ctx) == 2
    assert "bad --since" in capsys.readouterr().err


# ---- live-run fixes ----------------------------------------------------------------------

def test_fold_keeps_separate_cycles_apart():
    from reapercli.lib.timeline import Event, fold
    cycle = ["Stopping LVS", "Deactivated", "Stopped LVS", "Started LVS", "Entering BACKUP STATE"]
    evs = [Event(T0 + timedelta(minutes=10 * c, seconds=i), "bang-edam", "keepalived", m)
           for c in range(3) for i, m in enumerate(cycle)]
    got = fold(evs)
    assert [e.text for e in got].count("Entering BACKUP STATE") == 3 and all(e.count == 1 for e in got)


def test_fold_merges_an_alternating_pair():
    from reapercli.lib.timeline import Event, fold
    evs = [Event(T0 + timedelta(seconds=i), "bang-gouda", "smartctl", "open failed" if i % 2 else "identify failed")
           for i in range(100)]
    got = fold(evs)
    assert len(got) == 2 and {e.count for e in got} == {50}


def test_postgres_container_only_as_pg_log(monkeypatch):
    from reapercli.lib.diagscope import remote_spec
    dr, cmds = _remote(monkeypatch, {"docker ps -a --format '{{.Names}}'": (0, "flax-stack-postgres-1\n", "")})
    got = dr.gather(remote_spec(["flax_stack"], COMPS, _sh(), _win()))["sources"]
    assert "pg.log" in got and "container.flax-stack-postgres-1.logs" not in got


def test_fold_a_long_noisy_cycle_and_keep_rare_events():
    from reapercli.lib.timeline import Event, fold
    noise = [Event(T0 + timedelta(seconds=i), "bang-fiesta", "smartctl", m)
             for i in range(400) for m in [("open failed sda", "denied sda", "open failed sdb", "denied sdb")[i % 4]]]
    rare = [Event(T0 + timedelta(minutes=m), "bang-fiesta", "smartctl", "exporter restarted") for m in (1, 2)]
    got = fold(noise + rare)
    assert len(got) == 6 and sorted(e.count for e in got) == [1, 1, 100, 100, 100, 100]


def test_same_line_twice_in_one_second_is_one_event():
    out = "\n".join([jline(T0, "Stopped LVS and VRRP High Availability Monitor.", ident="systemd"),
                     jline(T0 + timedelta(milliseconds=300), "Stopped LVS and VRRP High Availability Monitor.", ident="systemd"),
                     jline(T0 + timedelta(minutes=5), "Stopped LVS and VRRP High Availability Monitor.", ident="systemd")])
    got = evs({"keepalived.journal": src(out)})
    assert [e.t for e in got] == [T0, T0 + timedelta(minutes=5)]


# ---- final review fixes ----------------------------------------------------------------------

def _inspect(name, started, finished="0001-01-01T00:00:00Z", code=0, oom=False, running=True):
    return src(json.dumps([{"Name": "/" + name, "Created": "2026-11-09T12:00:00.1Z",
                            "State": {"Running": running, "StartedAt": started, "FinishedAt": finished,
                                      "ExitCode": code, "OOMKilled": oom}}]))


def test_container_lifecycle_from_inspect():
    got = evs({"container.kea-dhcp4.inspect": _inspect("kea-dhcp4", "2026-11-09T13:10:00.5Z"),
               "container.flax-x-1.inspect": _inspect("flax-x-1", "2026-11-09T13:05:00Z", "2026-11-09T13:20:00Z",
                                                      137, True, False)})
    texts = [(e.t.strftime("%H:%M"), e.source, e.text) for e in got]
    assert ("13:10", "docker", "kea-dhcp4 start") in texts
    assert ("13:20", "docker", "flax-x-1 die exit=137 oom") in texts


def test_docker_events_reach_is_always_stated():
    from reapercli.lib.timeline import reach
    got = reach("bang-gouda", {"docker.events": src(""), "docker.daemon": src("Fri 2026-10-09 12:19:54 GMT\n")}, _win())
    assert any(r[0].startswith("docker events") and "256" in r[0] for r in got)


def test_kernel_journal_spans_boots_and_is_not_masked(monkeypatch):
    from reapercli.lib.diagscope import remote_spec
    dr, cmds = _remote(monkeypatch, {"_TRANSPORT=kernel": (1, "", "sudo: a password is required")})
    got = dr.gather(remote_spec([], COMPS, _sh(), _win()))["sources"]
    (kernel,) = [c for c in cmds if "_TRANSPORT=kernel" in c]
    assert " -k " not in kernel and "$?" not in kernel
    assert got["kernel.journal"]["rc"] == 1                     # a real failure stays a failure
    dr, _ = _remote(monkeypatch, {"_TRANSPORT=kernel": (1, "", "")})
    assert dr.gather(remote_spec([], COMPS, _sh(), _win()))["sources"]["kernel.journal"]["rc"] == 0   # no match


def test_pg_noise_filtered_in_python_before_the_cap(monkeypatch):
    from reapercli.lib.diagscope import remote_spec
    noise = "2026-11-09T13:00:00Z LOG:  connection received: host=1.2.3.4\n" * 50
    dr, cmds = _remote(monkeypatch, {"flax-stack-postgres-1 2>&1": (0, noise + "2026-11-09T13:00:01Z LOG: received promote request\n", "")})
    spec = remote_spec([], COMPS, _sh(), _win())
    spec["cap"] = 200
    got = dr.gather(spec)["sources"]["pg.log"]
    assert "promote" in got["out"] and "connection received" not in got["out"] and not got["truncated"]
    assert not [c for c in cmds if "grep -Ev" in c]


def test_truncated_or_bad_docker_events_still_parse():
    good = json.dumps({"Action": "die", "time": int(T0.timestamp()), "Actor": {"Attributes": {"name": "a", "exitCode": "1"}}})
    got = evs({"docker.events": src('ime":1}\n' + good + "\n{not json}\n" + good.replace('"a"', '"b"'))})
    assert [e.text for e in got] == ["a die exit=1", "b die exit=1"]


def test_bad_deploy_record_does_not_hide_the_next():
    rec = json.dumps({"component": "kea", "commit": "aaaaaaa", "user": "u", "from": "f", "at": "2026-11-09T13:00:00Z"}, indent=1)
    got = evs({"deployed": src("{broken\n" + rec)})
    assert [e.source for e in got] == ["deploy"]


def test_fold_keeps_docker_names_and_exit_codes():
    from reapercli.lib.timeline import Event, fold
    evs_ = [Event(T0 + timedelta(seconds=i), "bang-gouda", "docker",
                  f"kea-dhcp{4 if i % 2 else 6} die exit={137 if i > 9 else 0}") for i in range(12)]
    texts = {e.text for e in fold(evs_)}
    assert {"kea-dhcp4 die exit=137", "kea-dhcp6 die exit=137", "kea-dhcp4 die exit=0", "kea-dhcp6 die exit=0"} <= texts


def test_gather_has_a_global_deadline(monkeypatch):
    import reapercli.diag_remote as dr
    from reapercli.lib.diagscope import remote_spec
    clock = [0.0]
    monkeypatch.setattr(dr.time, "monotonic", lambda: clock[0])

    def run(cmd, timeout=60):
        clock[0] += 40
        return 0, "x", ""
    monkeypatch.setattr(dr, "run", run)
    spec = remote_spec(["kea"], COMPS, _sh(), _win())
    got = dr.gather(spec)["sources"]
    skipped = [n for n, s in got.items() if "deadline" in s.get("err", "")]
    assert skipped and all(got[n]["rc"] == 124 for n in skipped)
    assert spec["deadline"] < 180


def test_multivalued_and_null_journal_fields_do_not_crash():
    lines = [json.dumps({"__REALTIME_TIMESTAMP": _us(T0), "PRIORITY": ["3", "3"], "SYSLOG_IDENTIFIER": ["a", "b"],
                         "MESSAGE": ["boom", "boom"]}),
             json.dumps({"__REALTIME_TIMESTAMP": _us(T0), "PRIORITY": "3", "MESSAGE": None}),
             json.dumps({"__REALTIME_TIMESTAMP": [_us(T0)], "PRIORITY": "3", "MESSAGE": "x"})]
    got = evs({"unit.kea-dhcp4.journal": src("\n".join(lines))})
    assert "boom" in [e.text for e in got]


def test_journal_command_asks_for_long_fields(monkeypatch):
    from reapercli.lib.diagscope import remote_spec
    dr, cmds = _remote(monkeypatch)
    dr.gather(remote_spec(["kea"], COMPS, _sh(), _win()))
    assert all(" --all" in c for c in cmds if "journalctl" in c and "-o json" in c)


def test_bad_date_is_usage_error():
    from reapercli.lib.diagwin import parse_since
    with pytest.raises(ScopeError):
        parse_since("2026-13-01 10:00", NOW)


def test_clamped_since_with_window_is_not_shifted():
    from reapercli.lib.diagwin import window
    with pytest.raises(ScopeError, match="72 h"):
        window("2026-11-01 14:00", "2h", NOW)
    w = window("4d", "26h", NOW)                       # 4d ago + 26h = 70h ago -> inside the limit
    assert w.start == NOW - 72 * H and w.end == NOW - timedelta(hours=70) and w.clamped


def test_local_clock_time_is_dst_aware():
    from reapercli.lib.diagwin import parse_since
    now = datetime(2026, 11, 2, 10, 0).astimezone()   # the system zone, whatever it is
    got = parse_since("2026-10-31 14:00", now, local=True)
    assert got == datetime(2026, 10, 31, 14, 0).astimezone()


def test_bundle_is_private(tmp_path):
    import stat
    rc, d = _run(tmp_path, {"bang-gouda": _bang(True), "bang-edam": _bang(False)})
    assert stat.S_IMODE(d.stat().st_mode) == 0o700
    assert stat.S_IMODE((d.parent / (d.name + ".tar.gz")).stat().st_mode) == 0o600


# ---- deferred minors from the Phase 4 review (FOLLOWUP-20261109 item 7) ------------------------

BOOTS2 = ("IDX BOOT ID                          FIRST ENTRY                 LAST ENTRY\n"
          " -1 bb5d88e7ccf84b67b7b2fa5c4129b518 Mon 2026-11-09 13:10:00 GMT Mon 2026-11-09 13:50:00 GMT\n"
          "  0 aa5d88e7ccf84b67b7b2fa5c4129b518 Mon 2026-11-09 14:00:00 GMT Mon 2026-11-09 14:59:00 GMT\n")


def test_the_oldest_listed_boot_is_a_boot_only_when_its_kernel_start_survives():
    edge = evs({"host.boots": src(BOOTS2), "host.oldestboot": src("")})       # rotated past its start
    assert [e.t for e in edge if e.source == "boot"] == [datetime(2026, 11, 9, 14, 0, tzinfo=UTC)]
    real = evs({"host.boots": src(BOOTS2),
                "host.oldestboot": src("2026-11-09T13:10:00+0000 bang-gouda kernel: Linux version 6.4.0")})
    assert len([e for e in real if e.source == "boot"]) == 2


def test_a_truncated_source_reaches_back_to_its_oldest_kept_line():
    from reapercli.lib.timeline import reach
    kept = T0 + timedelta(minutes=30)
    got = reach("bang-gouda", {"keepalived.journal": src(jline(kept, "Entering MASTER STATE"), truncated=True),
                               "pg.log": src(f"{kept.strftime('%Y-%m-%dT%H:%M:%S')}.5Z LOG: x\n", truncated=True)},
                _win())
    assert ("keepalived", kept, True) in got
    assert ("postgres", kept.replace(microsecond=500000), True) in got
    assert not [r for r in got if r[1] is None and r[0] in ("keepalived", "postgres")]


def test_container_log_reach_follows_json_file_rotation():
    from reapercli.lib.timeline import reach
    inspect = json.dumps([{"Created": "2026-11-09T12:00:00Z", "State": {}}])
    oldest = json.dumps({"log": "x\n", "stream": "stdout", "time": "2026-11-09T13:40:00.25Z"})
    got = reach("bang-gouda", {"container.kea-dhcp4.inspect": src(inspect),
                               "container.kea-dhcp4.logs": src(""),
                               "container.kea-dhcp4.logstart": src(oldest + "\n")}, _win())
    assert ("kea-dhcp4", datetime(2026, 11, 9, 13, 40, 0, 250000, tzinfo=UTC), False) in got


def test_the_collector_reads_the_oldest_boot_and_each_containers_oldest_log(monkeypatch):
    from reapercli.lib.diagscope import remote_spec
    spec = remote_spec(["kea"], COMPS, _sh(), _win())
    dr, cmds = _remote(monkeypatch, {"docker ps -a --format '{{.Names}}'": (0, "kea-dhcp4\n", ""),
                                     "--list-boots": (0, BOOTS2, "")})
    s = dr.gather(dict(spec, deadline=150))["sources"]
    assert "host.oldestboot" in s and "-b -1 " in s["host.oldestboot"]["cmd"]
    assert "container.kea-dhcp4.logstart" in s and "LogPath" in s["container.kea-dhcp4.logstart"]["cmd"]


def test_the_cap_counts_bytes_and_cuts_on_a_character():
    import reapercli.diag_remote as dr
    out, cut = dr.cap_tail("é" * 10, 9)               # 20 bytes of UTF-8
    assert cut and len(out.encode()) <= 9 and out == "é" * 4


def test_rc_3_and_4_are_information_only_for_systemctl():
    from reapercli.lib.diagout import failed
    assert not failed({"cmd": "systemctl status x.service", "rc": 3, "out": ""})
    assert failed({"cmd": "docker logs --timestamps x", "rc": 3, "out": ""})


def test_fold_boundary_nine_stay_ten_fold():
    from reapercli.lib.timeline import NOISE, Event, fold
    def burst(n):
        return [Event(T0 + timedelta(seconds=i), "bang-gouda", "flax-observe", f"retry {i}") for i in range(n)]
    assert NOISE == 10
    assert len(fold(burst(9))) == 9
    (one,) = fold(burst(10))
    assert one.count == 10
