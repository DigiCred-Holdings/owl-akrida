"""Encrypted-at-rest key store."""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

from acapy_agent.wallet.error import WalletDuplicateError, WalletNotFoundError
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

LOGGER = logging.getLogger(__name__)


def _warn_for_update_without_tx(sess: AsyncSession, *, where: str) -> None:
    """Log a warning when `for_update=True` is requested without an active tx.

    Otherwise the lock is released by the next autocommit, giving the
    caller a false sense of serialisation. Callers should wrap the
    read-modify-write in `async with profile.transaction(): ...`.
    """
    try:
        in_tx = sess.in_transaction()
    except Exception:
        in_tx = False
    if not in_tx:
        LOGGER.warning(
            "Keystore.%s requested for_update=True outside an active "
            "transaction; the row lock is released by autocommit. Wrap "
            "the call in `async with profile.transaction(): ...`.",
            where,
        )


def _table_for(dialect: str):
    if dialect == "postgresql":
        from kanon_storage.v1_0.db.models.key_pg import KeyPg

        return KeyPg
    from kanon_storage.v1_0.db.models.key_sqlite import KeySqlite

    return KeySqlite


def _normalize_kid(kid) -> list[str]:
    """Coerce a kid value (None / str / list) to a list[str]."""
    if kid is None:
        return []
    if isinstance(kid, str):
        return [kid] if kid else []
    return list(kid)


def _multikey_for(verkey: str, key_alg: str) -> Optional[str]:
    """Compute the multibase-multicodec multikey, if the alg is supported.

    ACA-Py's key registry only knows a subset of algs (e.g. ed25519, p256,
    bls12381g2, x25519). For algs outside that set we silently return None
    rather than block key creation.
    """
    try:
        from acapy_agent.wallet.keys.manager import verkey_to_multikey

        return verkey_to_multikey(verkey, key_alg)
    except Exception:
        return None


