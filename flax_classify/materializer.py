"""Shadow materializer: the diff engine at the heart of phase 2.

SHADOW ONLY -- this module never writes kea.hosts / kea.ipv6_reservations.
Its only write is appending rows to `materializer_plan` (an append-only
plan-history log, one row per planned action per cycle).

`plan_materialization` is the pure diff: given the desired_reservations
snapshot, a kea.hosts actuals snapshot, and the set of currently-registered
owner roles, it decides what WOULD need to change to converge actuals onto
desired -- without touching anything. `run_shadow` is the thin apply layer:
read both snapshots for real, plan, write the plan rows, sweep plan rows
older than 7 days.

Owner attribution of an actual row (kea.hosts, keyed by mac):
  - `source` (user_context.source) if `source in owners` -- the row is
    tagged with a currently-registered role and trusted as-is.
  - else `"triage"` if `source is None and has_classify` -- the LEGACY
    transition rule: pre-dual-write triage rows carry a `classify` sub-key
    in user_context but no `source` tag (Task 3 only started tagging new/
    re-upserted rows with source="triage"; untagged rows already in the
    table are legacy triage writes, not "unowned"). This only counts as
    "triage" if "triage" is itself a currently-registered owner -- an
    empty/absent registry must attribute nothing (registry stand-down).
  - else: NOT OURS (`legacy-import` seed rows, any other/unknown source,
    or a classify-less untagged row). An actual row that is NOT OURS is
    a safety boundary: it is NEVER planned against, in ANY action. If a
    desired row happens to share that mac, the planner deliberately plans
    NOTHING for that mac (not even the upsert that would otherwise apply)
    and instead counts it in `skipped_unowned` -- safety beats completeness
    in shadow mode; we do not want the future live materializer's design to
    have ever been validated against a plan that assumes ownership of a row
    that might belong to something outside this system's registry.

Actions (owner_role, action, mac, detail):
  - "upsert": desired mac missing from actuals (detail={"reason":"missing",
    "desired": <desired row dict>}), or an owned actual for the same mac
    differs on any of (hostname, ipv4, vid, ipv6) (detail={"fields": [...],
    "desired": <desired row dict>}).
  - "delete": an owner's actual mac has no desired row at all (desired_reservations
    is keyed uniquely by mac, so "no desired row of that owner" and "no desired
    row of any owner" are the same condition).
  - "purge_handoff": the actual row's owner != the desired row's owner for the
    same mac (detail={"from": <actual owner>, "to": <desired owner>,
    "desired": <desired row dict>}).
  - no action at all: desired and the owned actual agree on every compared
    field (converged).

  The "desired" key (phase 3a, Task 4) carries EXACTLY the seven apply
  kwargs from the desired_reservations row -- switch, port, kind, vid,
  hostname, ipv4, ipv6 (see _APPLY_FIELDS) -- for upsert/purge_handoff
  actions (mac is already the action's own mac field). Deliberately NOT the
  raw read_desired row: that row carries updated_at (a Python datetime) and
  attrs, and every action detail is serialized into materializer_plan via
  psycopg's Jsonb (stock json.dumps -- a datetime raises TypeError and
  would roll back the whole plan-row+summary write, breaking shadow mode's
  byte-identical guarantee). Purely additive otherwise: the apply layer
  (apply_actions, below) is the only consumer. "delete" actions carry no
  desired row (none exists by definition) so their detail is unchanged.
"""
import collections
import logging
import uuid

from psycopg.types.json import Jsonb

from .desired_reservations import read_desired
from .kea_hosts import (read_kea_actuals, upsert_kea_host,
                        delete_hosts_for_mac, delete_other_subnet_rows)

log = logging.getLogger(__name__)

_COMPARE_FIELDS = ("hostname", "ipv4", "vid", "ipv6")

# The subset of a desired_reservations row embedded into upsert/purge_handoff
# action details as detail["desired"]: exactly the non-mac upsert_kea_host
# kwargs, all JSON-native (str/int/None). NEVER the raw read_desired row --
# it carries updated_at (a datetime json.dumps can't serialize; would abort
# the Jsonb plan-row insert) plus attrs/generation the apply doesn't need.
_APPLY_FIELDS = ("switch", "port", "kind", "vid", "hostname", "ipv4", "ipv6")

