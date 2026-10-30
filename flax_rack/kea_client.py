"""Client for the flax-control order-reservation endpoints (Task 6).

Kea is written ONLY through these endpoints; never raw SQL from flax_rack.
"""
import json
import urllib.error
import urllib.parse
import urllib.request


class KeaError(Exception):
    pass


class KeaConflict(KeaError):
    pass


class KeaClient:
    def __init__(self, base_url: str = "http://127.0.0.1:8000", *,
                 opener=urllib.request.urlopen):
        self.base = base_url.rstrip("/")
        self._open = opener

    def _call(self, method: str, query=None, body: dict | None = None,
              path: str = "/api/v1/order-reservations") -> dict:
        url = self.base + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with self._open(req, timeout=15) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                msg = json.loads(e.read()).get("error", "")
            except (ValueError, AttributeError):
                msg = ""
            msg = msg or f"HTTP {e.code}"
            raise (KeaConflict if e.code == 409 else KeaError)(msg) from e
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise KeaError(f"flax-control unreachable: {e}") from e

    def upsert(self, source, mac, ipv4, hostname, vid) -> None:
        self._call("POST", body={"source": source, "mac": mac, "ipv4": ipv4,
                                 "hostname": hostname, "vid": vid})

    def delete_source(self, source) -> int:
        return int(self._call("DELETE", {"source": source}).get("deleted", 0))

    def list(self, source) -> list[dict]:
        return self._call("GET", {"source": source}).get("rows", [])

    def leases(self, macs) -> dict[str, dict]:
        """Read-only: {mac12hex: {ip, expire, state, subnet_id, hostname}} for
        the MACs holding a Kea lease (GET /api/v1/leases; <= 64 per call)."""
        macs = list(macs)
        if not macs:
            return {}
        return self._call("GET", [("mac", m) for m in macs],
                          path="/api/v1/leases").get("leases", {})
