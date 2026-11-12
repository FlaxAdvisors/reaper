"""Switch port names and the LLDP row, shared by rackpost (flax_rack) and the rungs.

Neutral ground: flax_rungs never imports flax_rack, so the naming both need lives
here (flax_rack.switches re-exports it). link_iface(sw, port, lane) is the NOS's
name for a geometry front-panel port, front_panel(sw, iface) the inverse; `sw` is a
geometry switch (attributes `nos`, `model`):
  sonic   Dell N3248TE   port p           = Ethernet(p-1)
          wedge100s      port p lane l    = Ethernet((p-1)*4 + l-1)
  eos                    port p [lane l]  = Ethernet<p>[/<l>]  (Et<p>/<l> accepted)
  cumulus                port p [lane l]  = swp<p>[s<l-1>]     (breakout lanes are 0-based)
  ios                    port p           = Gi1/0/<p>
"""
import re
from dataclasses import dataclass


@dataclass
class LldpEntry:
    local_iface: str
    remote_device: str    # the neighbour's system name (may be "localhost")
    remote_port: str      # its port id: the port's MAC on the live image, an ifname on a switch
    remote_descr: str = ""   # its port description (the live image's ifname)


# ---------------------------------------------------------------- port names
def wedge_iface(port: int, lane: int | None) -> str:
    """wedge100s front port P (1-based) lane L (1-based) -> EthernetN, 4 lanes per port."""
    return f"Ethernet{(port - 1) * 4 + (lane - 1 if lane else 0)}"


def link_iface(sw, port: int, lane: int | None) -> str:
    """Geometry port (+lane) on switch `sw` -> that switch's interface name."""
    nos = getattr(sw, "nos", "sonic")
    if nos == "eos":
        return f"Ethernet{port}" + (f"/{lane}" if lane else "")
    if nos == "cumulus":
        return f"swp{port}" + (f"s{lane - 1}" if lane else "")
    if nos == "ios":
        return f"Gi1/0/{port}"
    if getattr(sw, "model", None) == "wedge100s":
        return wedge_iface(port, lane)
    return f"Ethernet{port - 1}"           # N3248TE: front port N = EthernetN-1


_EOS = re.compile(r"(?:Ethernet|Et)(\d+)(?:/(\d+))?")
_CUM = re.compile(r"swp(\d+)(?:s(\d+))?")
_IOS = re.compile(r"(?:Gi|GigabitEthernet)\d+/\d+/(\d+)")


def front_panel(sw, iface: str) -> tuple[int, int | None] | None:
    """Switch `sw`'s interface name -> (front-panel port, lane or None); None for a
    name that is no front-panel port (PortChannel, Vlan, eth0 ...). A wedge's lane is
    always given (1..4)."""
    nos = getattr(sw, "nos", "sonic")
    iface = (iface or "").strip()
    if nos == "eos":
        m = _EOS.fullmatch(iface)
        return (int(m[1]), int(m[2]) if m[2] else None) if m else None
    if nos == "cumulus":
        m = _CUM.fullmatch(iface)
        return (int(m[1]), int(m[2]) + 1 if m[2] else None) if m else None
    if nos == "ios":
        m = _IOS.fullmatch(iface)
        return (int(m[1]), None) if m else None
    m = re.fullmatch(r"Ethernet(\d+)", iface)
    if not m:
        return None
    n = int(m[1])
    if getattr(sw, "model", None) == "wedge100s":
        port, lane = divmod(n, 4)
        return port + 1, lane + 1
    return n + 1, None
