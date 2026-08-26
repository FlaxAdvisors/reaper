#!/usr/bin/env python3
"""Entry point for the post qualification agent: identify the node, run the battery
in a supervisor thread (re-running after a /restart ack), and serve the contract.
Launched by post.sh on postaction=postautomate. Stdlib only.
"""
import re
import threading
import time

import qual_battery
import qual_server
import qual_stages


def node_identity(runner, cmdline=None):
    if cmdline is None:
        cmdline = open("/proc/cmdline").read()
    m = re.search(r"BOOTIF=01-([0-9a-fA-F-]+)", cmdline)
    mac = m.group(1).replace("-", "").lower() if m else ""
    serial = runner(["dmidecode", "-s", "system-serial-number"], 15)[1].strip()
    return mac, serial


def _supervise_once(battery):
    battery.run()


def _supervisor(battery):
    while True:
        if battery.state == "running":
            battery.run()
        time.sleep(2)


def main(serve_fn=qual_server.serve, battery=None):
    if battery is None:
        mac, serial = node_identity(qual_stages.RUNNER)
        battery = qual_battery.Battery(mac=mac, serial=serial)
    threading.Thread(target=_supervisor, args=(battery,), daemon=True).start()
    httpd = serve_fn(battery)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