_DEFAULT_MAX_DELETES_PER_RUN = 20


def parse_enforce_config(env: dict) -> tuple:
    """Pure two-key arming parse for the phase-3a enforce-mode env knobs.

    Reads MATERIALIZER_MODE (default "shadow"), MATERIALIZER_ENFORCE_ROLES
    (default ""), and MATERIALIZER_MAX_DELETES_PER_RUN (default 20) from the
    given env mapping (never os.environ directly -- this must stay testable
    with plain dicts). Returns (enforced_roles: frozenset[str], max_deletes:
    int).

    Two-key arming: enforced_roles is non-empty ONLY when MODE == "enforce"
    AND the parsed roles list is non-empty. MODE == "shadow" (the deployed
    default) is a global kill-switch -- it suppresses the roles list
    unconditionally, so flipping MATERIALIZER_ENFORCE_ROLES alone enforces
    nothing. Any OTHER mode value is a misconfiguration in 3a: log.error and
    force shadow (frozenset()), exactly like the old MATERIALIZER_MODE
    validation this function subsumes.

    Roles parse: comma-split, strip, lower, drop empty entries.

    max_deletes: int()'d from the raw string; a non-integer or negative
    value is a misconfiguration -- log.error and default to 20.
    """
    mode = env.get("MATERIALIZER_MODE", "shadow")
    roles_raw = env.get("MATERIALIZER_ENFORCE_ROLES", "")
    roles = frozenset(
        r for r in (part.strip().lower() for part in roles_raw.split(","))
        if r)

    if mode == "enforce":
        enforced_roles = roles
    elif mode == "shadow":
        enforced_roles = frozenset()
    else:
        log.error("MATERIALIZER_MODE=%s unsupported in phase 3a - forcing "
                  "shadow", mode)
        enforced_roles = frozenset()

    max_deletes_raw = env.get("MATERIALIZER_MAX_DELETES_PER_RUN",
                              str(_DEFAULT_MAX_DELETES_PER_RUN))
    try:
        max_deletes = int(max_deletes_raw)
        if max_deletes < 0:
            raise ValueError("negative max_deletes")
    except (TypeError, ValueError):
        log.error("MATERIALIZER_MAX_DELETES_PER_RUN=%r invalid - defaulting "
                  "to %d", max_deletes_raw, _DEFAULT_MAX_DELETES_PER_RUN)
        max_deletes = _DEFAULT_MAX_DELETES_PER_RUN

    return enforced_roles, max_deletes


def _norm(mac: str) -> str:
    return mac.strip().lower()


def _apply_fields(d: dict) -> dict:
    """Project a desired_reservations row onto the JSON-safe apply subset
    embedded in action details (see _APPLY_FIELDS)."""
    return {k: d.get(k) for k in _APPLY_FIELDS}


def _attribute_owner(actual: dict, owners: set) -> str | None:
    source = actual.get("source")
    if source in owners:
        return source
    if source is None and actual.get("has_classify") and "triage" in owners:
        return "triage"
    return None


