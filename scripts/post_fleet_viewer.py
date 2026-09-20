#!/usr/bin/env python3
"""Quick live viewer for the eindhoven post fleet (rabbit-edam).

Stdlib only, no third-party deps. Pulls live data (postgres `flax` db in
flax-stack-postgres-1 + the /etc/flax/post_*.json firmware stores) on a
short cache TTL, and serves a field-selectable, sortable table over a few
different views.

Two run modes:
  --local          Run ON the bang itself (how apply_post_fleet_viewer
                    deploys it): shells out to `docker exec` / `cat`
                    directly, no ssh hop. Binds 127.0.0.1 by default --
                    it sits behind flax-stack's nginx TLS vhost
                    (viewer.<host>.flaxadvisors.cloud), which reaches it
                    over the host network the same way it reaches the
                    post/triage/console backends.
  --host <bang>     Run from an operator workstation/dev box, pulling data
                    over ssh to <bang>. Binds 0.0.0.0 by default so it's
                    reachable when this process itself runs inside a VM
                    whose browser is on the host (see reaper-devel/CLAUDE.md,
                    "Two dev-env constraints").

Usage:
    python3 scripts/post_fleet_viewer.py --local [--port 8799]
    python3 scripts/post_fleet_viewer.py --host bang-gouda [--port 8799]
"""
import argparse
import html
import json
import re
import subprocess
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Needs python >= 3.7 (ThreadingHTTPServer, subprocess.run(capture_output=)).
# bang-gouda's system /usr/bin/python3 is 3.6 -- the systemd unit
# (apply_post_fleet_viewer) explicitly invokes /usr/bin/python3.11 instead.

BANG_HOST = "bang-gouda"
LOCAL_MODE = False
CACHE_TTL = 4.0
SSH_TIMEOUT = 15


# --------------------------------------------------------------------------
# Data access -- either ssh out to a bang, or (LOCAL_MODE, when this script
# runs on the bang itself, e.g. deployed via apply_post_fleet_viewer) run the
# same shell commands directly, no ssh hop needed. Each raw fetch is cached
# briefly so a page with a short poll interval (or several open tabs) doesn't
# spawn a subprocess per column-toggle.
# --------------------------------------------------------------------------

_cache_lock = threading.Lock()
_cache = {}  # key -> (timestamp, value)
_last_error = {"msg": None, "at": 0}


def _run(cmd):
    argv = ["sh", "-c", cmd] if LOCAL_MODE else (
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=6", BANG_HOST, cmd])
    r = subprocess.run(argv, capture_output=True, text=True, timeout=SSH_TIMEOUT)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or f"command exited {r.returncode}").strip())
    return r.stdout


def _psql_rows(query, columns):
    sep = "\x1f"
    cmd = (
        "docker exec flax-stack-postgres-1 psql -U postgres -d flax "
        f"-t -A -F$'{sep}' -c \"{query}\""
    )
    out = _run(cmd)
    rows = []
    for line in out.splitlines():
        if not line.strip():
            continue
        parts = line.split(sep)
        rows.append(dict(zip(columns, parts)))
    return rows


def _read_json(path):
    out = _run(f"cat {path} 2>/dev/null || echo '{{}}'")
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _cached(key, fn, ttl=None):
    with _cache_lock:
        entry = _cache.get(key)
        if entry and time.time() - entry[0] < (CACHE_TTL if ttl is None else ttl):
            return entry[1]
    try:
        value = fn()
        _last_error["msg"] = None
    except Exception as exc:  # noqa: BLE001 -- surface any ssh/db failure to the page
        with _cache_lock:
            entry = _cache.get(key)
        _last_error["msg"] = str(exc)
        _last_error["at"] = time.time()
        if entry:
            return entry[1]  # serve last-known-good rather than a blank page
        raise
    with _cache_lock:
        _cache[key] = (time.time(), value)
    return value


# A pasted ID list is long: 48 serials is ~670 chars, so the old 200-char
# clip silently ate most of one. Still bounded -- this arrives in a URL.
Q_MAX_CHARS = 4000


def _clip_q(value):
    return (value or "").strip()[:Q_MAX_CHARS]


def _port_key(port):
    m = re.match(r"^et(\d+)b(\d+)$", port or "")
    return (int(m.group(1)), int(m.group(2))) if m else (9999, 9999)


_TOKEN_RE = re.compile(r"\d+|\D+")


def _natural_key(value):
    """`sort -V` style key: split into digit/non-digit runs, compare digit runs
    numerically and text runs case-insensitively. Each token is tagged (0, int)
    or (1, str) so tuples with a different shape at some position still compare
    safely instead of raising (e.g. a bare "—" against "TPC_P26F")."""
    s = "" if value is None else str(value)
    key = []
    for tok in _TOKEN_RE.findall(s):
        key.append((0, int(tok)) if tok.isdigit() else (1, tok.lower()))
    return key


# --------------------------------------------------------------------------
# Views: each is a live-fetched list of dict rows plus the column metadata
# the UI needs to build checkboxes.
# --------------------------------------------------------------------------

def fetch_post_state():
    rows = _cached("post_state", lambda: _psql_rows(
        "SELECT port,switch,serial,bmc_mac,order_no,updated_at,"
        "vars->'done'->>'verdict',vars->'pop'->>'verdict',vars->>'power_on' "
        "FROM post_state ORDER BY port",
        ["port", "switch", "serial", "bmc_mac", "order_no", "updated_at",
         "verdict", "pop", "power_on"],
    ))
    return sorted(rows, key=lambda r: _port_key(r["port"]))


