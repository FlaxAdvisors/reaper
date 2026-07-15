"""Alembic migration environment — uses psycopg, no SQLAlchemy models needed."""
import os
from logging.config import fileConfig

from alembic import context

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Build DSN from PG* env vars (libpq convention) and override the stub URL
# from alembic.ini. Building in Python avoids ConfigParser %(NAME)s interpolation
# which requires those keys be present in the same section.
url = (
    "postgresql+psycopg://"
    f"{os.environ.get('PGUSER', 'postgres')}:"
    f"{os.environ['PGPASSWORD']}@"
    f"{os.environ.get('PGHOST', '127.0.0.1')}:"
    f"{os.environ.get('PGPORT', '5432')}/"
    f"{os.environ.get('PGDATABASE', 'flax')}"
)
config.set_main_option("sqlalchemy.url", url)

target_metadata = None  # raw SQL migrations, not declarative models


def run_migrations_online() -> None:
    from sqlalchemy import engine_from_config, pool

    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


run_migrations_online()
