#!/usr/bin/env python3
"""Physical network diagram of one site, generated from files/configs/<site>/devices.yml
(the same source as the site health board). Plain graphviz with render.py's sentinel
colours (reaper-devel docs/network/render.py maps them to theme variables):

    scripts/site_diagram.py --site eindhoven > docs/network/src/eindhoven-physical.dot
"""
import argparse
import re
import sys
from html import escape
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from reapercli.lib.sitemodel import load_model  # noqa: E402

INK, MUTED, SURFACE, RULE, SUNKEN = "#010101", "#020202", "#030303", "#040404", "#050505"
SPEED = {"1G": "#0a0a01", "10G": "#0a0a02", "25G": "#0a0a03", "100G": "#0a0a04"}
WAN, DOWN = "#0a0a05", "#0a0a06"
WIDTH = {"1G": 1.4, "10G": 2.2, "25G": 2.6, "100G": 3.2}


def port_id(port: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "_", port)


def _node(d, ports) -> str:
    live = d.state == "live"
    head = f"<B>{escape(d.name)}</B>" + (f"  {escape(d.mgmt_ip)}" if d.mgmt_ip else "")
    sub = " · ".join(x for x in (d.note, "" if live else d.state, f"BMC {d.bmc_ip}" if d.bmc_ip else "") if x)
    cells = "".join(f'<TD PORT="{port_id(p)}">{escape(p)}</TD>' for p in ports) or "<TD> </TD>"
    n = max(len(ports), 1)
    rows = (f'<TR><TD COLSPAN="{n}" BGCOLOR="{SUNKEN}">{head}</TD></TR>'
            + (f'<TR><TD COLSPAN="{n}"><FONT COLOR="{MUTED}">{escape(sub)}</FONT></TD></TR>' if sub else "")
            + f"<TR>{cells}</TR>")
    style = "" if live else ', style="dashed"'
    return (f'  "{d.name}" [label=<<TABLE BORDER="1" CELLBORDER="1" CELLSPACING="0" CELLPADDING="4" '
            f'COLOR="{RULE}" BGCOLOR="{SURFACE}"{" STYLE=" + chr(34) + "dashed" + chr(34) if not live else ""}>'
            f"{rows}</TABLE>>{style}]")


def render(model) -> str:
    ports = {d.name: [] for d in model.devices}
    for l in model.links:
        for e in (l.a, l.b):
            if e.port not in ports.setdefault(e.device, []):
                ports[e.device].append(e.port)
    out = [f"// {model.site} physical topology: GENERATED from reaper files/configs/{model.site}/devices.yml",
           "// by scripts/site_diagram.py. Edit devices.yml, not this file.",
           f"graph {re.sub(r'[^A-Za-z0-9_]', '_', model.site)}_physical {{",
           f'  graph [rankdir=TB, bgcolor="transparent", fontname="Helvetica", fontsize=10, fontcolor="{MUTED}",'
           " nodesep=0.3, ranksep=0.75, pad=0.15, splines=true]",
           f'  node  [shape=plain, fontname="Helvetica", fontsize=10, fontcolor="{INK}"]',
           f'  edge  [fontname="Helvetica", fontsize=8.5, fontcolor="{MUTED}"]']
    bangs = [d for d in model.devices if d.kind == "bang"]
    if bangs:
        out.append(f'  subgraph cluster_bangs {{ label="bangs (HA pair)"; labeljust=l; style="rounded"; color="{RULE}";')
        out += ["  " + _node(d, ports[d.name]) for d in bangs]
        out.append("  }")
    out += [_node(d, ports[d.name]) for d in model.devices if d.kind != "bang"]
    for l in model.links:
        a, b = model.device(l.a.device), model.device(l.b.device)
        down = l.expect == "down" or not (a and b and a.state == "live" and b.state == "live")
        uplink = any(x and x.kind == "uplink" for x in (a, b))
        colour = DOWN if down else WAN if uplink else SPEED.get(l.speed, MUTED)
        label = " · ".join(x for x in (l.speed, l.note) if x)
        attrs = [f'color="{colour}"', f"penwidth={WIDTH.get(l.speed, 1.6)}", f'label="{label}"']
        if down:
            attrs.append("style=dashed")
        out.append(f'  "{l.a.device}":{port_id(l.a.port)} -- "{l.b.device}":{port_id(l.b.port)} '
                   f"[{', '.join(attrs)}]")
    out.append("}")
    return "\n".join(out) + "\n"


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="site_diagram.py")
    p.add_argument("--site", required=True)
    a = p.parse_args(argv)
    sys.stdout.write(render(load_model(ROOT, a.site)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
