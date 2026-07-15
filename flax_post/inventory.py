# flax_post/inventory.py
"""Per-node hardware inventory (INV modal) + population verdict (POP modal).

Both read the per-node recon dump at /export/nodes/post-<host_mac>/latest/*.txt
(populated by the PXE recon boot, NOT by flax) via the ghost `macinv` CLI.
`capture()` shells macinv in two forms: the detail (-v) form -> `parse()` ->
the INV section tables, and the count form -> `verdict()` -> the POP verdict.
`verdict()` runs the count-form text against a node_config profile via
flax_post.population -- see verdict() for why the count form (not verbose).

Field extraction in parse() is grounded in REAL macinv output captured from
bang-gouda (tests/fixtures/macinv/*.txt) -- not a guessed format. Sections/
fields that never appeared in any captured fixture are still implemented
(informed by reading /opt/flax/bin/macinv's dumpnode functions directly) but
are flagged as script-derived-only in the task report; every section defaults
to [] / {} rather than raising, so an absent section never breaks the modal.
"""
import os
import re
import subprocess

from . import population

NODES_ROOT = os.environ.get("FLAX_POST_NODES_ROOT", "/export/nodes")


def node_dir(host_mac: "str | None") -> "str | None":
    """/export/nodes/post-<mac, no colons, lowercase>/latest if it's a real
    (possibly symlinked) directory, else None. Falsy host_mac -> None."""
    if not host_mac:
        return None
    mac = host_mac.replace(":", "").lower()
    path = f"{NODES_ROOT}/post-{mac}/latest"
    return path if os.path.isdir(path) else None


def _default_runner(argv: list, timeout: int) -> str:
    """Subprocess seam: run argv, return stdout text (stderr appended only if
    stdout is empty). Timeout/missing binary -> "" (never raises)."""
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return r.stdout if r.stdout else (r.stderr or "")
    except subprocess.TimeoutExpired:
        return ""
    except FileNotFoundError:
        return ""


RUNNER = _default_runner


def capture(host_mac: "str | None", *, runner=None) -> dict:
    """Resolve the node dir and shell macinv in both forms: the detail (-v)
    form feeds parse()/INV, the count form feeds verdict()/POP.
    {"present": False} if the node dir doesn't exist."""
    d = node_dir(host_mac)
    if d is None:
        return {"present": False}
    run = runner or RUNNER
    verbose = run(["macinv", "-p", d, "-v"], 30)
    count = run(["macinv", "-p", d], 30)
    return {"present": True, "dir": d, "verbose": verbose, "count": count}


# --- parse(): macinv -v detail-form line patterns, one per macinv dumpnode
# section (see /opt/flax/bin/macinv on bang-gouda). Real examples in
# tests/fixtures/macinv/*.verbose.txt.

_BMC_MAC_RE = re.compile(r"^BMC MAC\d+:\s*(?P<mac>\S+)")
_BMC_VERSION_RE = re.compile(r"^BMC Version:\s*(?P<version>.*)$")
_BIOS_RE = re.compile(r"^BIOS Version:\s*(?P<version>[^,]+)")
_BOARD_RE = re.compile(
    r"^Board Mfg:\s*(?P<mfg>[^,]+),\s*Product:\s*(?P<product>[^,]+),.*?"
    r"Product Serial:\s*(?P<serial>\S+)"
)
_CPU_RE = re.compile(
    r"^Processor Version:\s*(?P<model>[^,]+?)\s*(?:,\s*Serial:\s*(?P<serial>.*))?$"
)
_MEM_RE = re.compile(
    r"^Memory:\s*(?P<size>[^,]+),\s*(?P<slot>DIMM\s*\S+),\s*[^,]+,\s*"
    r"(?P<speed>[^,]+),\s*(?P<mfg>[^,]+),\s*(?P<serial>[^,]+),\s*(?P<part>.+)$"
)
_NIC_RE = re.compile(
    r'^BusID:\s*\S+,\s*Eth MAC:\s*(?P<mac>\S+),\s*Model:\s*"(?P<model>[^"]+)"'
)
_BLOCK_CTLR_RE = re.compile(
    r'^Handle_:\s*\d+,\s*BusID:\s*\S+,\s*Model:\s*"(?P<model>[^"]+)"'
)
# blockdump lines: "Handler: N, BusID: ..., Model: "...", [SubVendor: ...,
# SubDevice: ...,] [Revision: "...",] [Serial ID: "...",] Capacity: <size>" --
# the optional fields (SubVendor/SubDevice/Revision/Serial ID) vary by device
# type (NVMe carries SubVendor+SubDevice+Serial ID, SAS carries Revision+
# Serial ID, USB carries neither), so pull model/serial/capacity independently
# rather than assuming one fixed field order.
_STORAGE_LINE_RE = re.compile(r'^Handler:\s*\d+,\s*BusID:\s*\S+,')
_STORAGE_MODEL_RE = re.compile(r'Model:\s*"(?P<model>[^"]+)"')
_STORAGE_SERIAL_RE = re.compile(r'Serial ID:\s*"(?P<serial>[^"]*)"')
_STORAGE_CAPACITY_RE = re.compile(r'Capacity:\s*(?P<size>.+)$')
# gpudump (macinv source) is BusID+Model+Device -- no Handle_/Handler prefix
# and no Eth MAC/Interface/[SN]/firmware-version fields the way ethdump has.
# Fixture-confirmed: the BMC's own ASPEED AST1000/2000 onboard VGA controller
# hits this pattern on node-a/node-b (macinv classifies it via hwinfo's "VGA
# ... controller" PCI class) -- not a discrete add-in GPU, but it is real
# macinv gpudump output.
_GPU_RE = re.compile(r'^BusID:\s*\S+,\s*Model:\s*"(?P<model>[^"]+)",\s*Device:')
_LLDP_RE = re.compile(
    r"^Interface:\s*(?P<iface>\S+),\s*ChassisID:\s*(?P<neighbor>[^,]+)"
    r"(?:,\s*PortDescr:\s*(?P<port>.+))?$"
)