def plan_materialization(desired: list, actuals: list, owners: set) -> tuple:
    """Pure diff. Returns (actions, skipped_dict).

    `actions` is a list of (owner_role, action, mac, detail) tuples, mac
    order is deterministic (sorted). `skipped` is a counter dict of the
    safety-skip invariants -- macs we counted but deliberately did NOT plan
    against:
      - "unowned": desired rows sharing a mac with a NOT-OURS actual
        (see module docstring).
      - "unregistered_desired": desired rows whose owner_role is NOT in
        `owners`. The whole mac is skipped, NOT just the desired row --
        dropping the row before the mac union would make "delete when no
        desired row of ANY owner" silently mean "any REGISTERED owner",
        so a handoff-in-progress mac (e.g. desired moved to post, actual
        still triage, post missing from the registry) would plan a triage
        delete: phase-3 data loss. Skipping the mac keeps the desired row
        visible as a delete-blocker even though we can't plan for it.
      - "multi_actual": macs with MORE THAN ONE distinct actual row.
        kea.hosts keys on (mac, type, subnet), so a mac CAN have rows in
        two subnets, and read_kea_actuals' ipv6_reservations LEFT JOIN can
        fan one row out into several. EXACT duplicates (identical on every
        attribution + compared field) are deduped to one row first -- the
        common v6-join fanout must not mask real drift -- but genuinely
        distinct rows for one mac are skipped: collapsing them
        last-write-wins would make the plan nondeterministic (converged vs
        flapping by row order). Post-3b this skip only catches genuinely
        ambiguous states -- rows split across different owners, or an
        operator_note-protected dup (purge-exempt by design) -- because the
        one same-owner way a dup is CREATED (a vid-moving upsert, whose new-
        subnet INSERT strands the old-subnet row) is cleaned up at apply
        time in the same cycle by delete_other_subnet_rows (see
        apply_actions). Those remaining ambiguous states stay skipped by
        policy: they need a human (or an owner handoff) to resolve, not a
        last-write-wins guess.
    """
    owners = set(owners)
    skipped = {"unowned": 0, "unregistered_desired": 0, "multi_actual": 0}
    if not owners:
        # Registry stand-down: no owner scoping exists, plan nothing (and
        # count nothing -- there is no attribution to skip against).
        return [], skipped

    desired_by_mac = {}
    unregistered_desired_macs = set()
    for d in desired:
        mac = _norm(d["mac"])
        if d.get("owner_role") in owners:
            desired_by_mac[mac] = d
        else:
            unregistered_desired_macs.add(mac)

    # Group actuals per mac, deduping EXACT duplicates (same attribution
    # inputs + same compared fields): the v6-join fanout shape.
    rows_by_mac = {}
    for a in actuals:
        mac = _norm(a["mac"])
        key = (a.get("source"), bool(a.get("has_classify")),
               tuple(a.get(f) for f in _COMPARE_FIELDS))
        rows_by_mac.setdefault(mac, {})[key] = a

    owned_actuals = {}
    unowned_macs = set()
    multi_actual_macs = set()
    for mac, uniq in rows_by_mac.items():
        if len(uniq) > 1:
            multi_actual_macs.add(mac)
            continue
        (a,) = uniq.values()
        owner = _attribute_owner(a, owners)
        if owner is not None:
            owned_actuals[mac] = (owner, a)
        else:
            unowned_macs.add(mac)

    actions = []
    all_macs = (set(desired_by_mac) | unregistered_desired_macs
                | set(owned_actuals) | multi_actual_macs)
    for mac in sorted(all_macs):
        if mac in multi_actual_macs:
            skipped["multi_actual"] += 1
            continue
        if mac in unregistered_desired_macs:
            skipped["unregistered_desired"] += 1
            continue
        d = desired_by_mac.get(mac)
        if mac in unowned_macs:
            if d is not None:
                skipped["unowned"] += 1
            continue
        a = owned_actuals.get(mac)
        if d is None:
            owner, _arow = a
            actions.append((owner, "delete", mac, {}))
        elif a is None:
            actions.append((d["owner_role"], "upsert", mac,
                           {"reason": "missing", "desired": _apply_fields(d)}))
        else:
            owner, arow = a
            if d["owner_role"] != owner:
                actions.append((d["owner_role"], "purge_handoff", mac,
                               {"from": owner, "to": d["owner_role"],
                                "desired": _apply_fields(d)}))
            else:
                diffs = [f for f in _COMPARE_FIELDS if d.get(f) != arow.get(f)]
                if diffs:
                    actions.append((owner, "upsert", mac,
                                   {"fields": diffs, "desired": _apply_fields(d)}))
    return actions, skipped


_INSERT_PLAN_SQL = """
    INSERT INTO materializer_plan (owner_role, action, mac, detail)
    VALUES (%(owner_role)s, %(action)s, %(mac)s, %(detail)s)
"""
_RETENTION_SQL = "DELETE FROM materializer_plan WHERE ts < now() - interval '7 days'"

