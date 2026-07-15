# flax_classify/vlan_policy.py
"""VLAN-steering policy for flax-classify (spec §5b).

desired_vid is the guarded vid decision (= reaper _vid_for + the no-steer
exclusion). Loaders lift reaper-leased's load_vlans / phase_for / DEFAULT_VIDS.
The reservation IP then follows desired_vid via formula.alloc_ip(172.<vid>...).
"""
import json

DEFAULT_VIDS = {"triage": 16, "post": 24}  # reaper-leased line 1710


def load_fp_to_vid(path: str) -> dict:
    """vlans.json (list of {vid, family, phase, ...}) -> {(family, phase): vid}."""
    with open(path) as f:
        return {(v["family"], v["phase"]): v["vid"] for v in json.load(f)
                if v.get("family") and v.get("phase")}


def fp_to_vid_from_roles(role_defs: dict):
    """Role registry (spine migration) -> {(family, role): vid}, or None.

    Each role_def may carry policy.vid.by_family (a {family: vid} map for
    that role -- "role" here is the same string as desired_vid's "phase",
    e.g. "triage"/"post"). This builder unions every role's by_family into
    one (family, role) -> vid dict, exactly the shape load_fp_to_vid produces
    from vlans.json (see the parity test).

    Returns None when NO role_def carries a (non-empty) by_family block --
    signals "this registry predates the vid policy" so the __main__ startup
    caller falls back to load_fp_to_vid(args.vlans) (deploy-order safety,
    same pattern as role_registry's own missing/invalid-dir fallback).

    policy.vid.default (per role) is the DEFAULT_VIDS-equivalent that
    desired_vid's `fp_to_vid.get((family, phase), DEFAULT_VIDS[phase])`
    fallback already consumes for a KNOWN family with no by_family entry.
    The deployed roles.d templates pin "default" to 16 (triage) / 24 (post)
    -- identical to this module's DEFAULT_VIDS constant -- so that fallback
    path is already registry-consistent without this builder needing to
    inject synthetic entries into the returned (family, role) dict (there is
    no family-less key that would fit its shape). If a site's registry ever
    sets a "default" that diverges from DEFAULT_VIDS, the module constant
    still wins for that gap -- a known, deliberate limitation (see
    .superpowers/sdd/task-4-report.md for the exact reasoning).
    """
    out = {}
    found = False
    for role, defn in (role_defs or {}).items():
        vid_policy = ((defn or {}).get("policy") or {}).get("vid") or {}
        by_family = vid_policy.get("by_family")
        if not by_family:
            continue
        found = True
        for family, vid in by_family.items():
            out[(family, role)] = vid
    return out if found else None


def _norm_rabbit_token(port: str) -> str:
    """Ethernet6/1 or et6b1 -> et6/1.

    Mirror reaper.normalise_rabbit_port output form: f"et{p}/{s}" where p and s
    are the integer port and slot. Both the slash form (Ethernet6/1, Et6/3) and
    the 'b' form (et6b1) are normalised to the canonical et<P>/<S> string.
    """
    p = port.strip().lower().replace("ethernet", "et")
    if p.startswith("et"):
        rest = p[2:]
    else:
        # Non-rabbit port (e.g. swp6) — return as-is; phase_for won't match it
        return p
    if "/" in rest:
        a, b = rest.split("/", 1)
    elif "b" in rest:
        a, b = rest.split("b", 1)
    else:
        return p
    try:
        return "et" + str(int(a)) + "/" + str(int(b))
    except ValueError:
        return p


def load_phase_geometry(path: str) -> dict:
    """geometry.json -> {switch: set of 'et<P>/<S>' triage-port tokens}.

    SWITCH-SCOPED: a token is a triage port ONLY on the switch geometry.json
    records it under (every entry carries "switch"). geometry.json describes
    triage's rabbit switch (e.g. rabbit-gouda) and has zero post info, so a
    token here must not claim the same port NUMBER on a post switch
    (rabbit-edam) -- that would hijack a post blade onto the triage lane. The
    previous flat-set form dropped the switch and did exactly that.
    """
    with open(path) as f:
        data = json.load(f)
    out: dict[str, set] = {}
    for e in data:
        # geometry entries use et<P>b<S> form; normalise to et<P>/<S>.
        # Mirror reaper.normalise_rabbit_port exactly.
        sw = e.get("switch")
        if not sw:
            continue  # a switch-less entry can't be scoped; skip (real
                      # geometry.json always records the switch).
        out.setdefault(sw, set()).add(_norm_rabbit_token(e["port"]))
    return out


def phase_for(geom_by_switch: dict, switch: str, port: str) -> str:
    if switch.lower().startswith("turtle"):
        return "post"
    toks = geom_by_switch.get(switch, set())
    return "triage" if _norm_rabbit_token(port) in toks else "post"


def load_no_steer(path: str) -> set:
    """Site no-steer port list -> {(switch, port)}; missing file => empty.
    Same format flax_reconcile.steer.load_no_steer reads (shared site file)."""
    try:
        with open(path) as f:
            data = json.load(f)
    except (FileNotFoundError, IsADirectoryError, OSError,
            json.JSONDecodeError, ValueError):
        # Absent file, a docker-created directory (bind-mount of a missing host
        # path), or malformed JSON -> no exclusions. observe now loads this at
        # startup, so a directory must NOT crash it (mirrors load_rabbit_geometry).
        return set()
    if not isinstance(data, list):
        return set()
    return {(e["switch"], e["port"]) for e in data
            if isinstance(e, dict) and "switch" in e and "port" in e}


def load_bmc_only_families(path: str = "/etc/flax/bmc-only-families.json") -> set:
    """Site list of "bmc-only" device families -> {family, ...} (lowercased).

    A bmc-only family is RJ45-LOM: one physical port carries ONE MAC that serves
    BOTH the BMC and the host NIC (e.g. capri). flax-observe synthesizes a
    phantom nic_mac for such single-MAC ports; the feeder consults this set to
    suppress the phantom host reservation (see derive_targets).

    Tolerant safe-load: returns an empty set when the file is absent, is a
    directory (a docker bind-mount of a missing path creates an empty dir),
    empty, or malformed. Soft-fails to empty rather than dying because the
    consequence of an empty set is merely the pre-existing phantom-host
    behaviour, not a safety violation."""
    try:
        with open(path) as f:
            data = json.load(f)
    except (FileNotFoundError, IsADirectoryError, json.JSONDecodeError,
            OSError, ValueError):
        return set()
    if not isinstance(data, list):
        return set()
    return {str(fam).strip().lower() for fam in data if fam}


def desired_vid(family, phase, *, access_vid, in_no_steer, fp_to_vid):
    """Guarded steer decision. Returns:
       None       -> skip this port entirely (excluded uplink/no-steer)
       access_vid -> hold at current vlan (unknown family)
       <vid>      -> steer to the family's (phase) vlan (known family)
    The mask!=access (trunk) case is handled upstream: feeder skips ports with
    no access_vid. This function additionally enforces the no-steer list."""
    if in_no_steer:
        return None
    if family in (None, "unknown"):
        return access_vid
    return fp_to_vid.get((family, phase), DEFAULT_VIDS[phase])