def fetch_post_node():
    """Durable node history (post_node) -- unlike post_state this is NEVER
    garbage-collected when a node leaves the switch (flax_post/observe/gc.py:
    "post_node is never touched"), so it's the view that answers "what used
    to be plugged in here". `attached` is computed, not stored: a node counts
    as attached now iff its bmc_mac still has a live post_state row -- gc.py
    deletes that row ~5min (POST_STATE_GC_GRACE_SECS) after the mac drops out
    of the switch's own FDB, provided it's unreserved and not mid-flash."""
    rows = _cached("post_node", lambda: _psql_rows(
        "SELECT bmc_mac,serial,host_mac,order_no,last_switch,last_port,customer,updated_at,"
        "vars->'result'->>'verdict',to_timestamp((vars->'result'->>'finished_at')::numeric) "
        "FROM post_node ORDER BY last_port, updated_at DESC",
        ["bmc_mac", "serial", "host_mac", "order_no", "last_switch", "last_port",
         "customer", "updated_at", "verdict", "finished"],
    ))
    live_macs = {r["bmc_mac"] for r in fetch_post_state() if r.get("bmc_mac")}
    for r in rows:
        r["attached"] = "attached" if r["bmc_mac"] in live_macs else "detached"
        r["link"] = "/node?mac=" + urllib.parse.quote(r["bmc_mac"] or "")
    return sorted(rows, key=lambda r: (_port_key(r["last_port"]), r["updated_at"]), reverse=False)


def _sql_lit(s):
    """MAC / run ids only: restrict to the safe character set instead of quoting,
    since the query travels through a shell -c string into psql."""
    return re.sub(r"[^0-9a-zA-Z:_-]", "", str(s or ""))


def fetch_node(bmc_mac):
    """One post_node row with its vars parsed (the durable tier), or None."""
    rows = _psql_rows(
        "SELECT bmc_mac,serial,host_mac,order_no,last_switch,last_port,customer,vars::text "
        f"FROM post_node WHERE bmc_mac = '{_sql_lit(bmc_mac)}'",
        ["bmc_mac", "serial", "host_mac", "order_no", "last_switch", "last_port", "customer", "vars"])
    if not rows:
        return None
    r = rows[0]
    try:
        r["vars"] = json.loads(r.get("vars") or "{}")
    except ValueError:
        r["vars"] = {}
    if not isinstance(r["vars"], dict):
        r["vars"] = {}
    return r


def fetch_run_artifacts(bmc_mac):
    """Every post_artifact row (metadata only) for a blade, newest run first."""
    return _psql_rows(
        "SELECT id,run_id,stage,name,kind,bytes FROM post_artifact "
        f"WHERE bmc_mac = '{_sql_lit(bmc_mac)}' ORDER BY captured_at DESC, stage, name",
        ["id", "run_id", "stage", "name", "kind", "bytes"])


def fetch_artifact(aid):
    """One artifact including its content. The content is multi-line, so it
    cannot go through _psql_rows' line splitter: the metadata comes from one
    query and the body verbatim from a second, single-cell one."""
    if not re.fullmatch(r"\d+", str(aid or "")):
        return None
    rows = _psql_rows(
        "SELECT id,bmc_mac,serial,run_id,stage,name,kind "
        f"FROM post_artifact WHERE id = {int(aid)}",
        ["id", "bmc_mac", "serial", "run_id", "stage", "name", "kind"])
    if not rows:
        return None
    body = _run("docker exec flax-stack-postgres-1 psql -U postgres -d flax -t -A "
                f"-c \"SELECT content FROM post_artifact WHERE id = {int(aid)}\"")
    rows[0]["content"] = body[:-1] if body.endswith("\n") else body
    return rows[0]


ART_SEARCH_TTL = 30.0   # a content scan is ~1.5s over ~115MB; don't redo it every 5s poll
ART_HITS_PER_NODE = 3   # newest matching artifacts linked per blade; the rest are counted


def fetch_artifact_hits(term):
    """{bmc_mac: {"count": n, "hits": [{id, stage, name}, ...]}} for every blade
    with an artifact whose content contains `term` (case-insensitive substring).

    The term is operator free text headed through `sh -c` (and ssh) into SQL, so
    it never appears raw: LIKE metacharacters are escaped here, then the whole
    pattern travels hex-encoded and is decoded inside postgres."""
    pattern = "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    hexpat = pattern.encode("utf-8").hex()
    key = "art:" + hexpat

    def query():
        rows = _psql_rows(
            "SELECT bmc_mac,id,stage,name,n FROM ("
            "SELECT bmc_mac,id,stage,name,count(*) OVER w AS n,"
            "row_number() OVER (w ORDER BY captured_at DESC, id DESC) AS rn "
            "FROM post_artifact "
            f"WHERE content ILIKE convert_from(decode('{hexpat}','hex'),'UTF8') "
            "WINDOW w AS (PARTITION BY bmc_mac)) t "
            f"WHERE rn <= {ART_HITS_PER_NODE} ORDER BY bmc_mac, rn",
            ["bmc_mac", "id", "stage", "name", "n"])
        hits = {}
        for r in rows:
            h = hits.setdefault(r["bmc_mac"], {"count": int(r["n"] or 0), "hits": []})
            h["hits"].append({"id": r["id"], "stage": r["stage"], "name": r["name"]})
        return hits

    with _cache_lock:  # one entry per distinct term typed; drop the expired ones
        now = time.time()
        for k in [k for k, (at, _) in _cache.items()
                  if k.startswith("art:") and k != key and now - at > ART_SEARCH_TTL]:
            del _cache[k]
    return _cached(key, query, ttl=ART_SEARCH_TTL)


