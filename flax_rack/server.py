"""flax-rackpost: one stdlib HTTP daemon serving every enabled rack order dir.

A dir under the orders root is served when it holds geometry.json AND .rackpost.
Its URL prefix is the geometry network.prefix without the trailing '-' (rr101-,
rr101b-). Two dirs claiming the same prefix, or the same (order, rack), are BOTH
refused and listed as conflicts on /rackpost/ -- never served by a guess. So are
two dirs on the same vid (each rack owns its 172.<vid>.0.0/16).

geometry.json is the hand-corrected authority: a request for a rack whose file
mtime differs from the one its Geometry was parsed at rescans, and the rack
adopts the freshly parsed geometry (or stops being served if it no longer
parses or now conflicts). Nothing is adopted from an older scan.

Stages run in daemon threads under one lock per rack dir (RackLock: a thread lock
plus a flock on <dir>/.stage.lock, so a CLI `discover` and the daemon exclude each
other too). Different racks run in parallel. A stage's exception lands in that
order's event log; it never takes the server down.

The bang host has no FastAPI: stdlib only (http.server, json, threading, sqlite).
"""
import fcntl
import html
import json
import mimetypes
import os
import re
import sqlite3
import threading
import urllib.parse
from dataclasses import asdict, dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .addressing import plan_addresses
from .geometry import Geometry, load_geometry
from .kea_client import KeaError
from .orderdb import OrderDB
from .stages import close_order, run_discover
from .version import __version__

PHASES = ("discover", "firmware", "qualify", "deploy", "done")
DISCOVER_RUNGS = ("switch-fdb", "slot-match", "reserve", "bmc-reach")
# v1 hand-written profile names (spec 5.4); a geometry `pop` or an override may add others
PROFILES = ("f4-compute-tp", "f4-bc-head-tp", "f4-bc-head-25g", "f4-bc-jbod", "ovh-bare-tp")
_PROFILE_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
_PREFIX_RE = re.compile(r"[a-z]{2}[0-9]{3}[a-z]?")      # the nginx location regex
_STATIC_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_MAX_BODY = 64 * 1024

WEB = Path(__file__).resolve().parent / "web"
POST_STATIC = Path(__file__).resolve().parent.parent / "flax_post" / "web" / "static"
DEFAULT_STATIC = [WEB, POST_STATIC]


def pop_default(g: Geometry, d) -> str:
    """The BOM default: geometry `pop` if set, else <customer>-<kind> (f4-compute-tp)."""
    return d.pop or f"{g.customer.lower()}-{d.kind}"


def _fmt_mac(m):
    return ":".join(m[i:i + 2] for i in range(0, 12, 2)) if m else None


def _link_str(l) -> str:
    return f"{l.switch}:{l.port}" + (f"/{l.lane}" if l.lane else "")


# ---------------------------------------------------------------- registry
@dataclass
class Scan:
    racks: dict[str, Path]                       # url_prefix -> rack dir
    conflicts: list[dict]                        # {"key", "dirs", "error"}
    geoms: dict[Path, Geometry] = field(default_factory=dict)
    mtimes: dict[Path, int | None] = field(default_factory=dict)   # read BEFORE parsing


def _geo_mtime(d: Path):
    try:
        return (d / "geometry.json").stat().st_mtime_ns
    except OSError:
        return None


