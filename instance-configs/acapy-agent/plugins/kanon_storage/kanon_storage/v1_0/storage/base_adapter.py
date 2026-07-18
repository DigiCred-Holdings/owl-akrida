"""BaseRecordAdapter — the only layer that knows the schema of a record."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any, Awaitable, Callable, ClassVar, Generic, Sequence, TypeVar

from acapy_agent.storage.record import StorageRecord
from sqlalchemy import asc, delete, desc, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from kanon_storage.v1_0.storage.errors import (
    RecordNotFoundError,
    StorageError,
    translate_db_error,
)

LOGGER = logging.getLogger(__name__)

T = TypeVar("T")


def _warn_for_update_without_tx(sess: AsyncSession, *, where: str) -> None:
    """Log a warning when `for_update=True` is requested but no tx is open.

    SQLAlchemy + asyncpg will auto-begin and auto-commit, so the lock is
    released immediately after the SELECT, giving the caller a false
    sense of serialisation. The fix is for the caller to wrap the
    read-modify-write in `async with profile.transaction(): ...`.
    """
    try:
        in_tx = sess.in_transaction()
    except Exception:  # pragma: no cover - defensive
        in_tx = False
    if not in_tx:
        LOGGER.warning(
            "%s requested for_update=True outside an active transaction; "
            "the row lock will be released immediately by autocommit. Wrap "
            "the call in `async with profile.transaction(): ...` for actual "
            "serialisation.",
            where,
        )


class BaseRecordAdapter(ABC, Generic[T]):
    """Abstract adapter for one or more StorageRecord types.

    Subclasses:
      * GenericRecordAdapter  — handles `record_type="*"` (catch-all)
      * Typed adapters per record_type for hot paths
    """

    record_types: ClassVar[Sequence[str]] = ()
    """The record_type strings this adapter handles. ('*',) means catch-all."""

    table_pg: ClassVar[type] = None
    table_sqlite: ClassVar[type] = None

    tag_key_mapping: ClassVar[dict[str, str]] = {}
    """Optional: maps a flat tag name (e.g. "auth_session") to a JSON path
    inside the value column (e.g. "presentation.auth_session"). Lets WQL
    queries reach into the value blob without unindexed JSON scans on tags.
    """

    def __init__(self, profile_id: str, dialect: str):
        if dialect not in ("postgresql", "sqlite"):
            raise ValueError(f"Unsupported dialect: {dialect!r}")
        self.profile_id = profile_id
        self.dialect = dialect

    @property
    def table(self) -> type:
        """Return the dialect-specific table class."""
        return self.table_pg if self.dialect == "postgresql" else self.table_sqlite

    @abstractmethod
    def get_values(self, record: StorageRecord) -> dict[str, Any]:
        """Map a StorageRecord into a dict of column values for this table.

        Must include `id`, `profile_id`, plus whatever the table requires.
        """

    @abstractmethod
    def to_record(self, row: Any) -> StorageRecord:
        """Reverse mapping: SQLAlchemy row instance -> StorageRecord."""

    async def add(self, sess: AsyncSession, record: StorageRecord) -> None:
        values = self.get_values(record)
        try:
            sess.add(self.table(**values))
            await sess.flush()
        except Exception as err:
            raise translate_db_error(err, record_type=record.type, record_id=record.id)

    async def get(
        self,
        sess: AsyncSession,
        record_type: str,
        record_id: str,
        *,
        for_update: bool = False,
    ) -> StorageRecord:
        stmt = select(self.table).where(
            self.table.id == record_id,
            self.table.profile_id == self.profile_id,
        )
        if hasattr(self.table, "record_type"):
            stmt = stmt.where(self.table.record_type == record_type)
        if for_update and self.dialect == "postgresql":
            _warn_for_update_without_tx(sess, where="BaseRecordAdapter.get")
            stmt = stmt.with_for_update(of=self.table, key_share=False)
        row = (await sess.execute(stmt)).scalar_one_or_none()
        if row is None:
            raise RecordNotFoundError(record_type=record_type, record_id=record_id)
        return self.to_record(row)

    async def update(
        self,
        sess: AsyncSession,
        record: StorageRecord,
        value: str,
        tags: dict[str, str],
    ) -> None:
        new_values = self.get_values(
            StorageRecord(record.type, value, tags, record.id)
        )
        new_values.pop("id", None)
        new_values.pop("profile_id", None)
        new_values.pop("created_at", None)
        stmt = (
            update(self.table)
            .where(
                self.table.id == record.id,
                self.table.profile_id == self.profile_id,
            )
            .values(**new_values)
        )
        if hasattr(self.table, "record_type"):
            stmt = stmt.where(self.table.record_type == record.type)
        result = await sess.execute(stmt)
        if result.rowcount == 0:
            raise RecordNotFoundError(record_type=record.type, record_id=record.id)

    async def delete(self, sess: AsyncSession, record: StorageRecord) -> None:
        stmt = delete(self.table).where(
            self.table.id == record.id,
            self.table.profile_id == self.profile_id,
        )
        if hasattr(self.table, "record_type"):
            stmt = stmt.where(self.table.record_type == record.type)
        result = await sess.execute(stmt)
        if result.rowcount == 0:
            raise RecordNotFoundError(record_type=record.type, record_id=record.id)

    def _build_filter_stmt(self, stmt, record_type: str, tag_query):
        """Apply profile/record_type + WQL tag filter to a select/delete stmt."""
        from kanon_storage.v1_0.storage.query import WqlToSqlAlchemy

        stmt = stmt.where(self.table.profile_id == self.profile_id)
        if hasattr(self.table, "record_type"):
            stmt = stmt.where(self.table.record_type == record_type)
        if tag_query:
            translator = WqlToSqlAlchemy(
                table=self.table,
                dialect=self.dialect,
                tag_column="tags" if hasattr(self.table, "tags") else None,
                custom_tags_column=(
                    "custom_tags" if hasattr(self.table, "custom_tags") else None
                ),
                tag_key_mapping=self.tag_key_mapping,
            )
            clause = translator(tag_query)
            if clause is not None:
                stmt = stmt.where(clause)
        return stmt

    async def find_all(
        self,
        sess: AsyncSession,
        record_type: str,
        tag_query: dict[str, Any] | None = None,
        *,
        limit: int | None = None,
        offset: int | None = None,
        order_by: str | None = None,
        descending: bool = False,
    ) -> list[StorageRecord]:
        stmt = select(self.table)
        stmt = self._build_filter_stmt(stmt, record_type, tag_query)
        # Match acapy_agent/storage/askar.py find_all_records semantics:
        # order_by names a column on the table; default direction asc.
        if order_by:
            col = getattr(self.table, order_by, None)
            if col is None:
                raise ValueError(
                    f"order_by={order_by!r} is not a column on "
                    f"{self.table.__tablename__}"
                )
            stmt = stmt.order_by(desc(col) if descending else asc(col))
        if limit is not None:
            stmt = stmt.limit(limit)
        if offset is not None:
            stmt = stmt.offset(offset)
        rows = (await sess.execute(stmt)).scalars().all()
        return [self.to_record(r) for r in rows]

    async def count(
        self,
        sess: AsyncSession,
        record_type: str,
        tag_query: dict[str, Any] | None = None,
    ) -> int:
        """Return the row count matching the same filter as `find_all`."""
        stmt = select(func.count()).select_from(self.table)
        stmt = self._build_filter_stmt(stmt, record_type, tag_query)
        result = await sess.execute(stmt)
        return int(result.scalar_one())

    async def delete_all(
        self,
        sess: AsyncSession,
        record_type: str,
        tag_query: dict[str, Any] | None = None,
    ) -> int:
        """Single-statement DELETE matching the WQL filter.

        Returns the number of rows deleted. Mirrors
        `acapy_agent/storage/kanon_storage.py:_delete_all_records` which
        issues a single `remove_all` call instead of paging.
        """
        stmt = delete(self.table)
        stmt = self._build_filter_stmt(stmt, record_type, tag_query)
        result = await sess.execute(stmt)
        return int(result.rowcount or 0)

    async def find_one(
        self,
        sess: AsyncSession,
        record_type: str,
        tag_query: dict[str, Any] | None = None,
    ) -> StorageRecord:
        results = await self.find_all(sess, record_type, tag_query, limit=2)
        if not results:
            raise RecordNotFoundError(record_type=record_type, record_id=None)
        if len(results) > 1:
            raise StorageError(
                f"More than one record found for type={record_type!r} "
                f"query={tag_query!r}"
            )
        return results[0]

    async def update_by_id_with_lock(
        self,
        sess_or_factory,
        record_type: str,
        record_id: str,
        callback: Callable[[StorageRecord], Awaitable[StorageRecord]],
    ) -> StorageRecord:
        """Drizzle-style: dialect-aware row-locked update.

        Postgres: opens a transaction, fetches with FOR NO KEY UPDATE,
        applies callback, writes, commits.
        SQLite: optimistic — fetch, apply, write. Caller must accept that
        a concurrent writer can stomp the update.

        `sess_or_factory` may be either an AsyncSession (caller controls tx)
        or an `async_sessionmaker` (we open and commit our own).
        """
        from sqlalchemy.ext.asyncio import async_sessionmaker

        if isinstance(sess_or_factory, async_sessionmaker):
            session_cm = sess_or_factory.begin()
        else:
            # Caller passed an existing session; assume already in tx.
            session_cm = _NoopAsyncCM(sess_or_factory)

        async with session_cm as sess:
            for_update = self.dialect == "postgresql"
            existing = await self.get(
                sess, record_type, record_id, for_update=for_update
            )
            updated = await callback(existing)
            await self.update(sess, updated, updated.value, updated.tags)
            return updated


class _NoopAsyncCM:
    """Async context manager that yields the existing session unchanged."""

    def __init__(self, sess):
        self.sess = sess

    async def __aenter__(self):
        return self.sess

    async def __aexit__(self, exc_type, exc, tb):
        return False