def fetch_fleet():
    state = fetch_post_state()
    # host_mac isn't in post_state -- pull it from post_node, keyed on bmc_mac
    # (post_node's primary key, so this dict is 1:1, no collision risk).
    nodes = fetch_post_node()
    host_mac_by_bmc = {r["bmc_mac"]: r["host_mac"] for r in nodes if r.get("bmc_mac")}
    # The verdict that counts is the RECORDED one (post_node.vars.result, pushed
    # by the engine the moment a run reaches Done). The slot's live verdict is
    # kept as a separate, off-by-default column: it clears on power-on/swap and
    # rows from before the recording existed carry verdicts nobody trusts.
    verdict_by_bmc = {r["bmc_mac"]: r.get("verdict") for r in nodes if r.get("bmc_mac")}
    finished_by_bmc = {r["bmc_mac"]: r.get("finished") for r in nodes if r.get("bmc_mac")}
    bios = _cached("bios_json", lambda: _read_json("/etc/flax/post_bios_fw.json"))
    bmc = _cached("bmc_json", lambda: _read_json("/etc/flax/post_fw.json"))
    rows = []
    for s in state:
        port = s["port"]
        b = bios.get(port, {})
        m = bmc.get(port, {})
        rows.append({
            "port": port,
            "switch": s["switch"],
            "serial": s["serial"],
            "order_no": s["order_no"],
            "bmc_mac": s.get("bmc_mac") or "—",
            "host_mac": host_mac_by_bmc.get(s.get("bmc_mac")) or "—",
            "bios_fw": b.get("current") or "—",
            "bios_phase": b.get("phase") or "no-data",
            "bmc_fw": m.get("current_version") or "—",
            "bmc_phase": m.get("phase") or "no-data",
            "bmc_ip": m.get("bmc_ip") or "—",
            "verdict": verdict_by_bmc.get(s.get("bmc_mac")) or "",
            "finished": finished_by_bmc.get(s.get("bmc_mac")) or "",
            "slot_verdict": s.get("verdict") or "",
            "pop": s.get("pop") or "",
            "power_on": s.get("power_on") or "",
            "state_updated": s["updated_at"],
            "link": "/node?mac=" + urllib.parse.quote(s.get("bmc_mac") or "") if s.get("bmc_mac") else "",
        })
    return rows


VIEWS = {
    "fleet": {
        "label": "Post fleet (joined)",
        "query": "post_state ⋈ post_node ⋈ post_bios_fw.json ⋈ post_fw.json",
        "columns": [
            ("port", "Switch port"), ("serial", "Serial"), ("switch", "Switch"),
            ("order_no", "Order"), ("bmc_mac", "BMC MAC"), ("host_mac", "Host MAC"),
            ("bios_fw", "BIOS FW"), ("bios_phase", "BIOS status"),
            ("bmc_fw", "BMC FW"), ("bmc_phase", "BMC status"), ("bmc_ip", "BMC IP"),
            ("verdict", "Verdict"), ("finished", "Finished"), ("slot_verdict", "Slot verdict (live)"),
            ("pop", "Population"), ("power_on", "Power"),
            ("state_updated", "post_state updated_at"), ("link", "Node page"),
        ],
        "default": ["port", "serial", "verdict", "finished", "pop", "power_on", "bios_fw",
                    "bios_phase", "bmc_fw", "bmc_phase", "link"],
        "fetch": fetch_fleet,
    },
    "post_state": {
        "label": "post_state (raw, live only)",
        "query": "SELECT * FROM post_state  -- GC'd ~5min after a node leaves the switch",
        "columns": [
            ("port", "Port"), ("switch", "Switch"), ("serial", "Serial"),
            ("bmc_mac", "BMC MAC"), ("order_no", "Order"), ("updated_at", "Updated at"),
        ],
        "default": ["port", "switch", "serial", "order_no", "updated_at"],
        "fetch": fetch_post_state,
    },
    "post_node": {
        "label": "post_node (all history, incl. detached)",
        "query": "SELECT * FROM post_node  -- never GC'd; attached = has a live post_state row",
        "columns": [
            ("attached", "Attached?"), ("bmc_mac", "BMC MAC"), ("serial", "Serial"),
            ("host_mac", "Host MAC"), ("order_no", "Order"), ("last_switch", "Last switch"),
            ("last_port", "Last port"), ("customer", "Customer"),
            ("updated_at", "Updated at"), ("verdict", "Verdict"),
            ("finished", "Finished"), ("link", "Node page"),
        ],
        "default": ["attached", "bmc_mac", "serial", "last_port", "order_no", "verdict",
                    "finished", "link"],
        "fetch": fetch_post_node,
    },
}

PHASE_CLASS = {
    "up_to_date": "good", "needs_update": "warn",
    "unreachable": "bad", "unsupported": "dim", "no-data": "dim",
    "attached": "good", "detached": "dim",
    "pass": "good", "fail": "bad", "green": "good", "red": "bad", "grey": "dim",
    "on": "good", "off": "dim",
}


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

def _cell(col, value):
    if col == "art_hits":
        return render_art_hits(value)
    value = "" if value is None else str(value)
    if col in ("verdict", "slot_verdict", "pop", "power_on") and not value:
        return '<span class="pill dim">—</span>'
    if col in ("bios_phase", "bmc_phase", "attached", "verdict", "slot_verdict", "pop", "power_on"):
        cls = PHASE_CLASS.get(value, "dim")
        return f'<span class="pill {cls}">{html.escape(value)}</span>'
    if col == "link" and value:
        return f'<a href="{html.escape(value)}">open</a>'
    return html.escape(value)


