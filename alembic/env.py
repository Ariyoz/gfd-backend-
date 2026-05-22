"""Alembic migration environment."""

from logging.config import fileConfig
from sqlalchemy import pool, create_engine
from alembic import context

from app.config import get_settings
from app.database import Base
from app.models import *  # noqa: Import all models for autogenerate

config = context.config
settings = get_settings()

# Convert async URL to sync for Alembic
sync_url = settings.DATABASE_URL.replace("+asyncpg", "")

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(url=sync_url, target_metadata=target_metadata, literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = create_engine(sync_url, poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