def parse(verbose_text: str) -> dict:
    """macinv -v detail-form text -> structured sections. Every list section
    defaults to []; every single-value section defaults to {} (or the str-typed
    ones simply aren't set); never raises on a missing/malformed section."""
    cpu, memory, nic, storage = [], [], [], []
    lldp, gpu, block_ctlr = [], [], []
    bmc, bios, fru = {}, {}, {}

    for line in (verbose_text or "").splitlines():
        line = line.strip()
        if not line:
            continue

        m = _BMC_MAC_RE.match(line)
        if m:
            bmc["mac"] = m.group("mac")
            continue
        m = _BMC_VERSION_RE.match(line)
        if m:
            bmc["version"] = m.group("version").strip()
            continue
        m = _BIOS_RE.match(line)
        if m:
            bios["version"] = m.group("version").strip()
            continue
        m = _BOARD_RE.match(line)
        if m and not fru:  # first Board Mfg line carrying Product Serial = mainboard
            fru = {"mfg": m.group("mfg").strip(),
                   "board_product": m.group("product").strip(),
                   "product_serial": m.group("serial").strip()}
            continue
        m = _CPU_RE.match(line)
        if m:
            cpu.append({"serial": (m.group("serial") or "").strip(),
                        "model": m.group("model").strip()})
            continue
        m = _MEM_RE.match(line)
        if m:
            memory.append({"slot": m.group("slot").strip(),
                           "serial": m.group("serial").strip(),
                           "size": m.group("size").strip(),
                           "part": m.group("part").strip(),
                           "mfg": m.group("mfg").strip(),
                           "speed": m.group("speed").strip()})
            continue
        m = _NIC_RE.match(line)
        if m:
            nic.append({"mac": m.group("mac"), "model": m.group("model").strip()})
            continue
        if _STORAGE_LINE_RE.match(line):
            model_m = _STORAGE_MODEL_RE.search(line)
            serial_m = _STORAGE_SERIAL_RE.search(line)
            size_m = _STORAGE_CAPACITY_RE.search(line)
            if model_m:
                storage.append({"serial": (serial_m.group("serial") if serial_m else "").strip(),
                                "size": (size_m.group("size") if size_m else "").strip(),
                                "model": model_m.group("model").strip()})
            continue
        m = _BLOCK_CTLR_RE.match(line)
        if m:
            block_ctlr.append({"model": m.group("model").strip()})
            continue
        m = _GPU_RE.match(line)
        if m:
            gpu.append({"model": m.group("model").strip()})
            continue
        m = _LLDP_RE.match(line)
        if m:
            lldp.append({"iface": m.group("iface"),
                        "neighbor": m.group("neighbor").strip(),
                        "port": (m.group("port") or "").strip()})
            continue

    return {"cpu": cpu, "memory": memory, "nic": nic, "storage": storage,
            "bmc": bmc, "bios": bios, "fru": fru,
            "lldp": lldp, "gpu": gpu, "block_ctlr": block_ctlr}


def verdict(count_text: str, profile_name: "str | None") -> dict:
    """POP verdict: run the operator's active node_config profile against the
    macinv COUNT-form dump (population.evaluate reused, not reimplemented).
    No profile selected, OR a named profile that fails to load (mistyped /
    deleted -> population.load_profile returns []) -> grey/no-op. Grey, not a
    vacuous green: evaluate([], ...) would otherwise report ok=True (all() of
    an empty result list), which would show a broken profile reference as a
    passing population check.

    Why the count form, not verbose: profile rules are full-line presence
    regexes authored to match macinv's `uniq -c`-collapsed count lines, e.g.
    `8 Memory Size: 32 GB` matches the count line `8 Memory Size: 32 GB, ...`
    where the leading integer IS part of the regex (it matches the count
    prefix). A 12x64GB node's `12 Memory Size: 64 GB` line correctly fails that
    rule. See flax_post.population for the full grammar.
    """
    rules = population.load_profile(profile_name) if profile_name else []
    if not profile_name or not rules:
        return {"state": "grey", "profile": profile_name, "results": [], "missing": []}
    r = population.evaluate(rules, count_text)
    return {"state": "green" if r["ok"] else "red", "profile": profile_name,
            "results": r["results"],
            "missing": [x["rule"] for x in r["results"] if not x["ok"]]}