ID_FIELDS = ("serial", "bmc_mac", "host_mac")
_BARE_MAC_RE = re.compile(r"[0-9a-f]{12}\Z")
_MAC_SEP_RE = re.compile(r"[:.-]")


def _norm_id(value):
    """An ID as compared: case-folded, and a MAC-shaped token loses its
    separators, so 98039ba6fdfc, 98:03:9B:A6:FD:FC and 98-03-9b-a6-fd-fc all
    compare equal. Everything else (a serial) is compared case-folded only."""
    s = str(value or "").strip().casefold()
    bare = _MAC_SEP_RE.sub("", s)
    return bare if _BARE_MAC_RE.match(bare) else s


def _id_tokens(q):
    """The typed list, uniquified on the COMPARED form: the same ID spelled two
    ways -- different case, or a MAC with and without its separators -- is one
    ID and is looked up once. The first spelling seen is the one kept, and the
    order typed is preserved."""
    seen, out = set(), []
    for token in (q or "").split():
        key = _norm_id(token)
        if key and key not in seen:
            seen.add(key)
            out.append(token)
    return out


def _id_dupes(q):
    """How many tokens uniquifying dropped, so a paste holding the same ID
    twice can say so instead of silently coming back shorter."""
    return len((q or "").split()) - len(_id_tokens(q))


def _id_search(rows, q):
    """Exact ID lookup for a whitespace-separated list, driven by the INPUT,
    not by the fleet: one entry per unique ID, in the order typed, as
    (index, token, matching rows). Indices run 1..N over the uniquified list
    with no gaps, and an ID that matches nothing keeps its index with an empty
    list, so the column never skips a number. Only ID_FIELDS match, and only
    whole values: a serial prefix finds nothing."""
    index = {}
    for row in rows:
        for field in ID_FIELDS:
            key = _norm_id(row.get(field))
            if key:
                index.setdefault(key, []).append(row)
    out = []
    for i, token in enumerate(_id_tokens(q), 1):
        hits, seen = [], set()
        for row in index.get(_norm_id(token), ()):
            if id(row) not in seen:          # a row indexed under two of its ID fields
                seen.add(id(row))
                hits.append(row)
        out.append((i, token, hits))
    return out


def _row_matches(row, needle):
    """Case-insensitive substring over every field the view fetched for the
    row, shown or not -- not just the ticked columns."""
    return any(needle in str(v).casefold() for k, v in row.items()
               if k not in ("link", "art_hits") and v)


def render_art_hits(entry):
    if not entry:
        return ""
    links = " ".join(
        f'<a href="/artifact?id={_esc(h["id"])}">{_esc(h["stage"])}/{_esc(h["name"])}</a>'
        for h in entry["hits"])
    more = entry["count"] - len(entry["hits"])
    return links + (f' <span class="more">+{more} more</span>' if more > 0 else "")


