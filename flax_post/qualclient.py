"""Typed client for the on-node qualification agent's REST contract.

Read-only but for the one /restart control route (design §5). Transport is an
injectable seam (default urllib, no `requests` dep — mirrors fwd/artifact.py) so
the producer and CLI test against a fake agent with no network. Any failure or
non-2xx becomes QualUnreachable; the caller turns that into an attention/unreach
state rather than crashing the producer loop.
"""
import json
import urllib.error
import urllib.request

_TIMEOUT = 5


class QualUnreachable(Exception):
    """The node agent did not answer, or answered non-2xx."""


def _default_transport(method, url, timeout):
    """(method, url, timeout) -> (status_code, body_text). Network failure -> (0, "")."""
    req = urllib.request.Request(url, method=method, data=(b"" if method == "POST" else None))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except (urllib.error.URLError, OSError, ValueError):
        return 0, ""


class QualClient:
    def __init__(self, base_url, *, transport=None, timeout=_TIMEOUT):
        self._base = base_url.rstrip("/")
        self._t = transport or _default_transport
        self._timeout = timeout

    def _get_text(self, path):
        code, body = self._t("GET", self._base + path, self._timeout)
        if not (200 <= code < 300):
            raise QualUnreachable(f"GET {path} -> {code}")
        return body

    def _get_json(self, path):
        try:
            return json.loads(self._get_text(path) or "null")
        except json.JSONDecodeError as e:
            raise QualUnreachable(f"GET {path} -> bad JSON: {e}") from e

    def _post_json(self, path):
        code, body = self._t("POST", self._base + path, self._timeout)
        if not (200 <= code < 300):
            raise QualUnreachable(f"POST {path} -> {code}")
        try:
            return json.loads(body or "null")
        except json.JSONDecodeError as e:
            raise QualUnreachable(f"POST {path} -> bad JSON: {e}") from e

    def health(self):
        return self._get_json("/health")

    def status(self):
        return self._get_json("/status")

    def stages(self):
        return self._get_json("/stages")

    def stage(self, name):
        return self._get_json(f"/stage/{name}")

    def stage_artifact(self, name, art):
        return self._get_text(f"/stage/{name}/{art}")

    def restart(self):
        return self._post_json("/restart")

    def restart_ack(self):
        return self._post_json("/restart/ack")
