#!/usr/bin/env python3
"""Emit nic_fw_lock.json from a node's collected mstflint query files.

collect_mellanox.sh writes one mstflint-d_<dev>_query.txt per Mellanox card
(full `mstflint query` output, including 'PSID:' and 'Security Attributes:').
A card is locked iff its Security Attributes contain 'secure-fw' -- mstflint
refuses to burn it; circumventing the lock needs a physical card change.
"""
import glob
import json
import os
import re
import sys

LOCK_TOKEN = "secure-fw"


def _field(text, label):
    for line in text.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            if k.strip() == label:
                return v.strip()
    return ""


def scan(logdir):
    cards = []
    pattern = os.path.join(logdir, "mstflint-d_*_query.txt")
    for path in sorted(glob.glob(pattern)):
        text = open(path, errors="replace").read()
        m = re.search(r"mstflint-d_(.+?)_query\.txt$", os.path.basename(path))
        pci = m.group(1) if m else ""
        security = _field(text, "Security Attributes")
        cards.append({
            "pci": pci,
            "psid": _field(text, "PSID"),
            "security": security,
            "locked": LOCK_TOKEN in security,
        })
    return {"locked": any(c["locked"] for c in cards), "cards": cards}


if __name__ == "__main__":
    logdir = sys.argv[1] if len(sys.argv) > 1 else "."
    print(json.dumps(scan(logdir)))