def render_table(view_key, selected_cols, sort_col=None, sort_dir="asc", q="", art=False,
                 ids=False):
    view = VIEWS[view_key]
    cols = [c for c in selected_cols if c in dict(view["columns"])] or view["default"]
    labels = dict(view["columns"])
    rows = view["fetch"]()
    total = len(rows)

    q = (q or "").strip()
    art_hits = None
    search_note = ""
    id_entries = None
    if q and ids:
        # Input-driven: the table is the list that was typed, one index per
        # token. Artifact search is skipped -- it takes a single term, and one
        # query per token would be N round trips.
        id_entries = _id_search(rows, q)
        found = sum(1 for _i, _t, hits in id_entries if hits)
        missing = len(id_entries) - found
        n_rows = sum(len(hits) or 1 for _i, _t, hits in id_entries)
        dropped = _id_dupes(q)
        search_note = (f"{found} of {len(id_entries)} IDs found &middot; {n_rows} rows"
                       + (f" &middot; {missing} not found" if missing else "")
                       + (f" &middot; {dropped} duplicate{'' if dropped == 1 else 's'} dropped"
                          if dropped else "")
                       + " &middot; ")
    elif q:
        needle = q.casefold()
        if art:
            art_hits = fetch_artifact_hits(q)
            rows = [r for r in rows if _row_matches(r, needle) or r.get("bmc_mac") in art_hits]
            rows = [dict(r, art_hits=art_hits.get(r.get("bmc_mac"))) for r in rows]
            cols = cols + ["art_hits"]
            labels = dict(labels, art_hits="Matched artifacts")
        else:
            rows = [r for r in rows if _row_matches(r, needle)]
        search_note = (f"{len(rows)} of {total} rows match <b>{html.escape(q)}</b>"
                       + (f" (fields, or artifacts on {len(art_hits)} blades)" if art else " (fields)")
                       + " &middot; ")

    # ID mode sorts whole index GROUPS below, not individual rows.
    if id_entries is None and sort_col in cols:
        rows = sorted(rows, key=lambda r: _natural_key(r.get(sort_col) if sort_col != "art_hits"
                                                      else (r.get("art_hits") or {}).get("count")),
                      reverse=(sort_dir == "desc"))

    def th(col):
        label = html.escape(labels[col])
        arrow = ""
        if col == sort_col:
            arrow = ' <span class="arrow">' + ("▾" if sort_dir == "asc" else "▴") + "</span>"
        next_dir = "desc" if (col == sort_col and sort_dir == "asc") else "asc"
        return (f'<th data-col="{col}" data-dir="{next_dir}" onclick="sortBy(this)">'
                f'{label}{arrow}</th>')

    thead = "".join(th(c) for c in cols)
    span = len(cols)
    body_rows = []
    if id_entries is not None:
        thead = '<th class="idx">#</th><th class="sid">Searched ID</th>' + thead
        span += 2
        if sort_col in cols:
            # Sort whole groups, never individual rows: an ID that matched
            # twice keeps its rows together. Not-found groups always sink to
            # the bottom, both directions, in the order they were typed. The
            # index column keeps its INPUT numbers -- it says which line of the
            # pasted list a row came from, so it is not renumbered.
            def _k(row):
                return _natural_key(row.get(sort_col))
            rev = sort_dir == "desc"
            id_entries = [(i, t, sorted(h, key=_k, reverse=rev)) for i, t, h in id_entries]
            found = [e for e in id_entries if e[2]]
            found.sort(key=lambda e: _k(e[2][0]), reverse=rev)
            id_entries = found + [e for e in id_entries if not e[2]]
        for pos, (i, token, hits) in enumerate(id_entries, 1):
            # Band by the group's POSITION on screen, not by row: every row of
            # one ID shares a shade and the next ID flips it -- a ledger for
            # the eye, which still reads once sorting has moved groups around.
            band = "band-a" if pos % 2 else "band-b"
            lead = f'<td class="idx">{i}</td>'
            if hits:
                for r in hits:
                    tds = "".join(f"<td>{_cell(c, r.get(c))}</td>" for c in cols)
                    body_rows.append(f'<tr class="{band}">{lead}'
                                     f'<td class="sid">{html.escape(token)}</td>{tds}</tr>')
            else:
                blanks = "<td></td>" * len(cols)
                body_rows.append(f'<tr class="{band}">{lead}'
                                 f'<td class="sid miss">{html.escape(token)}</td>{blanks}</tr>')
    else:
        for r in rows:
            tds = "".join(f"<td>{_cell(c, r.get(c))}</td>" for c in cols)
            body_rows.append(f"<tr>{tds}</tr>")
    tbody = "".join(body_rows) or f'<tr><td colspan="{span}" class="empty">no rows</td></tr>'

    err = ""
    if _last_error["msg"] and time.time() - _last_error["at"] < 30:
        err = f'<div class="err">ssh/db error, showing last-known-good: {html.escape(_last_error["msg"])}</div>'

    return (
        f'{err}'
        f'<div class="meta">{search_note or f"{len(rows)} rows &middot; "}{html.escape(view["query"])} '
        f'&middot; refreshed {time.strftime("%H:%M:%S")}</div>'
        f'<div class="table-scroll"><table><thead><tr>{thead}</tr></thead>'
        f'<tbody>{tbody}</tbody></table></div>'
    )


def _kv(label, value):
    return (f'<span class="lbl">{html.escape(label)}</span>'
            f'<span class="mono">{html.escape("" if value is None else str(value))}</span>')


def _ts(epoch):
    try:
        return time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime(int(epoch or 0)))
    except (TypeError, ValueError, OverflowError):
        return ""


def _esc(v):
    return html.escape("" if v is None else str(v))


def render_node_page(bmc_mac):
    """The durable record of one blade: identity, the latest finished run's
    result (firmware versions, verdict, population, done record), the run
    history, and every artifact captured for it (linking to /artifact)."""
    node = fetch_node(bmc_mac)
    if not node:
        return SUBPAGE.format(title="node", body=f"<p>no post_node row for {_esc(bmc_mac)}</p>")
    v = node.get("vars") or {}
    res = v.get("result") or {}
    ident = "".join(_kv(k, node.get(k)) for k in
                    ("bmc_mac", "serial", "host_mac", "order_no", "last_switch", "last_port"))
    parts = [f'<h2>node {_esc(node.get("serial") or bmc_mac)}</h2><div class="kv-grid">{ident}</div>']
    if res:
        fw = res.get("fw") or {}
        pop = res.get("pop") or {}
        done = res.get("done") or {}
        fwrows = "".join(
            f"<tr><td>{n}</td><td>{_esc((fw.get(k) or {}).get('current'))}</td>"
            f"<td>{_esc((fw.get(k) or {}).get('target'))}</td>"
            f"<td>{_esc((fw.get(k) or {}).get('phase'))}</td></tr>"
            for n, k in (("BMC", "bmc"), ("BIOS", "bios")))
        nics = "".join(
            f"<tr><td>NIC {_esc(d.get('pci'))}</td><td>{_esc(d.get('current'))}</td>"
            f"<td>{_esc(d.get('target'))}</td><td>{_esc(d.get('phase'))}</td></tr>"
            for d in ((fw.get("nic") or {}).get("devices") or []) if isinstance(d, dict))
        failed = "".join(f"<li>{_esc(r)}</li>" for r in (pop.get("failed_rules") or [])) or "<li>none</li>"
        parts.append(
            '<h3>latest result</h3><div class="kv-grid">'
            + _kv("run_id", res.get("run_id")) + _kv("verdict", res.get("verdict"))
            + _kv("finished", _ts(res.get("finished_at")))
            + _kv("port", f"{res.get('port') or ''} under {res.get('order_no') or ''}")
            + _kv("population", f"{pop.get('profile') or ''} -> {pop.get('verdict') or ''}")
            + _kv("done", ", ".join(f"{k}={val}" for k, val in done.items()))
            + "</div>"
            "<table><thead><tr><th>firmware</th><th>current</th><th>target</th><th>phase</th></tr></thead>"
            f"<tbody>{fwrows}{nics}</tbody></table>"
            f"<h3>failed population rules</h3><ul>{failed}</ul>")
    else:
        parts.append("<p class='dim'>no finished run recorded for this blade</p>")
    runs = "".join(
        f"<tr><td class='mono'>{_esc(r.get('run_id'))}</td><td>{_esc(r.get('verdict'))}</td>"
        f"<td>{_ts(r.get('finished_at'))}</td><td>{_esc(r.get('order_no'))}</td><td>{_esc(r.get('port'))}</td></tr>"
        for r in (v.get("runs") or []) if isinstance(r, dict))
    arts = "".join(
        f"<tr><td class='mono'>{_esc(a.get('run_id'))}</td><td>{_esc(a.get('stage'))}</td>"
        f"<td><a href=\"/artifact?id={_esc(a.get('id'))}\">{_esc(a.get('name'))}</a></td>"
        f"<td>{_esc(a.get('kind'))}</td><td>{_esc(a.get('bytes'))}</td></tr>"
        for a in fetch_run_artifacts(bmc_mac))
    parts.append(
        "<h3>runs</h3><table><thead><tr><th>run</th><th>verdict</th><th>finished</th><th>order</th><th>port</th></tr></thead>"
        f"<tbody>{runs or '<tr><td colspan=5 class=dim>none</td></tr>'}</tbody></table>"
        "<h3>artifacts</h3><table><thead><tr><th>run</th><th>stage</th><th>name</th><th>kind</th><th>bytes</th></tr></thead>"
        f"<tbody>{arts or '<tr><td colspan=5 class=dim>none</td></tr>'}</tbody></table>")
    return SUBPAGE.format(title=f"node {_esc(node.get('serial') or bmc_mac)}", body="".join(parts))