# Run key = "<process-token>:<counter>". Every action planned within one
# run_shadow() call shares this value in detail.run, so a later reader can
# group "what did one cycle plan" without a separate id column. The token
# (random per process lifetime, fixed at import) guarantees uniqueness across
# restarts: a bare counter would collide even for ADJACENT plan blocks when
# the prior lifetime's last counter equals the new lifetime's first, which
# equality/contiguity-based grouping cannot disambiguate. The viewer's
# contiguous-prefix grouping remains defense-in-depth on top of this.
_RUN_TOKEN = uuid.uuid4().hex[:8]
_run_counter = [0]


def _next_run_id() -> str:
    """Mint the next run key: "<process-token>:<counter>". See _RUN_TOKEN."""
    _run_counter[0] += 1
    return f"{_RUN_TOKEN}:{_run_counter[0]}"


def _apply_upsert(pool, owner_role, mac, detail):
    d = detail["desired"]
    upsert_kea_host(
        pool, switch=d["switch"], port=d["port"], mac=mac,
        kind=d["kind"], vid=d["vid"], ipv4_address=d["ipv4"],
        hostname=d["hostname"], ipv6_address=d["ipv6"], source=owner_role)


def _needs_relocation_cleanup(actual, detail) -> bool:
    """True when the mac's actual row sits in a DIFFERENT subnet than the
    desired row being upserted -- the evidence that upsert_kea_host's
    (mac, type, dhcp4_subnet_id) conflict key will INSERT a fresh new-subnet
    row and strand the old-subnet one (see kea_hosts.delete_other_subnet_rows).
    Gated on this evidence so converged / missing-actual upserts never issue
    a useless delete query. `actual` is the mac's actuals_by_mac row (None
    when the mac has no actual at all); an upsert action with an actual is
    same-owner by planner construction (a different owner plans
    purge_handoff, not upsert)."""
    if not actual:
        return False
    return actual.get("vid") != detail["desired"]["vid"]


