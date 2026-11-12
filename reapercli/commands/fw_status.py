"""reaper fw [site] status [bmc|bios] [<family>]: read-only.

Per site and lane, each family's target as reaper's inventory has it next to what the
rendered manifest on each bang says ("ns" = no target configured there), then when each
manifest was last written and when each reader of it started. A reader started before
its manifest was written still holds the old one (they load it once, at start).
One ssh per bang; nothing is changed.
"""
import json
import time

import yaml

from reapercli.lib.fwentry import LANES
from reapercli.lib.guards import Refused
from reapercli.lib.inventory import _Loader, site_hosts, ssh_target
from reapercli.lib.remote import fanout

NS = "ns"            # no target configured
UNKNOWN = "?"        # bang unreachable / file unreadable
POST_MANIFEST = "/etc/flax/bios-firmware-versions.json"
CONSOLE = "flax-stack-flax-control-1"

# (key, title, file); key "post" is post's own BIOS manifest (a list keyed by family)
_FILES = (("bmc", "bmc manifest", LANES["bmc"].manifest),
          ("bios", "host manifest", LANES["bios"].manifest),
          ("post", "post bios manifest", POST_MANIFEST))
# reader -> the manifest(s) it loads at start (a single-file bind mount, or read once)
_READERS = ((f"prod-{LANES['bmc'].agent}-1", ("bmc",)),
            (f"prod-{LANES['bios'].agent}-1", ("bios",)),
            (CONSOLE, ("bmc", "bios")),
            ("flax-post", ("bmc",)),
            ("flax-post-fwd", ("bmc",)),
            ("flax-post-biosd", ("post",)),
            ("flax-rackpost", ("bmc", "post")))
# host process (rack lane) that rereads the bmc + post BIOS manifests on each rack firmware run: never stale
PER_RUN = ("flax-rackpost",)
_CONTAINERS = tuple(r for r, _ in _READERS if r.startswith(("prod-", "flax-stack-")))
_UNITS = tuple(r for r, _ in _READERS if r not in _CONTAINERS)


def probe_cmd(vip: str = "") -> str:
    files = " ".join(f"{k}:{f}" for k, _, f in _FILES)
    fmt = "{{.State.Running}} {{.State.StartedAt}}"
    return (f"for x in {files}; do k=${{x%%:*}}; f=${{x#*:}}; "
            f"echo \"@@file $k $(stat -c %Y \"$f\" 2>/dev/null)\"; cat \"$f\" 2>/dev/null; echo; done; "
            f"for c in {' '.join(_CONTAINERS)}; do s=$(docker inspect -f '{fmt}' \"$c\" 2>/dev/null); "
            f"if [ \"${{s%% *}}\" = true ]; then echo \"@@start $c $(date -d \"${{s#* }}\" +%s)\"; "
            f"else echo \"@@start $c down\"; fi; done; "
            f"for u in {' '.join(_UNITS)}; do if systemctl is-active -q \"$u\"; then "
            f"echo \"@@start $u $(date -d \"$(systemctl show -p ActiveEnterTimestamp --value \"$u\")\" +%s)\"; "
            f"else echo \"@@start $u down\"; fi; done; "
            + (f"ip -o -4 addr | grep -qF ' inet {vip}/' && echo @@vip; " if vip else "") + "true")


def parse_probe(out: str) -> dict:
    """{"files": {key: {"mtime": int|None, "data": obj|None}}, "starts": {reader: int|None}, "vip": bool}"""
    files, starts, cur, buf, vip = {}, {}, None, [], False

    def flush():
        if cur is not None:
            text = "\n".join(buf).strip()
            try:
                files[cur]["data"] = json.loads(text) if text else None
            except ValueError:
                files[cur]["data"] = None
    for line in out.splitlines():
        if line.startswith("@@file "):
            flush()
            parts = line.split()
            cur, buf = parts[1], []
            files[cur] = {"mtime": int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else None,
                          "data": None}
        elif line == "@@vip":
            flush()
            cur, vip = None, True
        elif line.startswith("@@start "):
            flush()
            cur = None
            parts = line.split()
            starts[parts[1]] = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else None
        elif cur is not None:
            buf.append(line)
    flush()
    return {"files": files, "starts": starts, "vip": vip}


def _vars(site) -> dict:
    f = site.inventory / "group_vars" / "all" / "vars.yml"
    return (yaml.load(f.read_text(), Loader=_Loader) or {}) if f.exists() else {}


def _target(v) -> str:
    return str(v) if v not in (None, "") else NS


def reaper_targets(d: dict) -> dict:
    """{"bmc": {family: target}, "bios": {...}, "post": {family: target}} from one site's vars."""
    out = {lane: {k: _target(e.get("target_version")) for k, e in (d.get(lp.var) or {}).items()
                  if isinstance(e, dict)} for lane, lp in LANES.items()}
    out["post"] = _post(d.get("post_bios_fw_platforms") or [])
    return out


def _post(items: list) -> dict:
    """post's BIOS list by family; an entry with no family (the pre-inventory static file) by [platform]."""
    return {(e.get("family") or f"[{e.get('platform') or '?'}]"): _target(e.get("target"))
            for e in items if isinstance(e, dict)}


def post_links(d: dict) -> dict:
    return {e["family"]: e.get("host_fw_family") for e in d.get("post_bios_fw_platforms") or []
            if isinstance(e, dict) and e.get("family")}


def site_targets(probe: dict) -> dict:
    """Same shape as reaper_targets, from a bang's rendered manifests; None = unreadable."""
    f = probe.get("files", {})
    out = {}
    for lane in LANES:
        data = (f.get(lane) or {}).get("data")
        out[lane] = ({k: _target((e or {}).get("target_version")) for k, e in data.items()
                      if isinstance(e, dict)} if isinstance(data, dict) else None)
    data = (f.get("post") or {}).get("data")
    out["post"] = _post(data) if isinstance(data, list) else None
    return out