def render_artifact_page(aid):
    a = fetch_artifact(aid)
    if not a:
        return SUBPAGE.format(title="artifact", body="<p>no such artifact</p>")
    head = " &middot; ".join(_esc(a.get(k)) for k in ("serial", "bmc_mac", "run_id", "stage", "name", "kind"))
    back = f'<p><a href="/node?mac={urllib.parse.quote(a.get("bmc_mac") or "")}">&larr; node</a></p>'
    return SUBPAGE.format(title=f"{_esc(a.get('stage'))}/{_esc(a.get('name'))}",
                          body=f"{back}<p class='mono'>{head}</p><pre>{_esc(a.get('content'))}</pre>")


SUBPAGE = """<!doctype html><html><head><meta charset="utf-8"><title>{title}</title>
<style>body{{font:14px system-ui;margin:1.5rem;color-scheme:light dark}} .mono{{font-family:ui-monospace,monospace}}
.dim{{color:#6b7a88}} .kv-grid{{display:grid;grid-template-columns:max-content 1fr;gap:.2rem 1rem}} .lbl{{color:#6b7a88}}
table{{border-collapse:collapse;margin:.5rem 0}} td,th{{border:1px solid #8884;padding:.2rem .5rem;text-align:left}}
pre{{white-space:pre-wrap;word-break:break-all;border:1px solid #8884;padding:.5rem}}</style></head>
<body><p><a href="/?view=post_node">&larr; post_node</a></p>{body}</body></html>"""


