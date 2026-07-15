"""Read-side I/O + pool for flax-discover. Mirrors flax_classify.db."""
from psycopg_pool import ConnectionPool


def build_pool(conninfo: str, min_size: int = 1, max_size: int = 5) -> ConnectionPool:
    pool = ConnectionPool(conninfo, min_size=min_size, max_size=max_size,
                          kwargs={"autocommit": True}, open=True)
    pool.wait()
    return pool


def read_observe_rows(pool: ConnectionPool) -> list[dict]:
    """observe_state snapshot: [{switch, port, resolved}]. resolved carries
    bmc_mac/nic_mac/product_name (product_name added in Task 1)."""
    with pool.connection() as conn:
        cur = conn.execute("SELECT switch, port, resolved FROM observe_state")
        return [{"switch": s, "port": p, "resolved": r or {}}
                for s, p, r in cur.fetchall()]


def read_switch_facts(pool: ConnectionPool) -> dict:
    """{switch: {"ports": {port: {...}}}} -- for per-port junk_macs."""
    with pool.connection() as conn:
        cur = conn.execute("SELECT switch, ports FROM switch_facts")
        return {s: {"ports": p} for s, p in cur.fetchall()}


def read_devices(pool: ConnectionPool) -> list[dict]:
    """Existing devices snapshot: [{mac, switch, port, kind, latched}]."""
    with pool.connection() as conn:
        cur = conn.execute("SELECT mac, switch, port, kind, latched FROM devices")
        return [{"mac": m, "switch": s, "port": p, "kind": k, "latched": la or {}}
                for m, s, p, k, la in cur.fetchall()]