def _fmt_time(t) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(t))


def _table(rows: list, indent: str = "  ") -> list:
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    return [(indent + "  ".join(c.ljust(w) for c, w in zip(r, widths))).rstrip() for r in rows]


def _keys(lane: str) -> tuple:
    return ("bmc",) if lane == "bmc" else ("bios", "post") if lane == "bios" else ("bmc", "bios", "post")


def render(site_name: str, holder: str, bangs: list, reaper: dict, links: dict, probes: dict,
           families: dict, lane: str, family: str, notes: list) -> list:
    """Lines for one site, one aligned table. `families` = {key: sorted names to show};
    probes[bang] None = unreachable."""
    out = [f"{site_name}" + (f"  (VIP on {holder})" if holder else "")]
    out += [f"  note  {n}" for n in notes]
    on_site = {b: (site_targets(p) if p else None) for b, p in probes.items()}
    titles = {"bmc": "bmc", "bios": "bios", "post": "post bios"}
    rows = [["", "", "reaper", *bangs]]
    for key in _keys(lane):
        names = families.get(key) or []
        if family:
            names = [n for n in names if n == family or (key == "post" and links.get(n) == family)]
        for i, n in enumerate(names):
            label = f"{n} ({links[n]})" if key == "post" and links.get(n) else n
            cells = [UNKNOWN if on_site[b] is None or on_site[b][key] is None else on_site[b][key].get(n, NS)
                     for b in bangs]
            rows.append([titles[key] if i == 0 else "", label, reaper[key].get(n, NS), *cells])
    shown = set(_keys(lane))
    times = [[title, *[_cell(probes[b], key) for b in bangs]] for key, title, _ in _FILES if key in shown]
    times += [[reader, *[_reader_cell(probes[b], reader, [k for k in reads if k in shown]) for b in bangs]]
              for reader, reads in _READERS if shown & set(reads)]
    rows += [["written" if i == 0 else "", r[0], "", *r[1:]] for i, r in enumerate(times[:sum(
        1 for k, _, _ in _FILES if k in shown)])]
    rows += [["started" if i == 0 else "", r[0], "", *r[1:]] for i, r in enumerate(times[sum(
        1 for k, _, _ in _FILES if k in shown):])]
    return out + _table(rows)


def _cell(probe, key) -> str:
    if probe is None:
        return UNKNOWN
    m = (probe["files"].get(key) or {}).get("mtime")
    return _fmt_time(m) if m else "missing"


def _reader_cell(probe, reader, reads) -> str:
    if probe is None:
        return UNKNOWN
    t = probe["starts"].get(reader)
    if t is None:
        return "down"
    if reader in PER_RUN:
        return _fmt_time(t) + " (reads per run)"
    stale = any((m := (probe["files"].get(k) or {}).get("mtime")) and t < m for k in reads)
    return _fmt_time(t) + (" *" if stale else "")


def run(sites: list, rest: list, ctx) -> int:
    lane = rest[0] if rest and rest[0] in LANES else ""
    rest = rest[1:] if lane else rest
    if len(rest) > 1:
        raise Refused("usage: reaper fw [site] status [bmc|bios] [<family>]")
    family = rest[0] if rest else ""
    per_site, rc = [], 0
    for site in sites:
        d = _vars(site)
        sh = site_hosts(site, ctx.root, ctx.hostname)
        live = [b for b in sh.bangs if not b.dormant]
        res = fanout({b.name: ssh_target(b, sh.on_site) for b in live}, probe_cmd(sh.vip), None, 60, ctx.runner)
        probes, notes = {}, [f"{b.name} dormant: not probed" for b in sh.bangs if b.dormant]
        for b in live:
            r = res[b.name]
            if r.rc or "@@file" not in r.out:
                probes[b.name] = None
                notes.append(f"{b.name} unreachable: {(r.err or r.out).strip()[-120:] or f'rc {r.rc}'}")
                rc = 1
            else:
                probes[b.name] = parse_probe(r.out)
        held = [b for b, p in probes.items() if p and p["vip"]]
        holder = held[0] if sh.standby and len(held) == 1 else ""
        per_site.append((site, sh, d, [b.name for b in live], probes, notes, holder))

    # every family any active site's inventory (or any probed bang) knows, so one a site
    # does not configure reads "ns" there, also when that site is shown alone
    from reapercli.lib.sites import load_sites
    families = {k: set() for k in ("bmc", "bios", "post")}
    for s in load_sites(ctx.root).values():
        if s.state == "active":
            for k, v in reaper_targets(_vars(s)).items():
                families[k] |= set(v)
    for _, _, d, _, probes, _, _ in per_site:
        for p in probes.values():
            for k, v in (site_targets(p) if p else {}).items():
                families[k] |= set(v or {})
    if family and not any(family in families[k] for k in _keys(lane)) and not any(
            post_links(d).get(n) == family for _, _, d, *_ in per_site for n in families["post"]):
        raise Refused(f"no family {family!r} in reaper or on any bang"
                      + (f" ({lane})" if lane else ""))
    fams = {k: sorted(v) for k, v in families.items()}
    links = {}
    for _, _, d, *_ in per_site:
        links.update({k: v for k, v in post_links(d).items() if v})

    print(f"times local ({time.strftime('%Z')}); ns = no target configured; ? = unreachable/unreadable; "
          "* = started before a manifest it reads was written (still holds the old one)")
    for site, sh, d, bangs, probes, notes, holder in per_site:
        print()
        for line in render(site.name, holder, bangs, reaper_targets(d), links, probes, fams, lane, family,
                           notes):
            print(line)
    return rc
