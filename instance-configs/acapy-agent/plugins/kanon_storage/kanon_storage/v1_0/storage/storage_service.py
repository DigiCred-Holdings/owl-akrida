"""KanonStorage — implements ``acapy_agent.storage.base.BaseStorage``."""

from __future__ import annotations

import logging
from typing import Mapping, Optional, Sequence

from acapy_agent.storage.base import BaseStorage, BaseStorageSearch, DEFAULT_PAGE_SIZE
from acapy_agent.storage.error import StorageNotFoundError
from acapy_agent.storage.record import StorageRecord
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kanon_storage.v1_0.storage.base_adapter import BaseRecordAdapter
from kanon_storage.v1_0.storage.errors import RecordNotFoundError
from kanon_storage.v1_0.storage.generic_adapter import GenericRecordAdapter

LOGGER = logging.getLogger(__name__)

# Default cap on `find_all_records` so a WQL match across a multi-tenant
# table can't blow the heap. Callers wanting unbounded iteration must
# pass `options={"limit": None}` explicitly. 10_000 is well above any
# typical admin-route page but small enough to fit in memory.
_FIND_ALL_DEFAULT_LIMIT = 10_000


class KanonStorage(BaseStorage, BaseStorageSearch):
    """Storage backend dispatching to per-record adapters over SQLAlchemy.

    Two construction styles:
      * `KanonStorage(profile_ref)` — what `ClassProvider` invokes; takes
        a Profile (or weakref to one) and pulls config from it. Used
        when ACA-Py binds via `injector.bind_provider(BaseStorage, ClassProvider(...))`.
      * `KanonStorage(profile_id=..., dialect=..., session_factory=..., active_session=...)`
        — keyword form for our session-bound BaseStorage where we share
        the active SQLAlchemy session with the wallet.
    """

    def __init__(
        self,
        profile_or_ref=None,
        *,
        profile_id: Optional[str] = None,
        dialect: Optional[str] = None,
        session_factory: Optional[async_sessionmaker[AsyncSession]] = None,
        typed_adapters: Sequence[type[BaseRecordAdapter]] = (),
        active_session: Optional[AsyncSession] = None,
    ):
        if profile_or_ref is not None:
            profile = profile_or_ref() if callable(profile_or_ref) else profile_or_ref
            if profile is None:
                raise RuntimeError(
                    "KanonStorageProfile has been garbage-collected"
                )
            # Defer import to avoid a circular at module load (profile ->
            # storage_service -> profile).
            from kanon_storage.v1_0.profile.profile import KanonStorageProfile

            if not isinstance(profile, KanonStorageProfile):
                raise TypeError(
                    "KanonStorage expected a KanonStorageProfile (or weakref "
                    f"to one); got {type(profile).__name__}"
                )
            profile_id = profile.profile_id
            dialect = profile.config.dialect
            session_factory = profile.session_factory

        if profile_id is None or dialect is None or session_factory is None:
            raise TypeError(
                "KanonStorage requires either a Profile arg or "
                "profile_id/dialect/session_factory kwargs"
            )

        self._profile_id = profile_id
        self._dialect = dialect
        self._session_factory = session_factory
        self._active_session = active_session
        self._adapters_by_type: dict[str, BaseRecordAdapter] = {}
        for cls in typed_adapters:
            inst = cls(profile_id=profile_id, dialect=dialect)
            for rt in cls.record_types:
                if rt == "*":
                    continue
                self._adapters_by_type[rt] = inst
        self._generic = GenericRecordAdapter(profile_id=profile_id, dialect=dialect)

    def _adapter_for(self, record_type: str) -> BaseRecordAdapter:
        return self._adapters_by_type.get(record_type, self._generic)

    def _session_cm(self):
        """Return an async-context-manager that yields a session.

        If a transaction-bound session was injected (the profile is in a
        transaction), reuse it without opening a new one. Otherwise begin a
        short-lived transaction via the factory.
        """
        if self._active_session is not None:
            return _NoopSessCM(self._active_session)
        return self._session_factory.begin()

    async def add_record(self, record: StorageRecord):
        async with self._session_cm() as sess:
            await self._adapter_for(record.type).add(sess, record)

    async def get_record(
        self,
        record_type: str,
        record_id: str,
        options: Optional[Mapping] = None,
    ) -> StorageRecord:
        for_update = bool(options and options.get("forUpdate"))
        async with self._session_cm() as sess:
            adapter = self._adapter_for(record_type)
            try:
                return await adapter.get(
                    sess, record_type, record_id, for_update=for_update
                )
            except RecordNotFoundError:
                raise StorageNotFoundError(
                    f"{record_type} record id={record_id!r} not found"
                )

    async def update_record(
        self, record: StorageRecord, value: str, tags: Mapping
    ):
        async with self._session_cm() as sess:
            adapter = self._adapter_for(record.type)
            await adapter.update(sess, record, value, dict(tags) if tags else {})

    async def delete_record(self, record: StorageRecord):
        async with self._session_cm() as sess:
            adapter = self._adapter_for(record.type)
            await adapter.delete(sess, record)

    async def find_paginated_records(
        self,
        type_filter: str,
        tag_query: Optional[Mapping] = None,
        limit: int = DEFAULT_PAGE_SIZE,
        offset: int = 0,
        order_by: Optional[str] = None,
        descending: bool = False,
    ) -> Sequence[StorageRecord]:
        async with self._session_cm() as sess:
            adapter = self._adapter_for(type_filter)
            results = await adapter.find_all(
                sess,
                type_filter,
                dict(tag_query) if tag_query else None,
                limit=limit,
                offset=offset,
                order_by=order_by,
                descending=descending,
            )
        return results

    async def find_all_records(
        self,
        type_filter: str,
        tag_query: Optional[Mapping] = None,
        order_by: Optional[str] = None,
        descending: bool = False,
        options: Optional[Mapping] = None,
    ) -> Sequence[StorageRecord]:
        # ACA-Py BaseStorage callers pass ordering either as positional kwargs
        # or via the options mapping (e.g. {"order_by": "id", "descending": True}).
        # Honor both.
        limit: Optional[int] = _FIND_ALL_DEFAULT_LIMIT
        if options:
            if order_by is None and options.get("order_by") is not None:
                order_by = options.get("order_by")
            if not descending and options.get("descending"):
                descending = bool(options.get("descending"))
            # Default to a cap; callers that genuinely want all rows must
            # opt in by passing `limit=None` explicitly.
            if "limit" in options:
                limit = options.get("limit")
        async with self._session_cm() as sess:
            adapter = self._adapter_for(type_filter)
            results = await adapter.find_all(
                sess,
                type_filter,
                dict(tag_query) if tag_query else None,
                limit=limit,
                order_by=order_by,
                descending=descending,
            )
        if limit is not None and len(results) >= limit:
            LOGGER.warning(
                "find_all_records hit the default cap of %d rows for "
                "type=%r. Pass options={'limit': None} to opt into "
                "unbounded scans, or use search_records for paging.",
                limit,
                type_filter,
            )
        return results

    async def count_records(
        self,
        type_filter: str,
        tag_query: Optional[Mapping] = None,
    ) -> int:
        """Return the total row count for a type + WQL tag filter.

        The `count_records` extension consumed by workflow_protocol's
        `count_instances` for numeric page navigation; absent on stock
        ACA-Py storages, present here on KanonStorage.
        """
        async with self._session_cm() as sess:
            adapter = self._adapter_for(type_filter)
            return await adapter.count(
                sess, type_filter, dict(tag_query) if tag_query else None
            )

    async def delete_all_records(
        self,
        type_filter: str,
        tag_query: Optional[Mapping] = None,
    ) -> None:
        # Atomic single-statement DELETE built from the WQL filter.
        # Mirrors acapy_agent/storage/kanon_storage.py:_delete_all_records,
        # which calls remove_all in a single round-trip.
        async with self._session_cm() as sess:
            adapter = self._adapter_for(type_filter)
            await adapter.delete_all(
                sess, type_filter, dict(tag_query) if tag_query else None
            )

    def search_records(
        self,
        type_filter: str,
        tag_query: Optional[Mapping] = None,
        page_size: Optional[int] = None,
        options: Optional[Mapping] = None,
    ):
        """Return a search session that pages through results.

        Implementation defers to find_paginated_records under the hood.
        """
        return _KanonSearchSession(
            storage=self,
            type_filter=type_filter,
            tag_query=tag_query,
            page_size=page_size or DEFAULT_PAGE_SIZE,
        )


