"""Postgres connection pool for flax-post (read-only, source='post' devices)."""
import os

from psycopg_pool import ConnectionPool

_pool = None


def _dsn_from_env() -> str:
    return " ".join([
        f"host={os.environ.get('PGHOST', 'localhost')}",
        f"port={os.environ.get('PGPORT', '5432')}",
        f"user={os.environ.get('PGUSER', 'flax_post')}",
        f"password={os.environ.get('PGPASSWORD', '')}",
        f"dbname={os.environ.get('PGDATABASE', 'flax')}",
        "application_name=flax-post",
    ])


def get_pool() -> ConnectionPool:
    """Return the process-wide ConnectionPool, creating it on first call."""
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            _dsn_from_env(),
            min_size=1,
            max_size=5,
            timeout=5.0,
            kwargs={"autocommit": True},
        )
    return _pool
