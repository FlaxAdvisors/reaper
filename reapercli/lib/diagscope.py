"""reaper diag's scope: service words -> components, and what to collect on the bangs."""
from reapercli.lib.components import LANES
from reapercli.lib.sites import ScopeError


def _words(comps: dict) -> dict:
    words = {n: {n} for n in comps}
    for n, c in comps.items():
        if c.group:
            words.setdefault(c.group, set()).add(n)
        words.setdefault(c.lane, set()).add(n)
    words["post"] = words.get("post-loose", set()) | words.get("post-rack", set())
    for lane in LANES:
        words.setdefault(lane, set())
    return words


def resolve(words: list, comps: dict) -> list:
    if not words:
        return sorted(comps)
    table, out = _words(comps), set()
    for w in words:
        if w not in table:
            raise ScopeError(f"unknown service {w!r}: use a component, group or lane: "
                             f"{' '.join(sorted(table))}")
        out |= table[w]
    return sorted(out)


def remote_spec(names: list, comps: dict, sh, win) -> dict:
    from reapercli.lib.extras import SCRIPT
    sel = [comps[n] for n in names]

    def union(attr):
        return sorted({x for c in sel for x in getattr(c, attr)})
    return {
        "since": int(win.start.timestamp()), "until": int(win.end.timestamp()), "vip": sh.vip,
        "units": union("units"), "timers": union("timers"), "containers": union("containers"),
        "compose": [{"name": c.name, "dir": c.compose.dir, "file": c.compose.file,
                     "env_file": c.compose.env_file} for c in sel if c.compose],
        "kea": "kea" in names, "extras": bool({"kea", "flax_switch_sense"} & set(names)),
        "drain": "triage" in names, "cap": 5_000_000, "extras_script": SCRIPT,
        "deadline": 150,     # seconds on the bang, under collect()'s 180 s ssh timeout
    }


def collect(sh, spec: dict, runner=None, timeout: float = 180) -> dict:
    """One ssh call per live bang, like probe.probe_site; a dormant bang is skipped."""
    import json
    from pathlib import Path

    from reapercli.lib.inventory import ssh_target
    from reapercli.lib.remote import fanout
    script = (Path(__file__).resolve().parent.parent / "diag_remote.py").read_text()
    stdin = f"SPEC = {spec!r}\n{script}\nmain(SPEC)\n"
    live = {b.name: ssh_target(b, sh.on_site) for b in sh.bangs if not b.dormant}
    got = {b.name: {"dormant": True} for b in sh.bangs if b.dormant}
    for name, r in fanout(live, "python3.12 -", stdin, timeout, runner).items():
        if r.rc != 0:
            got[name] = {"error": "timeout" if r.rc == 124 else
                         (r.err.strip().splitlines() or [f"rc={r.rc}"])[-1][:200]}
            continue
        try:
            got[name] = json.loads(r.out)
        except ValueError:
            got[name] = {"error": "bad json from diag_remote"}
    return got
