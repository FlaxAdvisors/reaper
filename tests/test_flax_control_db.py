"""Unit tests for the Postgres connection pool wrapper."""
import os
from unittest.mock import patch, MagicMock

import pytest


def test_get_pool_returns_singleton():
    """Repeated calls return the same pool instance."""
    from flax_control import db

    with patch("flax_control.db.ConnectionPool") as mock_pool_cls:
        mock_pool_cls.return_value = MagicMock()
        db._pool = None  # reset singleton for the test
        a = db.get_pool()
        b = db.get_pool()
        assert a is b
        assert mock_pool_cls.call_count == 1


def test_get_pool_uses_env_for_dsn():
    """DSN is composed from PG* env vars."""
    from flax_control import db

    with patch.dict(os.environ, {
        "PGHOST": "localhost",
        "PGPORT": "5432",
        "PGUSER": "flax_control",
        "PGPASSWORD": "secret",
        "PGDATABASE": "flax",
    }, clear=False), patch("flax_control.db.ConnectionPool") as mock_pool_cls:
        db._pool = None
        db.get_pool()
        args, kwargs = mock_pool_cls.call_args
        dsn = args[0] if args else kwargs["conninfo"]
        assert "host=localhost" in dsn
        assert "user=flax_control" in dsn
        assert "dbname=flax" in dsn