class Registry:
    def __init__(self, orders_root: Path):
        self.root = Path(orders_root)

    def scan(self) -> Scan:
        conflicts, found, mtimes = [], [], {}
        try:
            dirs = sorted(p for p in self.root.iterdir() if p.is_dir())
        except OSError as e:
            return Scan({}, [{"key": str(self.root), "dirs": [], "error": str(e)}])
        for d in dirs:
            if not ((d / "geometry.json").is_file() and (d / ".rackpost").exists()):
                continue
            mtimes[d] = _geo_mtime(d)
            try:
                g = load_geometry(d / "geometry.json")
                plan_addresses(g)                # a colliding plan is not servable
            except Exception as e:               # noqa: BLE001 -- any bad file is a conflict row
                conflicts.append({"key": d.name, "dirs": [str(d)],
                                  "error": f"geometry: {type(e).__name__}: {e}"})
                continue
            if not _PREFIX_RE.fullmatch(g.url_prefix):
                conflicts.append({"key": d.name, "dirs": [str(d)],
                                  "error": f"prefix {g.prefix!r} is not <2 letters><3 digits>[tag]-"})
                continue
            found.append((d, g))
        bad: set[Path] = set()
        keys = (("prefix", lambda g: g.url_prefix),
                ("order", lambda g: g.order + (f"/{g.rack}" if g.rack else "")),
                ("vid", lambda g: str(g.vid)))
        for label, keyf in keys:
            by: dict[str, list[Path]] = {}
            for d, g in found:
                by.setdefault(keyf(g), []).append(d)
            for k, ds in by.items():
                if len(ds) > 1:
                    conflicts.append({"key": f"{label} {k}", "dirs": [str(x) for x in ds],
                                      "error": "claimed by more than one dir; none served"})
                    bad.update(ds)
        ok = [(d, g) for d, g in found if d not in bad]
        return Scan({g.url_prefix: d for d, g in ok}, conflicts, {d: g for d, g in ok},
                    {d: mtimes[d] for d, _ in ok})


# ---------------------------------------------------------------- per-rack state
class RackLock:
    """Non-blocking stage lock for one rack dir: in-process lock + flock across processes."""

    def __init__(self, rack_dir: Path):
        self.path = Path(rack_dir) / ".stage.lock"
        self._t = threading.Lock()
        self._fd = None

    def acquire(self) -> bool:
        if not self._t.acquire(blocking=False):
            return False
        try:
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                os.close(fd)
                raise
        except OSError:
            self._t.release()
            return False
        self._fd = fd
        return True

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is not None:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        self._t.release()

    def locked(self) -> bool:
        return self._t.locked()


class Rack:
    def __init__(self, rack_dir: Path, g: Geometry, mtime=None):
        self.dir = rack_dir
        self.g = g
        self.db = OrderDB(rack_dir / "order.db")
        self.lock = RackLock(rack_dir)
        self.mtime = mtime
        self.db.init(g)
        # a 'running' row left by a previous daemon is interrupted -- unless another
        # process (a CLI discover) holds the stage lock right now
        if self.lock.acquire():
            try:
                n = self.db.mark_interrupted()
                if n:
                    self.db.add_event("restart", f"{n} rung(s) interrupted by a daemon restart")
            finally:
                self.lock.release()

    def adopt(self, g: Geometry, mtime) -> None:
        """Take a geometry parsed from the file at `mtime` (a stage already running
        keeps the Geometry it started with)."""
        self.g, self.mtime = g, mtime
        self.db.init(g)


def _seen(d, slot, mgmt_ids) -> dict:
    """Per link: what Discover saw. Only the mgmt-switch links are checked in Plan 1."""
    out = {}
    for name, l in d.links.items():
        if l.switch not in mgmt_ids:
            out[name] = {"state": "", "text": ""}
        elif slot is None:
            out[name] = {"state": "", "text": "not run"}
        elif slot["status"] != "done":
            out[name] = {"state": "", "text": slot["status"]}
        elif slot["verdict"] == "pass":
            det = slot["detail"] or {}
            if name.startswith("iom"):
                i = int(name[3:]) - 1
                ioms = det.get("iom_macs") or []
                text = _fmt_mac(ioms[i]) if i < len(ioms) else ""
            else:
                text = "host+bmc"
            out[name] = {"state": "ok", "text": text}
        else:
            det = slot["detail"] or {}
            out[name] = {"state": "bad",
                         "text": " ".join(x for x in (det.get("status"), det.get("note")) if x)
                         or "fail"}
    return out


def _trim(run: dict) -> dict:
    r = dict(run)
    if isinstance(r.get("detail"), dict) and "fdb" in r["detail"]:
        r["detail"] = {k: v for k, v in r["detail"].items() if k != "fdb"}
    return r


