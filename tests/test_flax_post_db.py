"""flax_post.db builds a DSN defaulting to the read-only flax_post role."""


def test_dsn_defaults_to_flax_post_user(monkeypatch):
    for k in ("PGHOST", "PGPORT", "PGUSER", "PGPASSWORD", "PGDATABASE"):
        monkeypatch.delenv(k, raising=False)
    from flax_post import db
    dsn = db._dsn_from_env()
    assert "user=flax_post" in dsn
    assert "dbname=flax" in dsn
    assert "application_name=flax-post" in dsn


def test_dsn_honors_env_overrides(monkeypatch):
    for k in ("PGHOST", "PGPORT", "PGUSER", "PGPASSWORD", "PGDATABASE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("PGHOST", "10.0.0.5")
    monkeypatch.setenv("PGPASSWORD", "s3cret")
    from flax_post import db
    dsn = db._dsn_from_env()
    assert "host=10.0.0.5" in dsn
    assert "password=s3cret" in dsn