PAGE = """<!doctype html>
<html><head><meta charset="utf-8">
<title>Post fleet viewer</title>
<style>
  :root{{
    color-scheme: light dark;
    --bg:#eef1f4; --surface:#fff; --border:#d3dae1; --text:#161d24;
    --dim:#5c6b78; --accent:#0d7d8f;
    --good:#1c8a5c; --good-bg:#e0f3e8; --warn:#a8690b; --warn-bg:#faedd8;
    --bad:#b6432a; --bad-bg:#fbe6e0; --dimbg:#e7eaed; --band:#f4f7f9;
  }}
  @media (prefers-color-scheme: dark){{
    :root{{
      --bg:#0f1418; --surface:#161d23; --border:#2b353e; --text:#e8edf1;
      --dim:#9aa8b3; --accent:#54d3e0;
      --good:#4fd398; --good-bg:#123328; --warn:#f0b154; --warn-bg:#3a2c11;
      --bad:#f28468; --bad-bg:#3a1f18; --dimbg:#232c33; --band:#1b232a;
    }}
  }}
  *{{box-sizing:border-box}}
  body{{background:var(--bg);color:var(--text);font:14px/1.4 -apple-system,Segoe UI,Roboto,sans-serif;
       margin:0;padding:22px 20px 60px}}
  .wrap{{max-width:1080px;margin:0 auto}}
  h1{{font-size:19px;margin:0 0 2px}}
  .sub{{color:var(--dim);font-size:12.5px;margin-bottom:16px}}
  .controls{{display:flex;gap:18px;flex-wrap:wrap;align-items:flex-start;
             background:var(--surface);border:1px solid var(--border);border-radius:10px;
             padding:12px 16px;margin-bottom:14px}}
  .ctrl-group{{display:flex;flex-direction:column;gap:6px}}
  .ctrl-group .lbl{{font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:var(--dim)}}
  .views{{display:flex;gap:6px;flex-wrap:wrap}}
  .views a{{padding:5px 10px;border-radius:20px;font-size:12.5px;text-decoration:none;
            border:1px solid var(--border);color:var(--text)}}
  .views a.active{{background:var(--accent);border-color:var(--accent);color:#fff}}
  .cols{{display:flex;gap:10px;flex-wrap:wrap;max-width:560px}}
  .cols label{{font-size:12.5px;display:flex;gap:4px;align-items:center;cursor:pointer;white-space:nowrap}}
  select{{font-size:12.5px;padding:3px 6px;border-radius:6px;border:1px solid var(--border);
          background:var(--surface);color:var(--text)}}
  .search input[type=search]{{font-size:13px;padding:5px 8px;border-radius:6px;width:240px;max-width:100%;
          border:1px solid var(--border);background:var(--surface);color:var(--text)}}
  .search label{{font-size:12.5px;display:flex;gap:4px;align-items:center;cursor:pointer;white-space:nowrap}}
  td a{{color:var(--accent)}}
  td .more{{color:var(--dim)}}
  .meta{{font-size:11.5px;color:var(--dim);margin:0 0 8px;font-family:ui-monospace,monospace}}
  .err{{background:var(--warn-bg);color:var(--warn);border-radius:8px;padding:8px 12px;
        font-size:12.5px;margin-bottom:10px}}
  .table-scroll{{overflow-x:auto;background:var(--surface);border:1px solid var(--border);
                 border-radius:10px}}
  table{{border-collapse:collapse;width:100%;min-width:480px}}
  th{{text-align:left;font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:var(--dim);
      padding:8px 12px;border-bottom:1px solid var(--border);white-space:nowrap;
      cursor:pointer;user-select:none}}
  th:hover{{color:var(--text)}}
  th .arrow{{color:var(--accent);font-size:10px}}
  td{{padding:7px 12px;border-bottom:1px solid var(--border);font-family:ui-monospace,SFMono-Regular,monospace;
      font-size:12.5px;white-space:nowrap}}
  tr:last-child td{{border-bottom:none}}
  tr.band-b td{{background:var(--band)}}
  tr.band-a td{{background:var(--surface)}}
  td.idx,th.idx{{text-align:right;color:var(--dim);font-variant-numeric:tabular-nums;width:1%}}
  td.sid,th.sid{{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;white-space:nowrap}}
  td.sid.miss{{color:var(--bad);font-weight:600}}
  tr:hover td{{background:var(--dimbg)}}
  td.empty{{color:var(--dim);font-family:inherit;text-align:center;padding:20px}}
  .pill{{display:inline-block;padding:1px 8px;border-radius:20px;font-size:11px;font-weight:600}}
  .pill.good{{background:var(--good-bg);color:var(--good)}}
  .pill.warn{{background:var(--warn-bg);color:var(--warn)}}
  .pill.bad{{background:var(--bad-bg);color:var(--bad)}}
  .pill.dim{{background:var(--dimbg);color:var(--dim)}}
</style></head>
<body><div class="wrap">
  <h1>Post fleet viewer</h1>
  <div class="sub">Live from {source}: flax db + /etc/flax/post_*.json, rabbit-edam / eindhoven.</div>

  <form id="ctrl" class="controls" onsubmit="refresh(); return false;">
    <div class="ctrl-group search">
      <div class="lbl">Search</div>
      <input id="q" type="search" placeholder="serial, mac, port, anything..." value="{q_attr}" autocomplete="off">
      <label title="Match a whitespace-separated list of IDs exactly (serial, BMC MAC or host MAC) instead of searching for substrings. The list is uniquified first (the same ID in two spellings, or a MAC with and without colons, counts once), then numbered 1-N in the order typed; an ID that matches nothing still gets its own numbered, blank line.">
        <input id="ids" type="checkbox" {ids_checked}>exact ID list</label>
      <label title="Also match the text of each blade's post_artifact captures (a slower scan, cached 30s)." id="art-label">
        <input id="art" type="checkbox" {art_checked} {art_disabled}>also search node artifacts</label>
    </div>
    <div class="ctrl-group">
      <div class="lbl">View</div>
      <div class="views">{view_links}</div>
    </div>
    <div class="ctrl-group">
      <div class="lbl">Columns</div>
      <div class="cols">{col_checkboxes}</div>
    </div>
    <div class="ctrl-group">
      <div class="lbl">Refresh</div>
      <select id="interval" onchange="setInterval_(this.value)">
        <option value="0">off</option>
        <option value="2000">2s</option>
        <option value="5000" selected>5s</option>
        <option value="15000">15s</option>
      </select>
    </div>
  </form>

  <div id="table-wrap">{table_html}</div>
</div>

<script>
  const view = {view_json};
  let sortCol = {sort_col_json};
  let sortDir = {sort_dir_json};
  document.querySelectorAll('#ctrl .cols input[type=checkbox]').forEach(cb => {{
    cb.addEventListener('change', refresh);
  }});
  document.getElementById('art').addEventListener('change', refresh);
  let debounce = null;
  document.getElementById('q').addEventListener('input', () => {{
    clearTimeout(debounce);
    debounce = setTimeout(refresh, 350);
  }});
  let timer = null;
  let seq = 0;  // a slow artifact scan must not overwrite a newer answer
  function currentCols() {{
    return [...document.querySelectorAll('#ctrl .cols input[type=checkbox]:checked')].map(c => c.value);
  }}
  function sortBy(th) {{
    sortCol = th.dataset.col;
    sortDir = th.dataset.dir;  // header already carries the NEXT direction to apply
    refresh();
  }}
  function refresh() {{
    const cols = currentCols().join(',');
    const params = new URLSearchParams({{view, cols}});
    if (sortCol) {{ params.set('sort', sortCol); params.set('dir', sortDir); }}
    const q = document.getElementById('q').value.trim();
    if (q) params.set('q', q);
    const idsOn = document.getElementById('ids').checked;
    if (idsOn) params.set('ids', '1');
    // artifact search takes a single term -- it has no meaning for a list
    const artBox = document.getElementById('art');
    artBox.disabled = idsOn;
    if (artBox.checked && !idsOn) params.set('art', '1');
    const mine = ++seq;
    fetch(`/fragment?${{params}}`)
      .then(r => r.text())
      .then(t => {{ if (mine === seq) document.getElementById('table-wrap').innerHTML = t; }})
      .catch(() => {{}});
    const url = new URL(location);
    url.search = params;
    history.replaceState(null, '', url);
    document.querySelectorAll('.views a').forEach(a => {{
      const u = new URL(a.href);
      q ? u.searchParams.set('q', q) : u.searchParams.delete('q');
      params.has('art') ? u.searchParams.set('art', '1') : u.searchParams.delete('art');
      params.has('ids') ? u.searchParams.set('ids', '1') : u.searchParams.delete('ids');
      a.href = u;
    }});
  }}
  function setInterval_(ms) {{
    if (timer) clearInterval(timer);
    ms = parseInt(ms, 10);
    if (ms > 0) timer = setInterval(refresh, ms);
  }}
  setInterval_(document.getElementById('interval').value);
</script>
</body></html>"""