class _NoopSessCM:
    """Yield a caller-owned session, isolating failures from the outer tx.

    Why: on asyncpg, a probe that raises StorageNotFoundError leaves the
    outer transaction in `in-failed-transaction` state — every subsequent
    write silently no-ops. We wrap the inner op in a SAVEPOINT so the
    outer tx survives.

    SQLite is skipped intentionally: pysqlite issues SAVEPOINT before
    SQLAlchemy's deferred BEGIN reaches the driver, sqlite auto-opens
    an implicit tx, the matching RELEASE commits it, and a later
    outer rollback has nothing to undo.
    """

    def __init__(self, sess: AsyncSession):
        self.sess = sess
        self._savepoint = None

    async def __aenter__(self) -> AsyncSession:
        if self.sess.in_transaction() and self._dialect_supports_savepoint():
            self._savepoint = await self.sess.begin_nested()
        return self.sess

    def _dialect_supports_savepoint(self) -> bool:
        bind = self.sess.get_bind() if hasattr(self.sess, "get_bind") else None
        dialect_name = getattr(getattr(bind, "dialect", None), "name", "")
        return dialect_name != "sqlite"

    async def __aexit__(self, exc_type, exc, tb):
        if self._savepoint is None:
            return False
        if exc_type is not None:
            try:
                await self._savepoint.rollback()
            except Exception:
                LOGGER.exception(
                    "KanonStorage savepoint rollback failed after %s",
                    exc_type.__name__,
                )
            return False
        try:
            await self._savepoint.commit()
        except Exception:
            LOGGER.exception("KanonStorage savepoint commit failed")
            raise
        return False


