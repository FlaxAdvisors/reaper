"""On-host mstflint driver: bash the nicd agent pipes over SSH (all tools —
mstflint/mstconfig/mstfwreset/ipmitool — are on the live ISO). QUERY reads each
Mellanox card's FW/PSID/Security + UEFI bit; FLASH burns with
--allow_psid_change (re-brands OEM cards) + mstfwreset (card reset, no host
reboot). run_over_ssh is reused from the biosd driver (sudo -n bash -s)."""
import re

from flax_post.biosd.driver import run_over_ssh  # noqa: F401  (re-export)

# Per card: an mstflint query block then a UEFI (mstconfig) block, both
# delimited so parse_cards can split the concatenated output of all cards.
QUERY_SCRIPT = (
    "for pci in $(lspci -d 15b3: | grep '00.0' | cut -d' ' -f1); do\n"
    '  echo "===CARD $pci==="\n'
    "  mstflint -d $pci query\n"
    '  echo "===UEFI $pci==="\n'
    "  mstconfig -d $pci query | grep EXP_ROM_UEFI_x86_ENABLE || true\n"
    "done\n"
)

_FWVER = re.compile(r"^FW Version:\s*(\S+)", re.MULTILINE)
_PSID = re.compile(r"^PSID:\s*(\S+)", re.MULTILINE)
_SEC = re.compile(r"^Security Attributes:\s*(.+)$", re.MULTILINE)


def card_query_script(pci: str) -> str:
    """Single-card re-query (used to poll a card after a reset)."""
    return (
        f'echo "===CARD {pci}==="\n'
        f"mstflint -d {pci} query\n"
        f'echo "===UEFI {pci}==="\n'
        f"mstconfig -d {pci} query | grep EXP_ROM_UEFI_x86_ENABLE || true\n"
    )


def parse_cards(query_out: str) -> list[dict]:
    """Split the delimited multi-card output into per-card dicts."""
    out = []
    # Split into ===CARD <pci>=== ... (up to the next ===CARD or EOF)
    for m in re.finditer(r"===CARD (\S+)===\n(.*?)(?=\n===CARD |\Z)", query_out or "", re.DOTALL):
        pci, block = m.group(1), m.group(2)
        parts = block.split("===UEFI", 1)
        qblock, ublock = parts[0], (parts[1] if len(parts) > 1 else "")
        fw = _FWVER.search(qblock)
        psid = _PSID.search(qblock)
        sec = _SEC.search(qblock)
        if not fw or not psid:
            continue
        out.append({
            "pci": pci,
            "current": fw.group(1),
            "psid": psid.group(1),
            "secure": bool(sec and "secure-fw" in sec.group(1)),
            # tri-state: True=EXP_ROM_UEFI_x86_ENABLE on, False=present but off,
            # None=token absent (some cards/FW don't expose it -> not-applicable).
            "uefi": ("(1)" in ublock) if "EXP_ROM_UEFI_x86_ENABLE" in ublock else None,
        })
    return out


def flash_script(entry: dict, pci: str, share_base: str) -> str:
    # bin URL is built from share_base + the manifest's REQUIRED dir/bin
    # fields -> strict indexing (a bad manifest entry raises KeyError,
    # surfaced by the caller as a fault).
    url = f"{share_base}/mellanox/{entry['dir']}/{entry['bin']}.zip"
    return (
        "set -e\n"
        "T=$(mktemp -d)\n"
        f"curl -fsS {url} -o $T/fw.zip\n"
        "unzip -o $T/fw.zip -d $T/fw\n"
        f"BIN=$(ls $T/fw/{entry['bin']}* 2>/dev/null | head -1); [ -n \"$BIN\" ] || BIN=$T/fw/{entry['bin']}\n"
        f"mstflint -d {pci} -y -i $BIN --allow_psid_change burn\n"
        # DETACH the reset: mstfwreset drops the card's link -- the very NIC this
        # SSH rides -- so running it inline severs its own session and the SSH
        # call hangs on the dead connection. nohup+& lets the burn SSH return
        # immediately; wait_card_reset then polls for the true post-reset state.
        f"nohup mstfwreset -d {pci} -y reset </dev/null >/tmp/mstfwreset.log 2>&1 &\n"
        "sleep 2\n"
    )


def uefi_script(pci: str) -> str:
    return (
        f"mstconfig -d {pci} -y set EXP_ROM_UEFI_x86_ENABLE=true\n"
        # detached like flash_script's reset (same self-severing-SSH reason):
        f"nohup mstfwreset -d {pci} -y reset </dev/null >/tmp/mstfwreset.log 2>&1 &\n"
        "sleep 2\n"
    )


# BMC (OpenBMC) uptime read + reboot, over a separate SSH session (bmc creds).
# The BMC is rebooted over REDFISH (flax_post.fwd.redfish.RedfishClient
# .manager_reset), NOT SSH/IPMI -- OpenBMC's `ipmitool mc reset cold` hangs it.
