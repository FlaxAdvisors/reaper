"""Arista EOS eAPI driver (JSON-RPC over HTTPS).

Persistent requests.Session per driver instance so TLS handshakes are
amortized over the lifetime of the daemon. Per the spec's
'Connection ownership' section.

EOS's eAPI offers legacy cipher suites (e.g. AES256-SHA) that Python 3.12's
default SSL context refuses. We mount a custom HTTPAdapter that uses
SECLEVEL=0 so older ciphers are accepted. Certificate verification is also
disabled by default — lab-internal switches use self-signed certs.
"""
import ssl

import requests
import urllib3
from requests.adapters import HTTPAdapter
from urllib3.poolmanager import PoolManager
from urllib3.util.ssl_ import create_urllib3_context


urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class EosError(Exception):
    """eAPI returned an error response or HTTP failure."""


class _LegacyCipherAdapter(HTTPAdapter):
    """Accepts legacy TLS cipher suites (SECLEVEL=0) so older Arista EOS
    eAPI handshakes succeed."""

    def init_poolmanager(self, *args, **kwargs):
        ctx = create_urllib3_context()
        ctx.set_ciphers("DEFAULT:@SECLEVEL=0")
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        kwargs["ssl_context"] = ctx
        return super().init_poolmanager(*args, **kwargs)


class EosDriver:
    def __init__(self, host: str, user: str, password: str, *,
                 verify_ssl: bool = False, timeout: float = 15.0):
        # 15s (not 5s): weak whitebox control planes (e.g. WEDGE100S) stall the
        # eAPI plane for several seconds during the hourly EOS `schedule
        # tech-support` job. A 5s read timeout trips reachable=false during the
        # spike, evicting the whole switch from observe's cache and de-latching
        # inventory. 15s rides out the spike; a genuinely dead switch still
        # fails (just later), and the per-port linkstate debounce absorbs it.
        self.host = host
        self._url = f"https://{host}/command-api"
        self._auth = (user, password)
        self._timeout = timeout
        self._verify = verify_ssl
        self._session = requests.Session()
        self._session.auth = self._auth
        self._session.verify = self._verify
        # Legacy-cipher adapter for all https:// requests this Session makes.
        self._session.mount("https://", _LegacyCipherAdapter())

    def _post(self, commands: list[str], fmt: str = "json") -> list[dict]:
        """Issue an eAPI runCmds with the given list of CLI commands.
        Returns the per-command result objects in the same order."""
        payload = {
            "jsonrpc": "2.0",
            "method": "runCmds",
            "params": {"version": 1, "cmds": commands, "format": fmt},
            "id": 1,
        }
        try:
            resp = self._session.post(self._url, json=payload, timeout=self._timeout)
            resp.raise_for_status()
        except requests.RequestException as e:
            raise EosError(f"{self.host}: {e}") from e
        body = resp.json()
        if "error" in body:
            raise EosError(f"{self.host}: {body['error']}")
        return body["result"]

    def refresh(self) -> None:
        """No-op: eAPI fetches live per read. Present so _poll_once can call
        driver.refresh() uniformly (the IOS driver uses it to batch one ssh)."""

    # -- public read methods --

    def interfaces_status(self) -> dict[str, str]:
        """Return {port_name: 'link' | 'nolink' | 'unknown'} for every port."""
        out: dict[str, str] = {}
        result = self._post(["show interfaces status"])
        statuses = result[0].get("interfaceStatuses", {})
        for port, info in statuses.items():
            link = info.get("linkStatus", "").lower()
            if link == "connected":
                out[port] = "link"
            elif link in ("notconnect", "disabled", "down"):
                out[port] = "nolink"
            else:
                out[port] = "unknown"
        return out

    def vlans(self) -> dict[str, dict]:
        """Return {port_name: {access_vid: int | None, trunk: bool}} for every port
        that appears in the VLAN table. Caller fills in defaults for ports not
        listed (treated as untagged on vid 1)."""
        out: dict[str, dict] = {}
        result = self._post(["show vlan"])
        vlans = result[0].get("vlans", {})
        for vid_str, entry in vlans.items():
            vid = int(vid_str)
            for port in entry.get("interfaces", {}).keys():
                slot = out.setdefault(port, {"access_vid": None, "trunk": False})
                # Arista lists every VLAN a port can carry; access ports
                # appear under exactly one VLAN, trunks appear under many.
                if slot["access_vid"] is None:
                    slot["access_vid"] = vid
                else:
                    slot["trunk"] = True
        return out

    def mac_address_table(self) -> list[tuple[int, str, str]]:
        """Return [(vlan, mac, port_name)] for every DYNAMIC unicast entry.
        Static + multicast + cpu-injected entries are skipped -- they're
        infrastructure, not DUT presence."""
        result = self._post(["show mac address-table"])
        out: list[tuple[int, str, str]] = []
        entries = result[0].get("unicastTable", {}).get("tableEntries", [])
        for e in entries:
            if e.get("entryType", "").lower() != "dynamic":
                continue
            iface = e.get("interface", "")
            if iface in ("", "Cpu"):
                continue
            try:
                vlan = int(e["vlanId"])
                mac = e["macAddress"].lower()
            except (KeyError, ValueError, AttributeError):
                continue
            out.append((vlan, mac, iface))
        return out

    def lldp_neighbors_detail(self) -> dict[str, list[dict]]:
        """Return {port_name: [{mac, sysname, port_description, mgmt_addrs[]}]}.
        Per-port list because a port can have multiple LLDP neighbors (rare;
        seen on misconfigured trunks)."""
        result = self._post(["show lldp neighbors detail"])
        out: dict[str, list[dict]] = {}
        neighbors = result[0].get("lldpNeighbors", {})
        for port, info in neighbors.items():
            entries = info.get("lldpNeighborInfo", [])
            for n in entries:
                mac = (n.get("chassisId") or "").lower()
                if not mac:
                    continue
                mgmt = [m.get("address", "") for m in n.get("managementAddresses", [])
                        if m.get("address")]
                out.setdefault(port, []).append({
                    "mac": mac,
                    "sysname": n.get("systemName", ""),
                    "port_description": n.get("neighborInterfaceInfo", {})
                                          .get("interfaceDescription", ""),
                    "mgmt_addrs": mgmt,
                })
        return out
