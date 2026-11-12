"""Test-suite root: repo on sys.path, and a private Postgres database per pytest run.

sys.path: test modules share helpers via `from tests.x import y` and import the
flax_* packages from this checkout, so the repo root goes first. tests/ has no
__init__.py; it resolves as a namespace package. Because tests live beside the
code, a branch or a worktree always tests its own code.

Per-run database: every pytest process gets its own database, built from THIS
tree's schema/ (alembic upgrade head) and dropped at exit, so parallel runs from
different worktrees never share state and a branch's new migration never leaks into
anyone else's run. The DB-backed tests take their connection from PGDATABASE /
FLAX_TEST_DSN / FLAX_TEST_ADMIN_DSN / FLAX_TEST_CONTROL_DSN, which are pointed at
the new database here (only `dbname=` is rewritten; user/host stay as given).
Unset ones default to this cluster: FLAX_TEST_DSN and FLAX_TEST_ADMIN_DSN as PGUSER
(superuser), FLAX_TEST_CONTROL_DSN as flax_control, FLAX_TEST_POST_DSN as flax_post
(the work-records writer), so a plain `python -m pytest` runs every DB test.

Safety: a database is only created on a cluster that carries the marker database
`flax_test_cluster`, i.e. one somebody deliberately set up as a disposable test
cluster. On any other cluster (a bang's live flax postgres) the DB settings are
cleared, so the DB-backed tests skip instead of connecting. Create the marker once
on a test cluster:  createdb -h 127.0.0.1 -U postgres flax_test_cluster

  FLAX_TEST_DB_KEEP=1   keep the run's database after the run (for inspection;
                        the next run on this host drops it once its pid is gone)
"""
import os
import re
import secrets
import socket
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

MARKER_DB = "flax_test_cluster"
RUN_PREFIX = "flax_test_run_"
DSN_VARS = ("FLAX_TEST_DSN", "FLAX_TEST_ADMIN_DSN", "FLAX_TEST_CONTROL_DSN", "FLAX_TEST_POST_DSN")
KEA_FIXTURE = ROOT / "tests" / "fixtures" / "db" / "kea_tables.sql"
_run = {}


def _admin_kw(dbname):
    return dict(host=os.environ.get("PGHOST", "127.0.0.1"), port=os.environ.get("PGPORT", "5432"),
                user=os.environ.get("PGUSER", "postgres"),
                password=os.environ.get("PGPASSWORD", ""), dbname=dbname,
                connect_timeout=3, autocommit=True)


def _disable(config, why):
    """No usable test cluster: make every DB-backed test skip rather than connect."""
    for v in DSN_VARS:
        os.environ.pop(v, None)
    os.environ["PGDATABASE"] = RUN_PREFIX + "unavailable"
    _run["note"] = "per-run test DB: none (%s); DB-backed tests will skip" % why


def _host_tag():
    return re.sub(r"[^a-z0-9]", "", socket.gethostname().split(".")[0].lower())[:20] or "host"


def _reap_stale(cur):
    """Drop run databases left by earlier runs on this host whose process is gone."""
    tag = _host_tag()
    cur.execute("SELECT datname FROM pg_database WHERE datname LIKE %s", (RUN_PREFIX + "%",))
    for (name,) in cur.fetchall():
        m = re.fullmatch(re.escape(RUN_PREFIX) + r"([a-z0-9]+)_(\d+)_[0-9a-f]+", name)
        if not m or m.group(1) != tag:
            continue
        try:
            os.kill(int(m.group(2)), 0)
            continue                      # that run is still going
        except ProcessLookupError:
            pass
        except PermissionError:
            continue
        cur.execute('DROP DATABASE IF EXISTS "%s" WITH (FORCE)' % name)


def _alembic(dbname, target):
    env = dict(os.environ, PGDATABASE=dbname, PGPASSWORD=os.environ.get("PGPASSWORD", ""))
    r = subprocess.run([sys.executable, "-m", "alembic", "upgrade", target], cwd=ROOT / "schema",
                       env=env, capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError("alembic upgrade %s failed:\n%s" % (target, r.stderr[-3000:]))


def _build(psycopg, dbname):
    _alembic(dbname, "001000000001")          # schemas (incl. kea) + service roles
    with psycopg.connect(**_admin_kw(dbname)) as c:
        c.execute(KEA_FIXTURE.read_text())
    _alembic(dbname, "head")
    with psycopg.connect(**_admin_kw(dbname)) as c:
        c.execute("CREATE TABLE public._retention_scratch_marker (note text)")
        c.execute("COMMENT ON TABLE public._retention_scratch_marker IS "
                  "'Per-run test database (conftest.py): proves to the destructive "
                  "retention tests that this is not a live cluster.'")


def pytest_configure(config):
    try:
        import psycopg
    except ImportError:
        _disable(config, "psycopg not installed")
        return
    try:
        admin = psycopg.connect(**_admin_kw("postgres"))
    except Exception as exc:                  # no cluster reachable: DB tests skip
        _disable(config, "no postgres at %s:%s (%s)" % (os.environ.get("PGHOST", "127.0.0.1"),
                                                       os.environ.get("PGPORT", "5432"),
                                                       str(exc).splitlines()[0][:80]))
        return
    with admin:
        cur = admin.cursor()
        cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (MARKER_DB,))
        if cur.fetchone() is None:
            _disable(config, "cluster has no %s marker database, so it is not a "
                             "disposable test cluster" % MARKER_DB)
            return
        _reap_stale(cur)
        name = "%s%s_%d_%s" % (RUN_PREFIX, _host_tag(), os.getpid(), secrets.token_hex(3))
        cur.execute('CREATE DATABASE "%s"' % name)
    try:
        _build(psycopg, name)
    except Exception:
        _drop(psycopg, name)
        raise
    _run.update(name=name, note="per-run test DB: %s (schema/ at head)" % name)
    os.environ["PGDATABASE"] = name
    kw = _admin_kw(name)
    base = "host=%s port=%s" % (kw["host"], kw["port"])
    defaults = {"FLAX_TEST_DSN": kw["user"], "FLAX_TEST_ADMIN_DSN": kw["user"],
                "FLAX_TEST_CONTROL_DSN": "flax_control", "FLAX_TEST_POST_DSN": "flax_post"}
    for v in DSN_VARS:
        dsn = os.environ.get(v) or "%s user=%s" % (base, defaults[v])
        os.environ[v] = (re.sub(r"\bdbname=\S+", "dbname=" + name, dsn) if "dbname=" in dsn
                         else dsn + " dbname=" + name)


def _drop(psycopg, name):
    with psycopg.connect(**_admin_kw("postgres")) as c:
        c.execute('DROP DATABASE IF EXISTS "%s" WITH (FORCE)' % name)


def pytest_report_header(config):
    return _run.get("note")


def pytest_unconfigure(config):
    name = _run.get("name")
    if name and os.environ.get("FLAX_TEST_DB_KEEP") != "1":
        import psycopg
        _drop(psycopg, name)
