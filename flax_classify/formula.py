"""Pure (switch, port, kind, vid) -> (ipv4_address, hostname) formula.

Lifted from scripts/reaper_leased.py:alloc_ip + _port_token. Behaviour
kept byte-identical so the shadow-mode comparison against reaper-leased's
/etc/dnsmasq.dhcp-hosts/* output is a true byte-compare.
"""

import ipaddress

INFRA_SLOTS = {1, 2, 3, 4, 5}


def _normalise_rabbit_port(port):
    """Et6/3, Ethernet6/3, et6b3 -> (6, 3). Raises ValueError otherwise."""
    p = port.strip().lower()
    if p.startswith("ethernet"):
        rest = p[len("ethernet"):]
    elif p.startswith("et"):
        rest = p[len("et"):]
    else:
        raise ValueError(f"unrecognised rabbit port: {port!r}")
    if "/" in rest:
        a, b = rest.split("/", 1)
    elif "b" in rest:
        a, b = rest.split("b", 1)
    else:
        raise ValueError(f"unrecognised rabbit port: {port!r}")
    return int(a), int(b)


def _normalise_turtle_port(port):
    """swp<N> -> N. Raises ValueError otherwise."""
    p = port.strip().lower()
    if not p.startswith("swp"):
        raise ValueError(f"unrecognised turtle port: {port!r}")
    return int(p[3:])


def alloc_ip(switch, port, vid, kind, vm_n=None):
    """Compute the reserved IPv4 for (switch, port, vid, kind, vm_n).

    Conventions (per reaper_leased.alloc_ip, lifted verbatim):
      rabbit Et<P>/<S>:
        host: 172.<vid>.<P>.<S>
        bmc:  172.<vid>.<P>.<S+100>
        vm:   172.<vid>.<P>.<200+N>
      turtle swp<N>:
        host: 172.<vid>.0.<N>       (N>5; 1..5 are infra slots)
        bmc:  172.<vid>.0.<N+100>
        vm:   172.<vid>.0.<200+N>
    """
    if kind not in ("host", "bmc", "vm"):
        raise ValueError(f"unsupported kind: {kind!r}")
    sw = switch.lower()
    if sw.startswith("rabbit"):
        p, s = _normalise_rabbit_port(port)
        if kind == "vm":
            if vm_n is None or vm_n < 1:
                raise ValueError("vm allocation requires vm_n >= 1")
            last = 200 + vm_n
        elif kind == "bmc":
            last = s + 100
        else:
            last = s
        return f"172.{vid}.{p}.{last}"
    if sw.startswith("turtle"):
        n = _normalise_turtle_port(port)
        if n in INFRA_SLOTS:
            raise ValueError(f"swp{n} collides with infra slot 172.{vid}.0.{n}")
        if kind == "vm":
            if vm_n is None or vm_n < 1:
                raise ValueError("vm allocation requires vm_n >= 1")
            last = 200 + vm_n
        elif kind == "bmc":
            last = n + 100
        else:
            last = n
        return f"172.{vid}.0.{last}"
    raise ValueError(f"unsupported switch: {switch!r}")


def alloc_ip6(switch, port, vid, kind, vm_n=None):
    """Reserved IPv6 mirroring alloc_ip's 3rd+4th octets into the last two
    hextets, reading the same decimal: 172.16.7.101 -> fd00:16::7:101.
    Turtle's 0 third octet collapses in canonical IPv6 (fd00:17::108).
    Reuses alloc_ip (so it validates inputs + stays in lockstep with v4)."""
    v4 = alloc_ip(switch, port, vid, kind, vm_n=vm_n)
    oct3, oct4 = v4.split(".")[2], v4.split(".")[3]
    return str(ipaddress.IPv6Address(f"fd00:{vid}::{oct3}:{oct4}"))


def port_token(port):
    """Et6/3, Ethernet6/3 -> '6b3'. swp6 -> 'swp6'. et6b3 -> 'et6b3'."""
    p = port.lower().replace("ethernet", "et")
    if p.startswith("et") and "/" in p:
        a, b = p[2:].split("/", 1)
        return f"{a}b{b}"
    return p


def _hostname(switch, port, kind, family, vm_n=None):
    """{family}{port_token}[-bmc|vm<N>] for rabbit; {family}{port} for turtle."""
    sw = switch.lower()
    if sw.startswith("rabbit"):
        base = f"{family}{port_token(port)}"
    else:
        base = f"{family}{port}"
    if kind == "bmc":
        return f"{base}-bmc"
    if kind == "vm":
        return f"{base}vm{vm_n}"
    return base


def classify_one(switch, port, mac, kind, vid, *, family="unknown", vm_n=None):
    """Compute a single (mac, kind, vid) -> {ipv4_address, hostname} record.

    `mac` is included in the signature so callers can keep their feeder
    loops tight, but the formula itself doesn't read it. `family` is supplied
    by the caller from devices.latched (defaulting to 'unknown' when no device
    row exists); `vm_n` is supplied for vm targets.
    """
    return {
        "ipv4_address": alloc_ip(switch, port, vid, kind, vm_n=vm_n),
        "ipv6_address": alloc_ip6(switch, port, vid, kind, vm_n=vm_n),
        "hostname":     _hostname(switch, port, kind, family, vm_n=vm_n),
    }
