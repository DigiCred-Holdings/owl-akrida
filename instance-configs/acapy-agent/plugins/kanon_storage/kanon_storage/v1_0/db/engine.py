"""Async SQLAlchemy engine factory."""

from __future__ import annotations

import logging
from typing import Tuple

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from kanon_storage.v1_0.config import KanonStorageConfig

LOGGER = logging.getLogger(__name__)


def make_engine(cfg: KanonStorageConfig) -> AsyncEngine:
    """Build an AsyncEngine from config. Caller owns disposal."""
    kwargs: dict = {"echo": cfg.echo_sql, "future": True}

    if cfg.dialect == "postgresql":
        kwargs.update(
            pool_size=cfg.pool_size,
            max_overflow=cfg.pool_overflow,
            pool_timeout=cfg.pool_timeout_s,
            pool_pre_ping=True,
            # Recycle conns before the server / PgBouncer idle-timeout
            # closes them out from under us. pool_pre_ping detects dead
            # conns at checkout; pool_recycle prevents holding them.
            pool_recycle=cfg.pool_recycle_s,
        )
    else:
        # SQLite: no pre-ping; in-memory needs StaticPool to share state across sessions.
        if cfg.database_url.endswith(":memory:") or cfg.database_url == "sqlite+aiosqlite:///:memory:":
            from sqlalchemy.pool import StaticPool

            kwargs.update(
                connect_args={"check_same_thread": False},
                poolclass=StaticPool,
            )

    LOGGER.debug(
        "creating engine: dialect=%s url=%s pool=%d/+%d",
        cfg.dialect,
        _safe_url(cfg.database_url),
        cfg.pool_size,
        cfg.pool_overflow,
    )
    return create_async_engine(cfg.database_url, **kwargs)


def make_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Build the session factory. Sessions are AsyncSession with expire_on_commit=False."""
    return async_sessionmaker(bind=engine, expire_on_commit=False, class_=AsyncSession)


def make_engine_and_session_factory(
    cfg: KanonStorageConfig,
) -> Tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    """Convenience: returns (engine, session_factory)."""
    engine = make_engine(cfg)
    return engine, make_session_factory(engine)


def _safe_url(url: str) -> str:
    """Redact password from URL for log lines."""
    if "@" not in url or "://" not in url:
        return url
    head, tail = url.split("://", 1)
    if "@" in tail:
        creds, host = tail.split("@", 1)
        if ":" in creds:
            user, _ = creds.split(":", 1)
            creds = f"{user}:***"
        return f"{head}://{creds}@{host}"
    return url