class _KanonSearchSession:
    """Paged search over KanonStorage. Cursor-style fetch(N).

    Exposes `_done` (matching the askar search session attribute name)
    so ACA-Py's upgrade flow can iterate until the cursor is exhausted.
    """

    def __init__(
        self,
        *,
        storage: KanonStorage,
        type_filter: str,
        tag_query: Optional[Mapping],
        page_size: int,
    ):
        self._storage = storage
        self._type_filter = type_filter
        self._tag_query = tag_query
        self._page_size = page_size
        self._offset = 0
        self._exhausted = False

    @property
    def _done(self) -> bool:
        return self._exhausted

    async def fetch(self, max_count: Optional[int] = None) -> Sequence[StorageRecord]:
        if self._exhausted:
            return []
        limit = max_count if max_count is not None else self._page_size
        # Treat 0 / negative as "use page_size" — the previous behaviour
        # silently marked the cursor exhausted on the first call when a
        # caller passed `max_count=0`, masking a real paging bug.
        if limit <= 0:
            limit = self._page_size
        results = await self._storage.find_paginated_records(
            self._type_filter, self._tag_query, limit=limit, offset=self._offset
        )
        # Stop condition: any fetch that returns fewer rows than the page
        # limit means the underlying scan is exhausted.
        if not results or len(results) < limit:
            self._exhausted = True
        self._offset += len(results)
        return results

    async def close(self):
        """Mark the iterator exhausted. No-op for resource release.

        This search session doesn't own a server-side cursor; each
        `fetch()` issues a fresh paginated SELECT. So `close()` just
        prevents further `fetch()` calls from issuing more queries — it
        does NOT release a DB cursor (because there isn't one).
        """
        self._exhausted = True
