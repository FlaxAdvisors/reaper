"""Unit tests for the switch-sense Postgres wrapper."""
import os
from unittest.mock import patch, MagicMock


def test_get_pool_returns_singleton():
    from flax_switch_sense import db
    with patch("flax_switch_sense.db.ConnectionPool") as mock_pool_cls:
        mock_pool_cls.return_value = MagicMock()
        db._pool = None
        a = db.get_pool()
        b = db.get_pool()
        assert a is b
        assert mock_pool_cls.call_count == 1


def test_get_pool_uses_env_for_dsn():
    from flax_switch_sense import db
    with patch.dict(os.environ, {
        "PGHOST": "localhost", "PGPORT": "5432",
        "PGUSER": "flax_switch_sense", "PGPASSWORD": "secret",
        "PGDATABASE": "flax",
    }, clear=False), patch("flax_switch_sense.db.ConnectionPool") as mock_pool_cls:
        db._pool = None
        db.get_pool()
        args, kwargs = mock_pool_cls.call_args
        dsn = args[0] if args else kwargs["conninfo"]
        assert "user=flax_switch_sense" in dsn
        assert "dbname=flax" in dsn
        assert "application_name=flax-switch-sense" in dsn