def rack_state(r: Rack) -> dict:
    g, db = r.g, r.db
    by_pos: dict[str, dict] = {}
    for (pos, rung), v in db.latest_runs().items():
        by_pos.setdefault(pos, {})[rung] = _trim(v)
    dbdev = {x["pos"]: x for x in db.devices()}
    addrs: dict[str, dict] = {}
    names: dict[str, dict] = {}
    for a in plan_addresses(g):
        addrs.setdefault(a.pos, {})[a.role] = a.ip
        names.setdefault(a.pos, {})[a.role] = a.name
    mgmt = {s.id for s in g.switches if s.role == "mgmt"}
    profiles = set(PROFILES)
    devices = []
    for d in g.devices:
        x = dbdev.get(d.pos, {})
        default = pop_default(g, d)
        ovr = x.get("pop_override")
        profiles.update(p for p in (default, ovr) if p)
        runs = by_pos.get(d.pos, {})
        devices.append({
            "pos": d.pos, "ou": d.ou, "h": d.h, "col": d.col, "kind": d.kind, "part": d.part,
            "links": {k: _link_str(l) for k, l in d.links.items()},
            "link_seen": _seen(d, runs.get("slot-match"), mgmt),
            "pop": ovr or default, "pop_default": default, "pop_overridden": bool(ovr),
            "host_mac": _fmt_mac(x.get("host_mac")), "bmc_mac": _fmt_mac(x.get("bmc_mac")),
            "iom_macs": [_fmt_mac(m) for m in x.get("iom_macs") or []],
            "addrs": addrs.get(d.pos, {}), "names": names.get(d.pos, {}),
            "runs": runs})
    rack_runs = by_pos.get("_rack", {})
    fdb = rack_runs.get("switch-fdb")
    switches = []
    for s in g.switches:
        sw = asdict(s)
        sw["fdb_count"] = ((fdb or {}).get("detail") or {}).get("count") \
            if s.role == "mgmt" and s.mgmt_ip else None
        switches.append(sw)
    return {
        "order": g.order, "customer": g.customer, "vid": g.vid, "site": g.site, "rack": g.rack,
        "prefix": g.url_prefix, "version": __version__, "closed_at": db.closed_at(),
        "stage_running": r.lock.locked(), "phases": list(PHASES),
        "discover_rungs": list(DISCOVER_RUNGS), "profiles": sorted(profiles),
        "switches": switches, "devices": devices, "rack_runs": rack_runs,
        "events": db.events(30)}