def apply_actions(pool, actions: list, *, enforced_roles: frozenset,
                  max_deletes: int, actuals_by_mac: dict) -> dict:
    """The enforce-mode apply layer: the ONLY place phase 3a writes
    kea.hosts on the materializer's behalf, and only for actions whose
    owner_role is in `enforced_roles` -- an action for a role NOT in
    enforced_roles is left untouched (plan-only), which is what makes
    `enforced_roles=frozenset()` (the deployed default) a true no-op: this
    function should not even be CALLED in that case (see run_cycle), but if
    it ever were, it would apply nothing.

    Dispatch per action, using the SAME writers the legacy lanes use --
    upsert_kea_host and the new delete_hosts_for_mac (kea_hosts.py):
      - "upsert": upsert_kea_host with the desired row's fields
        (detail["desired"]), source=owner_role. If the mac's actual row sits
        in a DIFFERENT subnet than the desired vid (same-owner vid move),
        the upsert INSERTs a new-subnet row -- kea's conflict key is
        (mac, type, dhcp4_subnet_id) -- so after the upsert succeeds the
        stranded old-subnet row is cleaned up via delete_other_subnet_rows
        (source=owner_role, keep_vid=desired vid); its rowcount is recorded
        as the action's deleted_rows. Gated on actuals_by_mac evidence: no
        other-vid actual, no cleanup call.
      - "delete": delete_hosts_for_mac(mac=mac, source=owner_role) -- UNLESS
        the actual row is operator_note-flagged (never delete an
        operator-curated row; mirrors the legacy sweeps' protection),
        in which case the delete is refused and counted in
        skipped_operator_note.
      - "purge_handoff": delete_hosts_for_mac(mac=mac, source=detail["from"])
        (the OLD owner's rows for this mac; skipped under the same
        operator_note guard, checked against the mac's actual row) THEN
        upsert_kea_host for the NEW owner (source=owner_role, i.e.
        detail["to"]) -- unconditionally, since an upsert is never a
        delete and preserves operator_note via the existing conflict-merge.

    Per-role circuit breaker, applied BEFORE any writing for that role: the
    planned deletions for the role (delete + purge_handoff actions both
    count -- a purge_handoff still deletes the old owner's row -- and so
    does every upsert whose actuals_by_mac evidence shows a same-owner
    different-vid actual, since its relocation cleanup is a real kea
    delete) are counted first; if that count exceeds max_deletes, NOTHING
    is applied for that role this cycle (every one of its actions is left
    unapplied, tagged breaker=True in its result) and one ERROR is logged
    for the role. Other (non-tripped) roles are entirely unaffected.

    Per-action try/except: a raised exception from either writer is caught,
    logged, and recorded as that action's apply_error -- it is NEVER
    re-raised, so one bad row can never abort the rest of the cycle's apply
    pass.

    Returns {"applied": N, "apply_errors": N, "skipped_operator_note": N,
    "breaker_tripped": [role, ...], "results": [dict, ...]} where results[i]
    describes actions[i]: {"applied": bool, "apply_error": str|None,
    "skipped_operator_note": bool, "breaker": bool} plus, whenever a
    delete/purge_handoff actually issued its delete, "deleted_rows" (the
    helper's rowcount) -- a 0 there means the source-scoped SQL matched
    nothing (e.g. a legacy row still untagged), observable residue for the
    checkpoint gate instead of silently reading as a clean apply.
    """
    per_role_deletes = collections.Counter()
    for owner_role, action, mac, detail in actions:
        if owner_role not in enforced_roles:
            continue
        if action in ("delete", "purge_handoff"):
            per_role_deletes[owner_role] += 1
        elif action == "upsert" and _needs_relocation_cleanup(
                actuals_by_mac.get(mac), detail):
            # The relocation cleanup this upsert will trigger is a real kea
            # delete -- bound it by the same per-role breaker budget.
            per_role_deletes[owner_role] += 1
    breaker_tripped = sorted(
        role for role, n in per_role_deletes.items() if n > max_deletes)
    breaker_set = set(breaker_tripped)
    for role in breaker_tripped:
        log.error(
            "materializer breaker tripped for role=%s: %d planned deletions "
            "(incl purge_handoff) > max_deletes=%d -- applying NOTHING for "
            "this role this cycle", role, per_role_deletes[role], max_deletes)

    applied = 0
    apply_errors = 0
    skipped_operator_note = 0
    results = []
    for owner_role, action, mac, detail in actions:
        result = {"applied": False, "apply_error": None,
                  "skipped_operator_note": False, "breaker": False}
        if owner_role not in enforced_roles:
            results.append(result)
            continue
        if owner_role in breaker_set:
            result["breaker"] = True
            results.append(result)
            continue
        try:
            if action == "upsert":
                _apply_upsert(pool, owner_role, mac, detail)
                # Same-owner vid move: the upsert just INSERTed a new-subnet
                # row (kea's conflict key includes dhcp4_subnet_id), so clean
                # up the stranded old-subnet row NOW -- left in place it
                # would make this mac two actuals next cycle and trip the
                # planner's multi_actual freeze. Runs only AFTER the upsert
                # succeeded (a failed upsert must not delete the mac's only
                # remaining row) and only on actuals_by_mac evidence.
                if _needs_relocation_cleanup(actuals_by_mac.get(mac), detail):
                    result["deleted_rows"] = delete_other_subnet_rows(
                        pool, mac=mac, source=owner_role,
                        keep_vid=detail["desired"]["vid"])
                result["applied"] = True
                applied += 1
            elif action == "delete":
                actual = actuals_by_mac.get(mac) or {}
                if actual.get("operator_note"):
                    result["skipped_operator_note"] = True
                    skipped_operator_note += 1
                else:
                    # Record the actual rowcount: a 0-row delete (e.g. a
                    # legacy row still untagged, so the source-scoped SQL
                    # matched nothing) is observable residue in the plan
                    # row rather than silently reading as a clean apply.
                    result["deleted_rows"] = delete_hosts_for_mac(
                        pool, mac=mac, source=owner_role)
                    result["applied"] = True
                    applied += 1
            elif action == "purge_handoff":
                actual = actuals_by_mac.get(mac) or {}
                if actual.get("operator_note"):
                    result["skipped_operator_note"] = True
                    skipped_operator_note += 1
                else:
                    result["deleted_rows"] = delete_hosts_for_mac(
                        pool, mac=mac, source=detail["from"])
                _apply_upsert(pool, owner_role, mac, detail)
                result["applied"] = True
                applied += 1
            else:
                log.error("apply_actions: unknown action %r for role=%s mac=%s",
                          action, owner_role, mac)
        except Exception as e:
            log.error("apply_actions failed role=%s action=%s mac=%s: %s",
                      owner_role, action, mac, e)
            result["applied"] = False
            result["apply_error"] = str(e)[:200]
            apply_errors += 1
        results.append(result)

    return {"applied": applied, "apply_errors": apply_errors,
            "skipped_operator_note": skipped_operator_note,
            "breaker_tripped": breaker_tripped, "results": results}


