"""Outbox enqueue + replay + handler registry."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from kanon_storage.v1_0.profile.session import KanonStorageProfileSession

LOGGER = logging.getLogger(__name__)

_HANDLERS: dict[str, Callable[["OutboxOp", Any], Awaitable[None]]] = {}

MAX_ATTEMPTS = 5

STATUS_PENDING = "pending"
STATUS_IN_FLIGHT = "in_flight"
STATUS_DONE = "done"
STATUS_FAILED = "failed"


def register_handler(
    op_type: str, handler: Callable[["OutboxOp", Any], Awaitable[None]]
) -> None:
    """Register an async handler for an outbox op_type.

    The handler receives `(OutboxOp, profile)` and should be idempotent:
    a redelivery (after a crash) must not produce a second side-effect.
    """
    if op_type in _HANDLERS:
        LOGGER.warning("outbox: handler for %r already registered, replacing", op_type)
    _HANDLERS[op_type] = handler


@dataclass
class OutboxOp:
    id: str
    profile_id: str
    op_type: str
    payload: dict[str, Any]
    attempts: int
    created_at: datetime


class Outbox:
    """Per-profile outbox API."""

    def __init__(self, profile_id: str, dialect: str):
        self._profile_id = profile_id
        self._dialect = dialect

    def _table(self):
        if self._dialect == "postgresql":
            from kanon_storage.v1_0.db.models.outbox_pg import OutboxPg

            return OutboxPg
        from kanon_storage.v1_0.db.models.outbox_sqlite import OutboxSqlite

        return OutboxSqlite

    async def enqueue(
        self,
        session: AsyncSession | KanonStorageProfileSession,
        *,
        op_type: str,
        payload: dict[str, Any],
        op_id: Optional[str] = None,
    ) -> str:
        """Insert a pending outbox row.

        `session` may be either a raw SQLAlchemy AsyncSession (caller
        controls transaction) or a `KanonStorageProfileSession` whose
        `sa_session` we'll use. The row is written in whatever
        transaction the caller is in — commit/rollback semantics are
        owned by the caller.

        The whole point of an outbox is to live in the same tx as the
        business record that produced it. We reject calls that aren't
        inside an explicit tx so an enqueue can never be committed
        independently of the side-effect-causing record.
        """
        if isinstance(session, KanonStorageProfileSession):
            if not getattr(session, "is_transaction", False):
                raise RuntimeError(
                    "Outbox.enqueue requires a transactional "
                    "KanonStorageProfileSession (use "
                    "`async with profile.transaction(): ...`)."
                )
            sa = session.sa_session
        else:
            sa = session
            try:
                in_tx = sa.in_transaction()
            except Exception:
                in_tx = False
            if not in_tx:
                raise RuntimeError(
                    "Outbox.enqueue requires the AsyncSession to be inside "
                    "an active transaction so the outbox row commits "
                    "atomically with the business record."
                )
        op_id = op_id or str(uuid.uuid4())
        row = self._table()(
            id=op_id,
            profile_id=self._profile_id,
            op_type=op_type,
            payload=payload,
            status=STATUS_PENDING,
            attempts=0,
        )
        sa.add(row)
        await sa.flush()
        return op_id

    async def scan_pending(
        self, session: AsyncSession, *, limit: int = 100
    ) -> list[OutboxOp]:
        T = self._table()
        stmt = (
            select(T)
            .where(
                T.profile_id == self._profile_id,
                T.status == STATUS_PENDING,
            )
            .order_by(T.created_at)
            .limit(limit)
        )
        rows = (await session.execute(stmt)).scalars().all()
        return [
            OutboxOp(
                id=r.id,
                profile_id=r.profile_id,
                op_type=r.op_type,
                payload=r.payload,
                attempts=r.attempts,
                created_at=_coerce_dt(r.created_at),
            )
            for r in rows
        ]

    async def mark_done(self, session: AsyncSession, op_id: str) -> None:
        T = self._table()
        await session.execute(
            update(T)
            .where(T.id == op_id, T.profile_id == self._profile_id)
            .values(status=STATUS_DONE)
        )

    async def mark_failed(
        self, session: AsyncSession, op_id: str, error: str
    ) -> None:
        T = self._table()
        await session.execute(
            update(T)
            .where(T.id == op_id, T.profile_id == self._profile_id)
            .values(status=STATUS_FAILED, last_error=error[:2000])
        )

    async def increment_attempts(
        self, session: AsyncSession, op_id: str, error: str
    ) -> int:
        """Bump attempts. Returns the new attempts value."""
        T = self._table()
        # Read-modify-write — ok for our use; outbox isn't contended
        row = (
            await session.execute(
                select(T).where(
                    T.id == op_id, T.profile_id == self._profile_id
                )
            )
        ).scalar_one_or_none()
        if row is None:
            return 0
        row.attempts += 1
        row.last_error = error[:2000]
        await session.flush()
        return row.attempts

    async def claim_pending(
        self, session: AsyncSession, *, limit: int = 100
    ) -> list[OutboxOp]:
        """Atomically claim pending rows by flipping `pending` -> `in_flight`.

        Returns the ops that were claimed by *this* call. Concurrent
        replay workers (two replicas, replay-on-open vs replay-on-event)
        cannot both claim the same row because the UPDATE is atomic and
        only matches rows still in PENDING.

        Postgres path uses `FOR UPDATE SKIP LOCKED` so concurrent workers
        partition the queue. SQLite path does a scan-then-conditional-
        update, which is correct under SQLite's serialised write model.
        """
        T = self._table()

        if self._dialect == "postgresql":
            # Pull row ids under a row-lock with SKIP LOCKED so two
            # concurrent claimers partition the queue.
            inner_stmt = (
                select(T.id)
                .where(
                    T.profile_id == self._profile_id,
                    T.status == STATUS_PENDING,
                )
                .order_by(T.created_at)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
            inner_ids = (await session.execute(inner_stmt)).scalars().all()
            if not inner_ids:
                return []
            update_stmt = (
                update(T)
                .where(T.id.in_(list(inner_ids)))
                .values(status=STATUS_IN_FLIGHT)
                .returning(T)
            )
            rows = (await session.execute(update_stmt)).scalars().all()
        else:
            # SQLite: scan + conditional update guarded by status.
            scan = (
                select(T)
                .where(
                    T.profile_id == self._profile_id,
                    T.status == STATUS_PENDING,
                )
                .order_by(T.created_at)
                .limit(limit)
            )
            candidates = (await session.execute(scan)).scalars().all()
            rows = []
            for cand in candidates:
                upd = (
                    update(T)
                    .where(
                        T.id == cand.id,
                        T.profile_id == self._profile_id,
                        T.status == STATUS_PENDING,
                    )
                    .values(status=STATUS_IN_FLIGHT)
                )
                result = await session.execute(upd)
                if result.rowcount:
                    rows.append(cand)
        return [
            OutboxOp(
                id=r.id,
                profile_id=r.profile_id,
                op_type=r.op_type,
                payload=r.payload,
                attempts=r.attempts,
                created_at=_coerce_dt(r.created_at),
            )
            for r in rows
        ]

    async def replay_pending(self, profile) -> int:
        """Drain pending rows for this profile. Returns rows applied.

        Per-row workflow:
          1. Open a tx, atomically claim a batch by flipping
             `pending` -> `in_flight` (this is the idempotency guard:
             two concurrent replayers cannot both claim the same row).
          2. Run the handler outside the tx (handlers can be slow / do IO).
          3. Open another tx, mark `done` (or bump attempts and mark
             `failed` once MAX_ATTEMPTS is reached).

        Handlers MUST be idempotent: a process crash between (1) and (3)
        leaves the row in `in_flight`, which will be re-claimed by a
        future replay (operators should reap stale `in_flight` rows
        older than ~5 minutes back to `pending`; we don't auto-reap here
        to avoid stomping a long-running handler in another worker).
        """
        applied = 0

        # (1) Claim a batch under a single tx so concurrent replayers
        # don't double-fire handlers.
        async with profile.session_factory.begin() as claim_sess:
            ops = await self.claim_pending(claim_sess)

        for op in ops:
            handler = _HANDLERS.get(op.op_type)
            if handler is None:
                LOGGER.warning(
                    "outbox: no handler for op_type=%r (id=%s); skipping",
                    op.op_type,
                    op.id,
                )
                # Roll the claim back so a future deploy that registers
                # the handler can replay this op.
                async with profile.session_factory.begin() as sess:
                    await session_revert_to_pending(sess, self, op.id)
                continue

            try:
                await handler(op, profile)
                async with profile.session_factory.begin() as sess:
                    await self.mark_done(sess, op.id)
                applied += 1
                LOGGER.info(
                    "outbox: replayed op_type=%s id=%s", op.op_type, op.id
                )
            except Exception as err:
                LOGGER.error(
                    "outbox: handler raised for op_type=%s id=%s: %s",
                    op.op_type,
                    op.id,
                    err,
                )
                async with profile.session_factory.begin() as sess:
                    new_attempts = await self.increment_attempts(
                        sess, op.id, repr(err)
                    )
                    if new_attempts >= MAX_ATTEMPTS:
                        await self.mark_failed(
                            sess, op.id, f"max attempts reached: {err!r}"
                        )
                    else:
                        # Return to PENDING so the next replay cycle can
                        # retry; otherwise the row stays in_flight and
                        # only the stale-reaper would pick it up.
                        await session_revert_to_pending(sess, self, op.id)

        if applied or ops:
            LOGGER.info(
                "outbox: replay scan profile=%s pending=%d applied=%d",
                self._profile_id,
                len(ops),
                applied,
            )
        return applied


async def session_revert_to_pending(
    session: AsyncSession, outbox: "Outbox", op_id: str
) -> None:
    """Move an `in_flight` row back to `pending` so it gets retried."""
    T = outbox._table()
    await session.execute(
        update(T)
        .where(
            T.id == op_id,
            T.profile_id == outbox._profile_id,
            T.status == STATUS_IN_FLIGHT,
        )
        .values(status=STATUS_PENDING)
    )


def _coerce_dt(value: Any) -> datetime:
    """SQLite stores TEXT, Postgres stores TIMESTAMP. Normalize."""
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value)
    return datetime.now(timezone.utc)
