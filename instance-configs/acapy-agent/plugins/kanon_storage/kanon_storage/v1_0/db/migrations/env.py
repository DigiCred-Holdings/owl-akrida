"""Alembic env.py."""

from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def _resolve_url() -> str:
    url = (
        os.environ.get("ACAPY_KS_DATABASE_URL")
        or os.environ.get("KANON_STORAGE_DATABASE_URL")
        or config.get_main_option("sqlalchemy.url")
    )
    if not url:
        raise RuntimeError(
            "kanon_storage alembic env: database URL not set. Provide "
            "ACAPY_KS_DATABASE_URL, KANON_STORAGE_DATABASE_URL, or "
            "`sqlalchemy.url` in alembic.ini."
        )
    return url


def _resolve_metadata():
    url = _resolve_url()
    if url.startswith("postgres"):
        from kanon_storage.v1_0.db.models import (  # noqa: F401
            base_pg,
            did_pg,
            generic_record_pg,
            key_pg,
            outbox_pg,
            storage_version_pg,
        )

        return base_pg.BasePgModel.metadata
    from kanon_storage.v1_0.db.models import (  # noqa: F401
        base_sqlite,
        did_sqlite,
        generic_record_sqlite,
        key_sqlite,
        outbox_sqlite,
        storage_version_sqlite,
    )

    return base_sqlite.BaseSqliteModel.metadata


target_metadata = _resolve_metadata()


VERSION_TABLE = "kanon_storage_alembic"


def run_migrations_offline() -> None:
    url = _resolve_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        version_table=VERSION_TABLE,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        version_table=VERSION_TABLE,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    config.set_main_option("sqlalchemy.url", _resolve_url())
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
