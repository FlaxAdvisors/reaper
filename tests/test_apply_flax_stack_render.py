# tests/test_apply_flax_stack_render.py
"""Render tests for apply_flax_stack Postgres HA templates (jinja2, no live PG).

Repo root is derived from __file__ so this runs in any worktree (unlike the
older render tests that hardcode a stale .worktrees path).
"""
import os
import jinja2

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TPL = f"{REPO}/roles/apply_flax_stack/templates"

PEERS = [
    {"name": "bang-fiesta", "ip": "192.168.88.21"},
    {"name": "bang-siesta", "ip": "192.168.88.22"},
]


def _render(name, ctx):
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(TPL),
        trim_blocks=True, lstrip_blocks=True, undefined=jinja2.StrictUndefined)
    return env.get_template(name).render(**ctx)


# --- pg_hba.conf.j2 ---------------------------------------------------------

def test_pg_hba_emits_peer_replication_line_excluding_self():
    """On fiesta: allow siesta (.22) as replicator; never a self line (.21)."""
    out = _render("pg_hba.conf.j2",
                  dict(apply_flax_stack_peers=PEERS, inventory_hostname="bang-fiesta"))
    assert "host    replication  replicator  192.168.88.22/32  scram-sha-256" in out
    assert "192.168.88.21/32" not in out  # no self replication line


def test_pg_hba_siesta_allows_fiesta():
    out = _render("pg_hba.conf.j2",
                  dict(apply_flax_stack_peers=PEERS, inventory_hostname="bang-siesta"))
    assert "host    replication  replicator  192.168.88.21/32  scram-sha-256" in out
    assert "192.168.88.22/32" not in out


def test_pg_hba_standalone_has_no_replication_line():
    out = _render("pg_hba.conf.j2",
                  dict(apply_flax_stack_peers=[], inventory_hostname="bang-solo"))
    assert "host    replication" not in out  # no replication RULE (comment text is fine)
    # core service rules still present
    assert "host    flax      flax_control       127.0.0.1/32   scram-sha-256" in out
    assert "host    all       all                0.0.0.0/0      reject" in out


# --- postgresql.conf.j2 -----------------------------------------------------

def _pg_ctx(peers):
    return dict(apply_flax_stack_peers=peers, apply_flax_stack_postgres_port=5432)


def test_postgresql_listens_on_all_when_peers_set():
    out = _render("postgresql.conf.j2", _pg_ctx(PEERS))
    # '*' (all interfaces, incl. localhost) — 'localhost,*' bound localhost-only
    # in postgres:16, so use the canonical '*'.
    assert "listen_addresses = '*'" in out


def test_postgresql_listens_localhost_only_when_standalone():
    out = _render("postgresql.conf.j2", _pg_ctx([]))
    assert "listen_addresses = 'localhost'" in out


def test_bind_file_placeholders_cover_every_compose_mount():
    """Every single /etc/flax/*.json the console bind-mounts gets a placeholder before the
    stack starts; a missing one becomes a docker-made empty directory on a fresh bang."""
    import re
    import yaml
    role = f"{REPO}/roles/apply_flax_stack"
    with open(f"{role}/files/docker-compose.yml") as f:
        mounted = set(re.findall(r"/etc/flax/([a-z_.-]+\.json)", f.read()))
    with open(f"{role}/defaults/main.yml") as f:
        listed = set(yaml.safe_load(f)["apply_flax_stack_bind_files"])
    assert mounted and mounted == listed
