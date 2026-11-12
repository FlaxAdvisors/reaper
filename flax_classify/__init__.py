"""flax-classify — pure-formula (switch, port, mac, kind, vid) -> (ip, hostname).

Lifts the formulaic core of reaper_leased (alloc_ip, _port_token) into a
standalone service that writes proposals to public.classify_proposals.
Shadow mode: nobody consumes the table yet; operators byte-compare
against reaper-leased's live /etc/dnsmasq.dhcp-hosts/* output via
flax-control's /reservations page.
"""
from .version import __version__

__all__ = ["__version__"]
