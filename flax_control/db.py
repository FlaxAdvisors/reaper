"""Postgres connection pool — singleton, lazily initialized.

Reads connection parameters from PG* env vars (libpq convention) so the
container's env file is the single source of truth and tests can monkey-patch.
"""
import os
from typing import Optional

from psycopg_pool import ConnectionPool

_pool: Optional[ConnectionPool] = None


def _dsn_from_env() -> str:
    return " ".join([
        f"host={os.environ.get('PGHOST', 'localhost')}",
        f"port={os.environ.get('PGPORT', '5432')}",
        f"user={os.environ.get('PGUSER', 'flax_control')}",
        f"password={os.environ.get('PGPASSWORD', '')}",  # empty → pool connect fails loudly at runtime
        f"dbname={os.environ.get('PGDATABASE', 'flax')}",
        "application_name=flax-control",
    ])


def get_pool() -> ConnectionPool:
    """Return the process-wide ConnectionPool, creating it on first call."""
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            _dsn_from_env(),
            min_size=2,
            max_size=10,
            timeout=5.0,
            kwargs={"autocommit": True},
        )
    return _pool


def close_pool() -> None:
    """Close the pool (for shutdown). Safe to call when not initialized."""
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None
