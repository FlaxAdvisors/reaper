"""UEFI boot order: read `efibootmgr [-v]`, pick the installed system's ubuntu entry.

AMI FixedBootOrder BIOSes (Wiwynn TiogaPass TPO_P26F) rebuild BootOrder every POST; on the
first POST after an install the new ubuntu entry can land behind every network entry, so the
node PXEs instead of booting its disk. An OS write that puts a KNOWN entry first does stick.
Spec: reaper-devel docs/superpowers/specs/2026-11-11-deploy-firstboot-boot-order-design.md."""
import re

_CURRENT = re.compile(r"^BootCurrent:\s*([0-9A-Fa-f]{4})\s*$", re.M)
_ORDER = re.compile(r"^BootOrder:\s*([0-9A-Fa-f,]*)\s*$", re.M)
_ENTRY = re.compile(r"^Boot([0-9A-Fa-f]{4})\*?\s+(.*)$", re.M)
_HD = re.compile(r"HD\(\d+,GPT,([0-9A-Fa-f-]{36}),", re.I)
_SHIM = re.compile(r"\\EFI\\ubuntu\\shimx64\.efi", re.I)


def parse(text: str) -> dict:
    """{"current": id | None, "order": [id, ...], "entries": {id: rest of its line}};
    ids upper-case 4-hex."""
    cur = _CURRENT.search(text or "")
    order = _ORDER.search(text or "")
    return {"current": cur.group(1).upper() if cur else None,
            "order": [x.upper() for x in order.group(1).split(",") if x] if order else [],
            "entries": {m.group(1).upper(): m.group(2) for m in _ENTRY.finditer(text or "")}}


def ubuntu_entry(entries: dict, partuuids: set) -> "str | None":
    """The lowest id booting \\EFI\\ubuntu\\shimx64.efi from a GPT partition that is on
    this host; an entry left for a wiped disk never matches."""
    have = {p.lower() for p in partuuids}
    hits = [i for i, line in entries.items()
            if _SHIM.search(line) and (m := _HD.search(line)) and m.group(1).lower() in have]
    return min(hits, key=lambda i: int(i, 16)) if hits else None


def ubuntu_first(order: list, entry: str) -> list:
    return [entry] + [x for x in order if x != entry]


def order_line(text: str) -> str:
    """'BootCurrent: X · BootOrder: a,b' (whichever lines exist), else 'not read'."""
    p = parse(text)
    parts = ([f"BootCurrent: {p['current']}"] if p["current"] else []) + \
            ([f"BootOrder: {','.join(p['order'])}"] if _ORDER.search(text or "") else [])
    return " · ".join(parts) or "not read"
