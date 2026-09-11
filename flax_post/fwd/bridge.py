"""Map a fwd store row -> the rendered firmware slice the main view renders.

Pure + DB-free so it unit-tests without Postgres. The production mirror
(flax_post/fwd/__main__._Deps) injects a real `set_state`; tests inject a fake.
The slice is written as post_state.vars.fw_bmc (a top-level key so the shallow
JSONB merge never clobbers a sibling fw_bios slice added in Plan C).
"""
from . import manifest

# No ordering exists (triage-parity: version.compare is same|differs), so a
# mismatch is one class. "ver-hi" (was: current newer than target) is gone.
_VER_CLASS = {"same": "ver-ok", "differs": "ver-warn"}


def _ver_class(current, target):
    if not current or current == "—" or not target:
        return "ver-na"
    return _VER_CLASS.get(manifest.compare(current, target), "ver-na")


def fw_bmc_slice(row):
    current = row.get("current_version")
    target = row.get("target_version")
    return {
        "current": current,
        "target": target,
        "ver_class": _ver_class(current, target),
        "phase": row.get("phase"),
        "mode": row.get("mode"),
        "percent": row.get("percent"),
        "fault_reason": row.get("fault_reason") or "",
    }


def mirror_row(set_state, port, row):
    set_state(port, fw_bmc=fw_bmc_slice(row))
