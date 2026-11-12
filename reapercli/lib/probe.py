"""Probe every bang of a site with ONE ssh call each: the standalone
probe_remote.py runs under the bang's python3.12 and prints JSON facts."""
import json
from pathlib import Path

from reapercli.lib.inventory import ssh_target
from reapercli.lib.remote import fanout

_SCRIPT = Path(__file__).resolve().parent.parent / "probe_remote.py"


def build_spec(comps: dict, vip: str, toolchain: bool = False) -> dict:
    def union(attr):
        return sorted({x for c in comps.values() for x in getattr(c, attr)})
    return {
        "units": union("units"), "timers": union("timers"), "containers": union("containers"),
        "files": union("files"), "vip": vip, "toolchain": toolchain,
        "freshness": {c.freshness["table"]: c.freshness["column"]
                      for c in comps.values() if c.freshness},
    }


def probe_site(sh, comps: dict, *, timeout: float = 25, runner=None, toolchain: bool = False) -> dict:
    spec = build_spec(comps, sh.vip, toolchain)
    stdin = f"SPEC = {spec!r}\n" + _SCRIPT.read_text() + "\nmain(SPEC)\n"   # a python literal
    live = {b.name: ssh_target(b, sh.on_site) for b in sh.bangs if not b.dormant}
    results = fanout(live, "python3.12 -", stdin, timeout, runner)
    facts = {b.name: {"dormant": True} for b in sh.bangs if b.dormant}
    for name, r in results.items():
        if r.rc == 124:
            facts[name] = {"error": "ssh timeout", "unreachable": True}
        elif r.rc == 255:           # ssh itself failed: never got to run the probe
            last = (r.err.strip().splitlines() or [""])[-1]
            facts[name] = {"error": last or f"ssh rc=255, no message (LogLevel QUIET?): "
                                            f"ssh -v {live[name]} true", "unreachable": True}
        elif r.rc != 0:
            last = (r.err.strip().splitlines() or ["no output"])[-1]
            facts[name] = {"error": f"probe rc={r.rc}: {last}"}
        else:
            try:
                facts[name] = json.loads(r.out)
            except ValueError:
                facts[name] = {"error": "bad json"}
    return facts