def run_cycle(pool, owners: set, *, enforced_roles: frozenset = frozenset(),
             max_deletes: int = _DEFAULT_MAX_DELETES_PER_RUN) -> dict:
    """Read desired + actuals, plan, apply (for enforced roles only), write
    plan rows (now carrying `applied`/`apply_error`/`skipped_operator_note`/
    `breaker` flags in each row's detail), sweep rows older than 7 days.

    `apply_actions` is called ONLY when `enforced_roles` is non-empty --
    with the deployed default (enforced_roles=frozenset()) this function
    never calls it and never touches kea.hosts/kea.ipv6_reservations,
    exactly like the phase-2 run_shadow this supersedes.

    Returns {"planned": N, "by_action": {...}, "skipped": {...}, "applied":
    N, "apply_errors": N, "skipped_operator_note": N, "breaker_tripped":
    [role, ...]} -- the summary marker row's detail mirrors this dict.
    """
    desired = read_desired(pool)
    actuals = read_kea_actuals(pool)
    actions, skipped = plan_materialization(desired, actuals, owners)

    apply_result = {"applied": 0, "apply_errors": 0,
                    "skipped_operator_note": 0, "breaker_tripped": [],
                    "results": [None] * len(actions)}
    if enforced_roles:
        actuals_by_mac = {}
        for a in actuals:
            actuals_by_mac[_norm(a["mac"])] = a
        apply_result = apply_actions(
            pool, actions, enforced_roles=enforced_roles,
            max_deletes=max_deletes, actuals_by_mac=actuals_by_mac)

    run_id = _next_run_id()
    by_action = collections.Counter(a[1] for a in actions)
    result = {"planned": len(actions), "by_action": dict(by_action),
              "skipped": skipped, "applied": apply_result["applied"],
              "apply_errors": apply_result["apply_errors"],
              "skipped_operator_note": apply_result["skipped_operator_note"],
              "breaker_tripped": apply_result["breaker_tripped"]}

    with pool.connection() as conn:
        with conn.cursor() as cur:
            if actions:
                rows = []
                for (owner_role, action, mac, detail), r in zip(
                        actions, apply_result["results"]):
                    row_detail = {**detail, "run": run_id}
                    if r is None:
                        row_detail["applied"] = False
                    else:
                        row_detail["applied"] = r["applied"]
                        if r["apply_error"]:
                            row_detail["apply_error"] = r["apply_error"]
                        if r["skipped_operator_note"]:
                            row_detail["skipped_operator_note"] = True
                        if r["breaker"]:
                            row_detail["breaker"] = True
                        if r.get("deleted_rows") is not None:
                            row_detail["deleted_rows"] = r["deleted_rows"]
                    rows.append({
                        "owner_role": owner_role, "action": action, "mac": mac,
                        "detail": Jsonb(row_detail),
                    })
                cur.executemany(_INSERT_PLAN_SQL, rows)
            cur.execute(_INSERT_PLAN_SQL, {
                "owner_role": "-", "action": "summary", "mac": "-",
                "detail": Jsonb({"run": run_id, **result}),
            })
        conn.execute(_RETENTION_SQL)

    return result


def run_shadow(pool, owners: set) -> dict:
    """Thin alias for run_cycle with the phase-2 defaults (no enforcement,
    max_deletes irrelevant since nothing is ever applied) -- kept for
    tests/compat so the shadow-mode return shape stays exactly
    {"planned", "by_action", "skipped"}, byte-identical to phase 2.
    """
    full = run_cycle(pool, owners)
    return {"planned": full["planned"], "by_action": full["by_action"],
            "skipped": full["skipped"]}
