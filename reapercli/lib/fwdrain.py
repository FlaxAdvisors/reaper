"""The firmware agents (triage bmc_fw/bios_fw/fru_fw) must never be recreated mid-flash:
a recreate kills the job and can brick the chip. `fwdrain.py on <agent…>` stops each agent
starting new jobs and waits (up to 30 min) for the ones in flight; `off` releases it. A
recreated container clears its own drain, but one the deploy did NOT recreate keeps it for
ever -- so whoever drains, releases (memory fw-agents-drain-before-deploy)."""
from reapercli.lib.guards import Refused

DRAIN_TIMEOUT = 1900          # fwdrain.py on waits up to WAIT_S = 1800 s
DRAIN_DIR = "/run/flax/fw-drain"   # host path (bind-mounted /run/flax): the manual fallback


def running_agents(c, conts: dict) -> list:
    """The drainable services of `c` whose container is running in `conts` (probe facts)."""
    d = c.compose.drain if c.compose else None
    if not d:
        return []
    return [s for s in d["services"]
            if (conts.get(f"{c.compose.project}-{s}-1") or {}).get("state") == "running"]


def targets(v, comps: list) -> list:
    """[(bang, component, [agents])] to drain before deploying `comps` to site view `v`.
    Refused when a live bang's containers could not be read: an agent there may be mid-flash."""
    out = []
    for c in comps:
        if not (c.compose and c.compose.drain):
            continue
        for b in v.hosts.bangs:
            if b.dormant:
                continue
            conts = v.facts.get(b.name, {}).get("containers")
            if not isinstance(conts, dict) or "error" in conts:
                raise Refused(f"{v.site.name}/{b.name}: containers unreadable, cannot tell whether "
                              f"{c.name}'s fw agents are mid-flash")
            agents = running_agents(c, conts)
            if agents:
                out.append((b.name, c, agents))
    return out


def on_cmd(c, agents: list) -> str:
    return " ".join([c.compose.drain["cmd"], *agents])


def off_cmd(c, agents: list) -> str:
    return " ".join([c.compose.drain["release"], *agents])


def manual_release(bang: str, agents: list) -> str:
    return f"ssh {bang} sudo rm -f " + " ".join(f"{DRAIN_DIR}/{a}.drain" for a in agents)