# ---------------------------------------------------------------- app
class App:
    def __init__(self, orders_root: Path, static_dirs, context_factory):
        self.registry = Registry(orders_root)
        self.static_dirs = [Path(p).resolve() for p in static_dirs]
        self.factory = context_factory
        self._mu = threading.Lock()
        self._scan = Scan({}, [])
        self._racks: dict[Path, Rack] = {}

    def rescan(self) -> Scan:
        s = self.registry.scan()
        with self._mu:
            self._scan = s
        return s

    def rack(self, prefix: str) -> Rack | None:
        s = self._scan
        d = s.racks.get(prefix)
        if d is None or not ((d / ".rackpost").exists() and (d / "geometry.json").is_file()) \
                or _geo_mtime(d) != s.mtimes.get(d):
            # unknown, disabled, or geometry.json edited since that scan parsed it
            s = self.rescan()
            d = s.racks.get(prefix)
            if d is None:
                return None
        with self._mu:
            r = self._racks.get(d)
            if r is None:
                r = self._racks[d] = Rack(d, s.geoms[d], s.mtimes[d])
            elif r.mtime != s.mtimes[d]:
                r.adopt(s.geoms[d], s.mtimes[d])
        return r

    # -- actions --
    def start_discover(self, r: Rack):
        if r.db.closed_at():
            return 409, {"error": "order is closed"}
        if not r.lock.acquire():
            return 409, {"error": "a stage is already running for this rack"}
        g, db = r.g, r.db

        def work():
            try:
                run_discover(self.factory(r.dir, g, db))
            except Exception as e:           # noqa: BLE001 -- logged, never fatal
                try:
                    db.add_event("error", f"discover: {type(e).__name__}: {e}")
                except Exception:            # noqa: BLE001
                    pass
            finally:
                r.lock.release()

        try:
            db.add_event("discover", "discover started")
            threading.Thread(target=work, name=f"discover-{g.url_prefix}", daemon=True).start()
        except BaseException:
            r.lock.release()
            raise
        return 202, {"started": "discover"}

    def set_pop(self, r: Rack, body: dict):
        pos, profile = body.get("pos"), body.get("profile")
        try:
            d = r.g.device(pos)
        except KeyError:
            return 404, {"error": f"no device {pos!r}"}
        if profile is not None and not (isinstance(profile, str) and _PROFILE_RE.fullmatch(profile)):
            return 400, {"error": "profile must be null or a profile name"}
        if r.db.closed_at():
            return 409, {"error": "order is closed"}
        default = pop_default(r.g, d)
        if profile == default:
            profile = None
        r.db.set_pop(pos, profile)
        r.db.add_event("pop", f"{pos} pop -> {profile or 'default ' + default}")
        return 200, {"pos": pos, "pop": profile or default, "pop_overridden": bool(profile)}

    def close(self, r: Rack, body: dict):
        if body.get("confirm") != r.g.order:
            return 400, {"error": "type the order id to confirm"}
        if not r.lock.acquire():
            return 409, {"error": "a stage is running for this rack"}
        try:
            n = close_order(self.factory(r.dir, r.g, r.db))
        except KeaError as e:
            r.db.add_event("error", f"close: {e}")
            return 502, {"error": str(e)}
        except Exception as e:               # noqa: BLE001
            r.db.add_event("error", f"close: {type(e).__name__}: {e}")
            return 500, {"error": f"{type(e).__name__}: {e}"}
        finally:
            r.lock.release()
        return 200, {"closed_at": r.db.closed_at(), "released": n}

    # -- index --
    def index(self) -> dict:
        s = self.rescan()
        orders: dict[str, dict] = {}
        for prefix, d in sorted(s.racks.items()):
            g = s.geoms[d]
            o = orders.setdefault(g.order, {"order": g.order, "customer": g.customer, "racks": []})
            o["racks"].append({"rack": g.rack, "prefix": prefix, "url": f"/{prefix}/",
                               "vid": g.vid, "site": g.site, "dir": str(d),
                               "closed_at": _closed_at(d)})
        return {"version": __version__, "orders": [orders[k] for k in sorted(orders)],
                "conflicts": s.conflicts}

    def static_file(self, name: str) -> Path | None:
        if not _STATIC_RE.fullmatch(name) or ".." in name:
            return None
        for base in self.static_dirs:
            p = (base / name).resolve()
            if p.parent == base and p.is_file():
                return p
        return None


def _closed_at(d: Path):
    p = d / "order.db"
    if not p.exists():
        return None
    try:
        return OrderDB(p).closed_at()
    except sqlite3.Error:
        return None


def index_html(ix: dict) -> str:
    e = html.escape
    rows = []
    for o in ix["orders"]:
        rows.append(f'<div class="group"><div class="group-h" style="grid-template-columns:1fr">'
                    f'<span class="col-h" style="text-align:left">{e(o["order"])} · {e(o["customer"])}'
                    f'</span></div>')
        for r in o["racks"]:
            state = f'closed {e(r["closed_at"])}' if r["closed_at"] else "open"
            rows.append(f'<div class="row full"><span class="ou-label">{e(r["rack"] or "·")}</span>'
                        f'<a class="tile sw" href="{e(r["url"])}"><div class="t-head">'
                        f'<span class="t-port">{e(r["prefix"])}</span>'
                        f'<span class="kind">vid {r["vid"]} · {e(r["site"])}</span>'
                        f'<span class="watt {"unk" if r["closed_at"] else "on"}">{state}</span>'
                        f'</div></a></div>')
        rows.append("</div>")
    if not ix["orders"]:
        rows.append('<p class="pending-note">no enabled rack orders (geometry.json + .rackpost)</p>')
    if ix["conflicts"]:
        rows.append('<div class="section-l">conflicts (not served)</div>')
        for c in ix["conflicts"]:
            rows.append(f'<div class="t-row"><span class="v">{e(c["key"])}</span>'
                        f'<span title="{e(c.get("error", ""))}">{e(", ".join(c["dirs"]))}'
                        f' · {e(c.get("error", ""))}</span></div>')
    return ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            "<title>rackpost orders</title>"
            "<link rel=\"icon\" href=\"/rackpost/static/flax-logo.svg\" type=\"image/svg+xml\">"
            "<link rel=\"stylesheet\" href=\"/rackpost/static/rack.css\">"
            "<link rel=\"stylesheet\" href=\"/rackpost/static/rackpost.css\"></head><body>"
            "<div class=\"top\"><div class=\"toprow\"><div class=\"tl\">"
            "<img class=\"flogo\" src=\"/rackpost/static/flax-logo.svg\" alt=\"flax\"></div>"
            f"<div class=\"tr\"><span class=\"live-ver mono\">rackpost {e(ix['version'])}</span>"
            "<span class=\"title\">rack orders</span></div></div></div>"
            "<div class=\"rack index\">" + "".join(rows) + "</div></body></html>")


