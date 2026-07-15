"""Postgres connection pool for flax-switch-sense.

Same shape as flax_control/db.py -- singleton, lazy, env-driven. Duplicated
rather than imported because Python has no clean "shared between top-level
packages" pattern and we want each flax-* service to be self-contained.
"""
import os
from typing import Optional

from psycopg_pool import ConnectionPool

_pool: Optional[ConnectionPool] = None


def _dsn_from_env() -> str:
    return " ".join([
        f"host={os.environ.get('PGHOST', '127.0.0.1')}",
        f"port={os.environ.get('PGPORT', '5432')}",
        f"user={os.environ.get('PGUSER', 'flax_switch_sense')}",
        f"password={os.environ.get('PGPASSWORD', '')}",
        f"dbname={os.environ.get('PGDATABASE', 'flax')}",
        "application_name=flax-switch-sense",
    ])


def get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            _dsn_from_env(),
            min_size=1,
            max_size=4,
            timeout=5.0,
            kwargs={"autocommit": True},
        )
    return _pool


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None
