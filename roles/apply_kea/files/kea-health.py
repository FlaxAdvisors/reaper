#!/usr/bin/env python3
"""Container healthcheck for kea-dhcp4/kea-dhcp6 (runs inside the kea image).

The image's own HEALTHCHECK runs `supervisorctl status`, but our units start
kea directly (--entrypoint), so there is no supervisord and the check always
failed. This asks the daemon itself over its control socket instead.

Healthy = status-get answers result 0, the DHCP sockets are "ready", and DHCP
is not disabled (globally or by a lost Postgres connection).

Usage: kea-health.py <control-socket-path>   exit 0 healthy, 1 otherwise
"""
import json
import socket
import sys


def status_get(path):
    s = socket.socket(socket.AF_UNIX)
    s.settimeout(5)
    s.connect(path)
    s.sendall(json.dumps({"command": "status-get"}).encode())
    buf = b""
    while True:
        chunk = s.recv(65536)
        if not chunk:
            break
        buf += chunk
    return json.loads(buf)


def problems(reply):
    if reply.get("result") != 0:
        return [f"result={reply.get('result')} {reply.get('text', '')}"]
    args = reply.get("arguments", {})
    out = []
    sockets = args.get("sockets", {}).get("status")
    if sockets != "ready":
        out.append(f"sockets={sockets}")
    state = args.get("dhcp-state", {})
    if state.get("globally-disabled"):
        out.append("dhcp globally disabled")
    if state.get("disabled-by-db-connection"):
        out.append("dhcp disabled by lost db connection")
    return out


def main():
    try:
        found = problems(status_get(sys.argv[1]))
    except Exception as e:  # no socket, timeout, bad JSON: all unhealthy
        found = [f"{type(e).__name__}: {e}"]
    print("ok" if not found else "; ".join(found))
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