# ---------------------------------------------------------------- http
_CTYPES = {".js": "application/javascript", ".css": "text/css", ".svg": "image/svg+xml",
           ".html": "text/html; charset=utf-8"}


class _Handler(BaseHTTPRequestHandler):
    server_version = f"flax-rackpost/{__version__}"

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    # -- responses --
    def _send(self, code, body: bytes, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj).encode(), "application/json")

    def _redirect(self, loc):
        self.send_response(301)
        self.send_header("Location", loc)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _static(self, name):
        p = self.server.app.static_file(name)
        if p is None:
            return self._json(404, {"error": "not found"})
        ctype = _CTYPES.get(p.suffix) or mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        self._send(200, p.read_bytes(), ctype)

    def _body(self) -> dict | None:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None
        if not 0 <= n <= _MAX_BODY:
            return None
        raw = self.rfile.read(n) if n else b"{}"
        try:
            obj = json.loads(raw or b"{}")
        except ValueError:
            return None
        return obj if isinstance(obj, dict) else None

    # -- routing --
    def _dispatch(self, method):
        try:
            self._route(method, urllib.parse.urlsplit(self.path).path)
        except Exception as e:                   # noqa: BLE001 -- a request never kills the server
            try:
                self._json(500, {"error": f"{type(e).__name__}: {e}"})
            except Exception:                    # noqa: BLE001
                pass

    def _route(self, method, path):
        app: App = self.server.app
        parts = path.split("/")
        if len(parts) < 2 or not parts[1]:
            return self._redirect("/rackpost/")
        head, rest = parts[1], "/".join(parts[2:])
        if len(parts) == 2:
            return self._redirect(f"/{head}/")
        if head == "rackpost":
            if method == "GET" and rest == "":
                return self._send(200, index_html(app.index()).encode(), _CTYPES[".html"])
            if method == "GET" and rest == "api/racks":
                return self._json(200, app.index())
            if method == "GET" and rest.startswith("static/"):
                return self._static(rest[len("static/"):])
            return self._json(404, {"error": "not found"})
        if not _PREFIX_RE.fullmatch(head):
            return self._json(404, {"error": "not found"})
        r = app.rack(head)
        if r is None:
            return self._json(404, {"error": f"no enabled rack order {head!r}"})
        if method == "GET":
            if rest == "":
                return self._static("index.html")
            if rest.startswith("static/"):
                return self._static(rest[len("static/"):])
            if rest == "api/state":
                return self._json(200, rack_state(r))
            return self._json(404, {"error": "not found"})
        # CSRF: a cross-site form can only send a "simple" Content-Type; JSON
        # forces a preflight this server never answers
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            return self._json(415, {"error": "Content-Type must be application/json"})
        body = self._body()
        if body is None:
            return self._json(400, {"error": "body must be a JSON object"})
        if rest == "api/pop":
            return self._json(*app.set_pop(r, body))
        if rest == "api/stage/discover":
            return self._json(*app.start_discover(r))
        if rest == "api/close":
            return self._json(*app.close(r, body))
        return self._json(404, {"error": "not found"})


def default_context_factory(rack_dir: Path, g: Geometry, db: OrderDB):
    from .creds import load_creds
    from .kea_client import KeaClient
    from .stages import OrderContext
    # creds load lazily (first switch/BMC rung): Close needs Kea only
    return OrderContext(rack_dir, g, db, KeaClient(), load_creds)


def make_server(orders_root: Path, *, port=8450, bind="127.0.0.1", static_dirs=None,
                context_factory=default_context_factory) -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer((bind, port), _Handler)
    srv.daemon_threads = True
    srv.app = App(Path(orders_root), static_dirs or DEFAULT_STATIC, context_factory)
    return srv
