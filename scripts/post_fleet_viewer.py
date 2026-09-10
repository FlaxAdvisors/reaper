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


def _cached(key, fn):
    with _cache_lock:
        entry = _cache.get(key)
        if entry and time.time() - entry[0] < CACHE_TTL:
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
        "SELECT port,switch,serial,bmc_mac,order_no,updated_at FROM post_state ORDER BY port",
        ["port", "switch", "serial", "bmc_mac", "order_no", "updated_at"],
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
        "SELECT bmc_mac,serial,host_mac,order_no,last_switch,last_port,customer,updated_at "
        "FROM post_node ORDER BY last_port, updated_at DESC",
        ["bmc_mac", "serial", "host_mac", "order_no", "last_switch", "last_port",
         "customer", "updated_at"],
    ))
    live_macs = {r["bmc_mac"] for r in fetch_post_state() if r.get("bmc_mac")}
    for r in rows:
        r["attached"] = "attached" if r["bmc_mac"] in live_macs else "detached"
    return sorted(rows, key=lambda r: (_port_key(r["last_port"]), r["updated_at"]), reverse=False)


def fetch_fleet():
    state = fetch_post_state()
    # host_mac isn't in post_state -- pull it from post_node, keyed on bmc_mac
    # (post_node's primary key, so this dict is 1:1, no collision risk).
    host_mac_by_bmc = {r["bmc_mac"]: r["host_mac"] for r in fetch_post_node() if r.get("bmc_mac")}
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
            "state_updated": s["updated_at"],
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
            ("state_updated", "post_state updated_at"),
        ],
        "default": ["port", "serial", "bios_fw", "bios_phase", "bmc_fw", "bmc_phase"],
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
            ("updated_at", "Updated at"),
        ],
        "default": ["attached", "bmc_mac", "serial", "last_port", "order_no", "updated_at"],
        "fetch": fetch_post_node,
    },
}

PHASE_CLASS = {
    "up_to_date": "good", "needs_update": "warn",
    "unreachable": "bad", "unsupported": "dim", "no-data": "dim",
    "attached": "good", "detached": "dim",
}


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

def _cell(col, value):
    value = "" if value is None else str(value)
    if col in ("bios_phase", "bmc_phase", "attached"):
        cls = PHASE_CLASS.get(value, "dim")
        return f'<span class="pill {cls}">{html.escape(value)}</span>'
    return html.escape(value)


def render_table(view_key, selected_cols, sort_col=None, sort_dir="asc"):
    view = VIEWS[view_key]
    cols = [c for c in selected_cols if c in dict(view["columns"])] or view["default"]
    labels = dict(view["columns"])
    rows = view["fetch"]()

    if sort_col in cols:
        rows = sorted(rows, key=lambda r: _natural_key(r.get(sort_col)), reverse=(sort_dir == "desc"))

    def th(col):
        label = html.escape(labels[col])
        arrow = ""
        if col == sort_col:
            arrow = ' <span class="arrow">' + ("▾" if sort_dir == "asc" else "▴") + "</span>"
        next_dir = "desc" if (col == sort_col and sort_dir == "asc") else "asc"
        return (f'<th data-col="{col}" data-dir="{next_dir}" onclick="sortBy(this)">'
                f'{label}{arrow}</th>')

    thead = "".join(th(c) for c in cols)
    body_rows = []
    for r in rows:
        tds = "".join(f"<td>{_cell(c, r.get(c))}</td>" for c in cols)
        body_rows.append(f"<tr>{tds}</tr>")
    tbody = "".join(body_rows) or f'<tr><td colspan="{len(cols)}" class="empty">no rows</td></tr>'

    err = ""
    if _last_error["msg"] and time.time() - _last_error["at"] < 30:
        err = f'<div class="err">ssh/db error, showing last-known-good: {html.escape(_last_error["msg"])}</div>'

    return (
        f'{err}'
        f'<div class="meta">{len(rows)} rows &middot; {html.escape(view["query"])} '
        f'&middot; refreshed {time.strftime("%H:%M:%S")}</div>'
        f'<div class="table-scroll"><table><thead><tr>{thead}</tr></thead>'
        f'<tbody>{tbody}</tbody></table></div>'
    )


PAGE = """<!doctype html>
<html><head><meta charset="utf-8">
<title>Post fleet viewer</title>
<style>
  :root{{
    color-scheme: light dark;
    --bg:#eef1f4; --surface:#fff; --border:#d3dae1; --text:#161d24;
    --dim:#5c6b78; --accent:#0d7d8f;
    --good:#1c8a5c; --good-bg:#e0f3e8; --warn:#a8690b; --warn-bg:#faedd8;
    --bad:#b6432a; --bad-bg:#fbe6e0; --dimbg:#e7eaed;
  }}
  @media (prefers-color-scheme: dark){{
    :root{{
      --bg:#0f1418; --surface:#161d23; --border:#2b353e; --text:#e8edf1;
      --dim:#9aa8b3; --accent:#54d3e0;
      --good:#4fd398; --good-bg:#123328; --warn:#f0b154; --warn-bg:#3a2c11;
      --bad:#f28468; --bad-bg:#3a1f18; --dimbg:#232c33;
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

  <form id="ctrl" class="controls">
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
  document.querySelectorAll('#ctrl input[type=checkbox]').forEach(cb => {{
    cb.addEventListener('change', refresh);
  }});
  let timer = null;
  function currentCols() {{
    return [...document.querySelectorAll('#ctrl input[type=checkbox]:checked')].map(c => c.value);
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
    fetch(`/fragment?${{params}}`)
      .then(r => r.text())
      .then(t => {{ document.getElementById('table-wrap').innerHTML = t; }})
      .catch(() => {{}});
    const url = new URL(location);
    url.search = params;
    history.replaceState(null, '', url);
  }}
  function setInterval_(ms) {{
    if (timer) clearInterval(timer);
    ms = parseInt(ms, 10);
    if (ms > 0) timer = setInterval(refresh, ms);
  }}
  setInterval_(document.getElementById('interval').value);
</script>
</body></html>"""


def render_page(view_key, selected_cols, sort_col=None, sort_dir="asc"):
    view_links = "".join(
        f'<a href="/?view={k}" class="{"active" if k == view_key else ""}">{html.escape(v["label"])}</a>'
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
    table_html = render_table(view_key, cols, sort_col, sort_dir)
    source = "this host (local)" if LOCAL_MODE else BANG_HOST
    return PAGE.format(
        view_links=view_links,
        col_checkboxes=col_boxes,
        table_html=table_html,
        view_json=json.dumps(view_key),
        sort_col_json=json.dumps(sort_col),
        sort_dir_json=json.dumps(sort_dir),
        source=html.escape(source),
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

        try:
            if parsed.path == "/fragment":
                body = render_table(view_key, selected_cols or VIEWS[view_key]["default"],
                                     sort_col, sort_dir)
                content_type = "text/html; charset=utf-8"
            else:
                body = render_page(view_key, selected_cols, sort_col, sort_dir)
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