class Keystore:
    """Per-profile keystore — CRUD over the kanon_key table.

    All methods take an `AsyncSession` so the caller controls transaction
    scope (the wallet shares the session with the rest of the operation).
    """

    def __init__(self, *, profile_id: str, master_key: bytes, dialect: str):
        if len(master_key) not in (16, 24, 32):
            raise ValueError("master_key must be 16, 24, or 32 bytes")
        self._profile_id = profile_id
        self._aead = AESGCM(master_key)
        self._table = _table_for(dialect)
        self._dialect = dialect

    async def insert(
        self,
        session: AsyncSession,
        *,
        verkey: str,
        secret: bytes,
        key_alg: str,
        metadata: Optional[dict[str, Any]] = None,
        kid=None,
        multikey: Optional[str] = None,
    ) -> None:
        nonce = os.urandom(12)
        ct = self._aead.encrypt(nonce, secret, _aad(verkey, self._profile_id))
        kid_list = _normalize_kid(kid)
        if multikey is None:
            multikey = _multikey_for(verkey, key_alg)
        row = self._table(
            id=verkey,
            profile_id=self._profile_id,
            key_alg=key_alg,
            secret_ciphertext=ct,
            nonce=nonce,
            metadata_json=metadata,
            kid=kid_list,
            multikey=multikey,
        )
        try:
            session.add(row)
            await session.flush()
        except IntegrityError as err:
            raise WalletDuplicateError(
                f"Key with verkey {verkey!r} already exists in this profile"
            ) from err

    async def fetch(
        self, session: AsyncSession, verkey: str, *, for_update: bool = False
    ) -> tuple[bytes, str, dict[str, Any] | None, list[str]]:
        """Return (secret_bytes, key_alg, metadata, kid_list).

        Kid is now a `list[str]` (askar/kanon convention). Multikey, if
        callers need it, is available via `fetch_full`.
        """
        secret, alg, meta, kids, _multikey = await self.fetch_full(
            session, verkey, for_update=for_update
        )
        return secret, alg, meta, kids

    async def fetch_full(
        self, session: AsyncSession, verkey: str, *, for_update: bool = False
    ) -> tuple[bytes, str, dict[str, Any] | None, list[str], Optional[str]]:
        """Like `fetch` but also returns the multikey column."""
        stmt = select(self._table).where(
            self._table.id == verkey,
            self._table.profile_id == self._profile_id,
        )
        if for_update and self._dialect == "postgresql":
            _warn_for_update_without_tx(session, where="fetch_full")
            stmt = stmt.with_for_update(of=self._table, key_share=False)
        row = (await session.execute(stmt)).scalar_one_or_none()
        if row is None:
            raise WalletNotFoundError(f"Key with verkey {verkey!r} not found")
        secret = self._aead.decrypt(
            row.nonce, row.secret_ciphertext, _aad(verkey, self._profile_id)
        )
        return (
            secret,
            row.key_alg,
            row.metadata_json,
            _normalize_kid(row.kid),
            row.multikey,
        )

    async def fetch_by_kid(
        self, session: AsyncSession, kid: str
    ) -> tuple[str, bytes, str, dict[str, Any] | None]:
        """Return (verkey, secret_bytes, key_alg, metadata) for a key claiming `kid`.

        Multiple verkeys may technically claim the same kid — callers
        wanting that semantics should use `fetch_all_by_kid`.
        """
        rows = await self._fetch_rows_by_kid(session, kid, limit=1)
        if not rows:
            raise WalletNotFoundError(f"Key with kid {kid!r} not found")
        row = rows[0]
        secret = self._aead.decrypt(
            row.nonce, row.secret_ciphertext, _aad(row.id, self._profile_id)
        )
        return row.id, secret, row.key_alg, row.metadata_json

    async def fetch_all_by_kid(
        self, session: AsyncSession, kid: str, *, limit: Optional[int] = None
    ) -> list:
        """Return raw rows whose kid list contains `kid`."""
        return await self._fetch_rows_by_kid(session, kid, limit=limit)

    async def _fetch_rows_by_kid(
        self, session: AsyncSession, kid: str, *, limit: Optional[int]
    ) -> list:
        T = self._table
        stmt = select(T).where(T.profile_id == self._profile_id)
        if self._dialect == "postgresql":
            # JSONB containment: row.kid @> '["<kid>"]'
            stmt = stmt.where(T.kid.contains([kid]))
            if limit:
                stmt = stmt.limit(limit)
            return list((await session.execute(stmt)).scalars().all())
        # SQLite: filter in Python — the kid list is small and rows-per-profile
        # are bounded by wallet contents.
        rows = (await session.execute(stmt)).scalars().all()
        out = []
        for row in rows:
            if kid in _normalize_kid(row.kid):
                out.append(row)
                if limit and len(out) >= limit:
                    break
        return out

    async def fetch_all(
        self,
        session: AsyncSession,
        *,
        tag_filter: Optional[dict] = None,
        limit: Optional[int] = None,
        for_update: bool = False,
    ) -> list:
        """Return raw rows matching an optional `{kid|multikey: value}` tag filter.

        Mirrors askar/kanon's `Session.fetch_all_keys(tag_filter=...)`. Only
        the keys ACA-Py actually queries on are recognised.
        """
        T = self._table
        stmt = select(T).where(T.profile_id == self._profile_id)
        if for_update and self._dialect == "postgresql":
            _warn_for_update_without_tx(session, where="fetch_all")
            stmt = stmt.with_for_update(of=T, key_share=False)

        if tag_filter:
            tf = dict(tag_filter)
            kid_filter = tf.pop("kid", None)
            multikey_filter = tf.pop("multikey", None)
            if tf:
                raise ValueError(
                    f"Unsupported key tag_filter keys: {sorted(tf)}"
                )
            if multikey_filter is not None:
                stmt = stmt.where(T.multikey == multikey_filter)
            if kid_filter is not None and self._dialect == "postgresql":
                stmt = stmt.where(T.kid.contains([kid_filter]))
            if limit and (kid_filter is None or self._dialect == "postgresql"):
                stmt = stmt.limit(limit)
            rows = (await session.execute(stmt)).scalars().all()
            if kid_filter is not None and self._dialect != "postgresql":
                rows = [r for r in rows if kid_filter in _normalize_kid(r.kid)]
                if limit:
                    rows = rows[:limit]
            return list(rows)

        if limit:
            stmt = stmt.limit(limit)
        return list((await session.execute(stmt)).scalars().all())

    async def update_metadata(
        self,
        session: AsyncSession,
        verkey: str,
        metadata: dict[str, Any] | None,
    ) -> None:
        stmt = (
            update(self._table)
            .where(
                self._table.id == verkey,
                self._table.profile_id == self._profile_id,
            )
            .values(metadata_json=metadata)
        )
        result = await session.execute(stmt)
        if result.rowcount == 0:
            raise WalletNotFoundError(f"Key with verkey {verkey!r} not found")

    async def assign_kid(
        self, session: AsyncSession, verkey: str, kid: str
    ) -> list[str]:
        """Append `kid` to the verkey's kid list (idempotent).

        Returns the new full kid list.
        """
        stmt = select(self._table).where(
            self._table.id == verkey,
            self._table.profile_id == self._profile_id,
        )
        if self._dialect == "postgresql":
            _warn_for_update_without_tx(session, where="read-modify-write")
            stmt = stmt.with_for_update(of=self._table, key_share=False)
        row = (await session.execute(stmt)).scalar_one_or_none()
        if row is None:
            raise WalletNotFoundError(f"Key with verkey {verkey!r} not found")

        kids = _normalize_kid(row.kid)
        if kid not in kids:
            kids.append(kid)
        # Backfill multikey if missing — older rows may pre-date the column.
        new_values: dict[str, Any] = {"kid": kids}
        if row.multikey is None:
            mk = _multikey_for(verkey, row.key_alg)
            if mk is not None:
                new_values["multikey"] = mk
        await session.execute(
            update(self._table)
            .where(
                self._table.id == verkey,
                self._table.profile_id == self._profile_id,
            )
            .values(**new_values)
        )
        return kids

    async def unassign_kid(
        self, session: AsyncSession, verkey: str, kid: str
    ) -> list[str]:
        """Remove `kid` from the verkey's kid list (no-op if absent)."""
        stmt = select(self._table).where(
            self._table.id == verkey,
            self._table.profile_id == self._profile_id,
        )
        if self._dialect == "postgresql":
            _warn_for_update_without_tx(session, where="read-modify-write")
            stmt = stmt.with_for_update(of=self._table, key_share=False)
        row = (await session.execute(stmt)).scalar_one_or_none()
        if row is None:
            raise WalletNotFoundError(f"Key with verkey {verkey!r} not found")
        kids = _normalize_kid(row.kid)
        try:
            kids.remove(kid)
        except ValueError:
            pass
        await session.execute(
            update(self._table)
            .where(
                self._table.id == verkey,
                self._table.profile_id == self._profile_id,
            )
            .values(kid=kids)
        )
        return kids

    async def set_metadata(
        self,
        session: AsyncSession,
        verkey: str,
        *,
        metadata: Optional[dict[str, Any]] = None,
        kid=None,
        multikey: Optional[str] = None,
    ) -> None:
        """Replace metadata / kid list / multikey atomically.

        Used by `Wallet.update_key` semantics — pass only what you want
        replaced; `None` leaves the column untouched.
        """
        stmt = select(self._table).where(
            self._table.id == verkey,
            self._table.profile_id == self._profile_id,
        )
        if self._dialect == "postgresql":
            _warn_for_update_without_tx(session, where="read-modify-write")
            stmt = stmt.with_for_update(of=self._table, key_share=False)
        row = (await session.execute(stmt)).scalar_one_or_none()
        if row is None:
            raise WalletNotFoundError(f"Key with verkey {verkey!r} not found")

        new_values: dict[str, Any] = {}
        if metadata is not None:
            new_values["metadata_json"] = metadata
        if kid is not None:
            new_values["kid"] = _normalize_kid(kid)
        if multikey is not None:
            new_values["multikey"] = multikey
        elif row.multikey is None:
            mk = _multikey_for(verkey, row.key_alg)
            if mk is not None:
                new_values["multikey"] = mk
        if not new_values:
            return
        await session.execute(
            update(self._table)
            .where(
                self._table.id == verkey,
                self._table.profile_id == self._profile_id,
            )
            .values(**new_values)
        )

    async def remove(self, session: AsyncSession, verkey: str) -> None:
        from sqlalchemy import delete

        result = await session.execute(
            delete(self._table).where(
                self._table.id == verkey,
                self._table.profile_id == self._profile_id,
            )
        )
        if result.rowcount == 0:
            raise WalletNotFoundError(f"Key with verkey {verkey!r} not found")


def _aad(verkey: str, profile_id: str) -> bytes:
    """AEAD additional-authenticated-data binds ciphertext to (verkey, profile)."""
    return f"kanon-storage|{profile_id}|{verkey}".encode("utf-8")