def render_page(view_key, selected_cols, sort_col=None, sort_dir="asc", q="", art=False,
                ids=False):
    carry = (("&q=" + urllib.parse.quote(q)) if q else "") + ("&art=1" if art else "")
    view_links = "".join(
        f'<a href="/?view={k}{html.escape(carry)}" class="{"active" if k == view_key else ""}">'
        f'{html.escape(v["label"])}</a>'
        for k, v in VIEWS.items()
    )
    view = VIEWS[view_key]
    selset = set(selected_cols) if selected_cols else set(view["default"])
    col_boxes = "".join(
        f'<label><input type="checkbox" value="{key}" {"checked" if key in selset else ""}>'
        f'{html.escape(label)}</label>'
        for key, label in view["columns"]
    )
    cols = sorted(selset, key=lambda c: [k for k, _ in view["columns"]].index(c))
    table_html = render_table(view_key, cols, sort_col, sort_dir, q, art, ids)
    source = "this host (local)" if LOCAL_MODE else BANG_HOST
    return PAGE.format(
        view_links=view_links,
        col_checkboxes=col_boxes,
        table_html=table_html,
        view_json=json.dumps(view_key),
        sort_col_json=json.dumps(sort_col),
        sort_dir_json=json.dumps(sort_dir),
        source=html.escape(source),
        q_attr=html.escape(q or "", quote=True),
        art_checked="checked" if (art and not ids) else "",
        art_disabled="disabled" if ids else "",
        ids_checked="checked" if ids else "",
    )


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # quiet -- avoid noisy stderr for a local dev tool

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        view_key = (qs.get("view", ["fleet"])[0])
        if view_key not in VIEWS:
            view_key = "fleet"
        cols_param = qs.get("cols", [""])[0]
        selected_cols = [c for c in cols_param.split(",") if c] or None
        sort_col = qs.get("sort", [None])[0] or None
        sort_dir = qs.get("dir", ["asc"])[0]
        if sort_dir not in ("asc", "desc"):
            sort_dir = "asc"
        q = _clip_q(qs.get("q", [""])[0])
        art = qs.get("art", [""])[0] == "1"
        ids = qs.get("ids", [""])[0] == "1"

        if parsed.path in ("/node", "/artifact"):
            try:
                body = (render_node_page(qs.get("mac", [""])[0]) if parsed.path == "/node"
                        else render_artifact_page(qs.get("id", [""])[0]))
            except Exception as exc:  # noqa: BLE001
                body = f"<pre>error: {html.escape(str(exc))}</pre>"
            encoded = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)
            return

        try:
            if parsed.path == "/fragment":
                body = render_table(view_key, selected_cols or VIEWS[view_key]["default"],
                                     sort_col, sort_dir, q, art, ids)
                content_type = "text/html; charset=utf-8"
            else:
                body = render_page(view_key, selected_cols, sort_col, sort_dir, q, art, ids)
                content_type = "text/html; charset=utf-8"
        except Exception as exc:  # noqa: BLE001
            body = f"<pre>error: {html.escape(str(exc))}</pre>"
            content_type = "text/html; charset=utf-8"

        encoded = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


def main():
    global BANG_HOST, LOCAL_MODE
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8799)
    ap.add_argument("--host", default=BANG_HOST,
                     help="ssh host to pull live data from (ignored with --local)")
    ap.add_argument("--local", action="store_true",
                     help="run commands directly (this process IS on the bang) instead of over ssh")
    ap.add_argument("--bind", default=None,
                     help="listen address (default: 127.0.0.1 with --local, 0.0.0.0 otherwise)")
    args = ap.parse_args()
    BANG_HOST = args.host
    LOCAL_MODE = args.local
    bind = args.bind or ("127.0.0.1" if LOCAL_MODE else "0.0.0.0")

    server = ThreadingHTTPServer((bind, args.port), Handler)
    source = "local (docker exec / cat)" if LOCAL_MODE else f"ssh {BANG_HOST}"
    print(f"post fleet viewer: listening on {bind}:{args.port}  (source: {source})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
