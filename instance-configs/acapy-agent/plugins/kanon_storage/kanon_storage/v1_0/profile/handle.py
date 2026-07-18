"""Askar/Kanon Store-API shim over the active SQLAlchemy session."""

from __future__ import annotations

import base64
import json
import logging
from typing import Any, List, Optional

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from kanon_storage.v1_0.storage.errors import (
    RecordDuplicateError,
    RecordNotFoundError,
)

LOGGER = logging.getLogger(__name__)


_BYTES_MARKER = "__kanon_bytes_b64__"


def _decode_stored(stored: Any) -> Any:
    """Convert what's in the DB row back to either bytes or original JSON.

    `{"__kanon_bytes_b64__": "..."}` was a bytes-like value; decode.
    Anything else is returned as-is.
    """
    if isinstance(stored, dict) and _BYTES_MARKER in stored and len(stored) == 1:
        return base64.b64decode(stored[_BYTES_MARKER])
    return stored


def _encode_for_storage(value: Any) -> Any:
    """Coerce a value into something the JSONB column can store.

    Bytes / bytearray / memoryview → base64 wrapped in {_BYTES_MARKER: ...}.
    Everything else → pass through. Strings are stored as JSONB strings
    (NOT pre-parsed) — eagerly `json.loads()`ing them silently coerced
    numeric-string payloads (e.g. LinkSecret's `"12345"`) to `int`,
    which then broke `record.value.decode("ascii")` in upstream
    AnonCredsHolder.get_master_secret.
    """
    if isinstance(value, memoryview):
        value = bytes(value)
    if isinstance(value, (bytes, bytearray)):
        return {_BYTES_MARKER: base64.b64encode(bytes(value)).decode("ascii")}
    return value


class _Entry:
    """Mimics aries_askar.Entry — what `session.handle.fetch*` returns.

    Acapy code accesses `.name`, `.value`, `.value_json`, `.tags`, `.category`.
    """

    __slots__ = ("category", "name", "_stored", "_tags")

    def __init__(self, category: str, name: str, stored: Any, tags: dict | None):
        self.category = category
        self.name = name
        self._stored = _decode_stored(stored)
        self._tags = tags or {}

    @property
    def value(self) -> bytes:
        """Match `aries_askar.Entry.value` — always return bytes.

        ACA-Py code unconditionally calls `.decode("ascii")` /
        `.decode("utf-8")` on `.value`, so we must hand back bytes
        regardless of how the payload was stored. Strings, dicts, and
        lists are encoded as UTF-8; None becomes empty bytes.
        """
        v = self._stored
        if isinstance(v, bytes):
            return v
        if isinstance(v, str):
            return v.encode("utf-8")
        if isinstance(v, (dict, list)):
            return json.dumps(v).encode("utf-8")
        if v is None:
            return b""
        return str(v).encode("utf-8")

    @property
    def value_json(self) -> Any:
        v = self._stored
        if isinstance(v, (dict, list)):
            return v
        if isinstance(v, str):
            try:
                return json.loads(v)
            except (json.JSONDecodeError, TypeError):
                return None
        return v

    @property
    def tags(self) -> dict:
        return dict(self._tags)

    @property
    def raw_value(self) -> bytes:
        v = self._stored
        if isinstance(v, bytes):
            return v
        if isinstance(v, (dict, list)):
            return json.dumps(v).encode("utf-8")
        if v is None:
            return b""
        return str(v).encode("utf-8")


class _KeyEntry:
    """Mimics aries_askar.KeyEntry — what `session.handle.fetch_key` returns.

    Exposes `.name` (verkey), `.key` (an `aries_askar.Key` reconstructed
    from stored secret bytes), `.metadata` (dict-or-JSON-str matching
    askar's quirks), and `.tags` (`{"kid": [...], "multikey": "..."}`).
    """

    __slots__ = ("name", "key", "metadata", "tags", "algorithm")

    def __init__(
        self,
        *,
        name: str,
        key,
        metadata: dict | None,
        tags: dict,
        algorithm: str,
    ):
        self.name = name
        self.key = key
        # askar stores metadata as a JSON string; we hold the parsed dict
        # but expose `.metadata` matching the upstream contract — most
        # ACA-Py code calls `cast(dict, key_entry.metadata)` so a dict is
        # fine; `wallet/askar.py` does `json.loads(key_entry.metadata or "{}")`
        # so a JSON string also works. We hand back the dict, matching the
        # shape `kanon_wallet.py` expects post-parse.
        self.metadata = metadata
        self.tags = tags
        self.algorithm = algorithm


class KanonSessionHandle:
    """Translates the askar/kanon Store handle API to our SQLAlchemy tables."""

    def __init__(self, *, sa_session: AsyncSession, profile_id: str, dialect: str):
        self._sa = sa_session
        self._profile_id = profile_id
        self._dialect = dialect
        # Explicit None — key ops will raise loudly if no AEAD is bound
        # rather than silently returning entries with key=None and
        # crashing later in DIDComm v2.
        self._aead = None

    def _warn_if_no_tx(self, where: str) -> None:
        """Log a warning if `for_update` is requested without an active tx.

        Without an enclosing transaction, asyncpg auto-begins and
        auto-commits each statement, so the row lock is released before
        the caller can do anything useful with it.
        """
        try:
            in_tx = self._sa.in_transaction()
        except Exception:
            in_tx = False
        if not in_tx:
            LOGGER.warning(
                "KanonSessionHandle.%s requested for_update=True outside an "
                "active transaction; the row lock is released by the next "
                "autocommit. Wrap the read-modify-write in "
                "`async with profile.transaction(): ...`.",
                where,
            )

    def _table(self):
        if self._dialect == "postgresql":
            from kanon_storage.v1_0.db.models.generic_record_pg import GenericRecordPg

            return GenericRecordPg
        from kanon_storage.v1_0.db.models.generic_record_sqlite import (
            GenericRecordSqlite,
        )

        return GenericRecordSqlite

    async def fetch(
        self, category: str, name: str, *, for_update: bool = False
    ) -> Optional[_Entry]:
        T = self._table()
        stmt = select(T).where(
            T.profile_id == self._profile_id,
            T.record_type == category,
            T.id == name,
        )
        if for_update and self._dialect == "postgresql":
            self._warn_if_no_tx("fetch")
            stmt = stmt.with_for_update(of=T, key_share=False)
        row = (await self._sa.execute(stmt)).scalar_one_or_none()
        if row is None:
            return None
        return _Entry(category, row.id, row.value, row.tags)

    async def fetch_all(
        self,
        category: str,
        tag_filter: Optional[dict] = None,
        limit: Optional[int] = None,
        *,
        offset: Optional[int] = None,
        for_update: bool = False,
        order_by: Optional[str] = None,
        descending: bool = False,
    ) -> list[_Entry]:
        T = self._table()
        from kanon_storage.v1_0.storage.query import WqlToSqlAlchemy

        stmt = select(T).where(
            T.profile_id == self._profile_id,
            T.record_type == category,
        )
        if tag_filter:
            translator = WqlToSqlAlchemy(
                table=T,
                dialect=self._dialect,
                tag_column="tags",
                custom_tags_column=None,
            )
            clause = translator(tag_filter)
            if clause is not None:
                stmt = stmt.where(clause)
        if for_update and self._dialect == "postgresql":
            self._warn_if_no_tx("fetch")
            stmt = stmt.with_for_update(of=T, key_share=False)
        if limit:
            stmt = stmt.limit(limit)
        if offset:
            stmt = stmt.offset(offset)
        rows = (await self._sa.execute(stmt)).scalars().all()
        return [_Entry(category, r.id, r.value, r.tags) for r in rows]

    async def count(
        self, category: str, tag_filter: Optional[dict] = None
    ) -> int:
        rows = await self.fetch_all(category, tag_filter)
        return len(rows)

    def _coerce_value(self, value, value_json):
        """askar accepts either `value` (bytes/str) or `value_json` (dict).

        We accept the same shapes plus memoryview (indy_credx returns it
        for private keys / key proofs). Anything bytes-like is wrapped in
        a base64 sentinel dict for JSONB storage.
        """
        if value_json is not None:
            return value_json
        return _encode_for_storage(value)

    async def insert(
        self,
        category: str,
        name: str,
        value: Any = None,
        tags: Optional[dict] = None,
        expiry_ms: Optional[int] = None,
        value_json: Any = None,
    ) -> None:
        T = self._table()
        row = T(
            id=name,
            profile_id=self._profile_id,
            record_type=category,
            value=self._coerce_value(value, value_json),
            tags=dict(tags) if tags else None,
        )
        try:
            self._sa.add(row)
            await self._sa.flush()
        except IntegrityError as err:
            raise RecordDuplicateError(
                f"{category} record with id={name!r} already exists"
            ) from err

    async def replace(
        self,
        category: str,
        name: str,
        value: Any = None,
        tags: Optional[dict] = None,
        expiry_ms: Optional[int] = None,
        value_json: Any = None,
    ) -> None:
        T = self._table()
        new_values = {
            "value": self._coerce_value(value, value_json),
            "tags": dict(tags) if tags else None,
        }
        result = await self._sa.execute(
            update(T)
            .where(
                T.profile_id == self._profile_id,
                T.record_type == category,
                T.id == name,
            )
            .values(**new_values)
        )
        if result.rowcount == 0:
            raise RecordNotFoundError(record_type=category, record_id=name)

    async def remove(self, category: str, name: str) -> None:
        T = self._table()
        result = await self._sa.execute(
            delete(T).where(
                T.profile_id == self._profile_id,
                T.record_type == category,
                T.id == name,
            )
        )
        if result.rowcount == 0:
            raise RecordNotFoundError(record_type=category, record_id=name)

    async def remove_all(
        self, category: str, tag_filter: Optional[dict] = None
    ) -> int:
        """Delete every row in `category` matching `tag_filter` in one DELETE.

        Mirrors `BaseRecordAdapter.delete_all` — a single round-trip
        instead of the previous fetch-then-delete-per-row loop, which
        was 2 DB calls per row for a category that could span thousands
        of rows.
        """
        T = self._table()
        stmt = delete(T).where(
            T.profile_id == self._profile_id,
            T.record_type == category,
        )
        if tag_filter:
            from kanon_storage.v1_0.storage.query import WqlToSqlAlchemy

            translator = WqlToSqlAlchemy(
                table=T,
                dialect=self._dialect,
                tag_column="tags",
                custom_tags_column=None,
            )
            clause = translator(tag_filter)
            if clause is not None:
                stmt = stmt.where(clause)
        result = await self._sa.execute(stmt)
        return int(result.rowcount or 0)

    def _key_table(self):
        if self._dialect == "postgresql":
            from kanon_storage.v1_0.db.models.key_pg import KeyPg

            return KeyPg
        from kanon_storage.v1_0.db.models.key_sqlite import KeySqlite

        return KeySqlite

    def _row_to_key_entry(self, row) -> _KeyEntry:
        # Reconstruct eagerly because the `cast(Key, key_entry.key)`
        # pattern in ACA-Py won't tolerate a thunk.
        from kanon_storage.v1_0.wallet import crypto

        decrypted = self._decrypt_secret(row)
        key = crypto.key_from_secret(row.key_alg, decrypted)
        kid_list = list(row.kid) if isinstance(row.kid, list) else (
            [row.kid] if row.kid else []
        )
        tags = {"kid": kid_list}
        if row.multikey:
            tags["multikey"] = row.multikey
        return _KeyEntry(
            name=row.id,
            key=key,
            metadata=row.metadata_json or {},
            tags=tags,
            algorithm=row.key_alg,
        )

    def _decrypt_secret(self, row) -> bytes:
        """Decrypt the stored secret, requiring an attached AEAD.

        Returning None here was masking a real misconfiguration: handles
        without an AEAD bound would produce `_KeyEntry(key=None)`, which
        then NPE'd inside `cast(Key, key_entry.key).algorithm.value` far
        from the actual cause. Raise instead.
        """
        aead = self._aead
        if aead is None:
            raise RuntimeError(
                "KanonSessionHandle has no keystore AEAD attached; "
                "key decryption requires `attach_keystore_aead(...)`."
            )
        aad = f"kanon-storage|{self._profile_id}|{row.id}".encode("utf-8")
        return aead.decrypt(row.nonce, row.secret_ciphertext, aad)

    def attach_keystore_aead(self, aead) -> None:
        """Bind the AESGCM master-key cipher to enable `.key` materialisation.

        Called by the profile session when the wallet is wired up — keeps
        the master key off the handle by default but lets DIDComm v2 /
        kanon-style consumers grab `aries_askar.Key` objects when they
        need them.
        """
        self._aead = aead  # noqa: SLF001 — by-design back-channel.

    async def fetch_key(self, name: str, *, for_update: bool = False):
        T = self._key_table()
        stmt = select(T).where(
            T.id == name, T.profile_id == self._profile_id
        )
        if for_update and self._dialect == "postgresql":
            self._warn_if_no_tx("fetch")
            stmt = stmt.with_for_update(of=T, key_share=False)
        row = (await self._sa.execute(stmt)).scalar_one_or_none()
        if row is None:
            return None
        return self._row_to_key_entry(row)

    async def fetch_all_keys(
        self,
        *,
        category: Optional[str] = None,
        tag_filter: Optional[dict] = None,
        limit: Optional[int] = None,
        for_update: bool = False,
    ) -> List[_KeyEntry]:
        T = self._key_table()
        stmt = select(T).where(T.profile_id == self._profile_id)
        if for_update and self._dialect == "postgresql":
            self._warn_if_no_tx("fetch")
            stmt = stmt.with_for_update(of=T, key_share=False)

        kid_filter = None
        multikey_filter = None
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

        rows = (await self._sa.execute(stmt)).scalars().all()
        if kid_filter is not None and self._dialect != "postgresql":
            rows = [
                r
                for r in rows
                if kid_filter
                in (
                    list(r.kid) if isinstance(r.kid, list) else ([r.kid] if r.kid else [])
                )
            ]
            if limit:
                rows = rows[:limit]
        return [self._row_to_key_entry(r) for r in rows]

    async def insert_key(
        self,
        name: str,
        key,
        *,
        metadata=None,
        tags: Optional[dict] = None,
    ) -> None:
        """Insert a key by verkey.

        Accepts either an `aries_askar.Key` (extracts secret bytes) or a
        raw `bytes` blob (caller already extracted).
        """
        # Coerce to (secret_bytes, key_alg). We can't reach the wallet's
        # KeyTypes registry from here, so introspect off the askar Key.
        if isinstance(key, (bytes, bytearray)):
            secret = bytes(key)
            key_alg = (tags or {}).get("alg") or (
                metadata.get("alg") if isinstance(metadata, dict) else None
            )
            if not key_alg:
                raise ValueError(
                    "insert_key with raw bytes requires `tags={'alg': ...}` "
                    "or `metadata={'alg': ...}`"
                )
        else:
            secret = bytes(key.get_secret_bytes())
            try:
                key_alg = key.algorithm.value
            except Exception as exc:
                raise ValueError(
                    "insert_key: key object missing .algorithm.value"
                ) from exc

        tags = dict(tags or {})
        kid = tags.pop("kid", None)
        multikey = tags.pop("multikey", None)

        # Metadata may be a dict or a JSON-encoded string (askar style).
        # Reject malformed JSON strings instead of silently wrapping under
        # a `_raw` sentinel — downstream consumers do `metadata["alg"]`
        # and would otherwise miss the field with no diagnostic.
        if isinstance(metadata, str):
            if not metadata:
                metadata_dict = {}
            else:
                try:
                    parsed = json.loads(metadata)
                except (json.JSONDecodeError, TypeError) as err:
                    raise ValueError(
                        "insert_key: metadata string must be a JSON object; "
                        f"got malformed JSON ({err})"
                    ) from err
                if not isinstance(parsed, dict):
                    raise ValueError(
                        "insert_key: metadata JSON must decode to an object; "
                        f"got {type(parsed).__name__}"
                    )
                metadata_dict = parsed
        else:
            metadata_dict = dict(metadata or {})

        if self._aead is None:
            raise RuntimeError(
                "KanonSessionHandle.insert_key called without keystore AEAD; "
                "this handle was not wired by a profile session"
            )

        import os

        nonce = os.urandom(12)
        aad = f"kanon-storage|{self._profile_id}|{name}".encode("utf-8")
        ct = self._aead.encrypt(nonce, secret, aad)

        if multikey is None:
            try:
                from acapy_agent.wallet.keys.manager import verkey_to_multikey

                multikey = verkey_to_multikey(name, key_alg)
            except Exception:
                multikey = None

        T = self._key_table()
        kid_list = (
            list(kid) if isinstance(kid, list) else ([kid] if kid else [])
        )
        row = T(
            id=name,
            profile_id=self._profile_id,
            key_alg=key_alg,
            secret_ciphertext=ct,
            nonce=nonce,
            metadata_json=metadata_dict,
            kid=kid_list,
            multikey=multikey,
        )
        try:
            self._sa.add(row)
            await self._sa.flush()
        except IntegrityError as err:
            from acapy_agent.wallet.error import WalletDuplicateError

            raise WalletDuplicateError(
                f"Key {name!r} already exists in this profile"
            ) from err

    async def update_key(
        self,
        name: str,
        *,
        metadata=None,
        tags: Optional[dict] = None,
    ) -> None:
        from acapy_agent.wallet.error import WalletNotFoundError

        T = self._key_table()
        # Pre-check existence so we can raise the upstream-shaped error
        stmt = select(T).where(
            T.id == name, T.profile_id == self._profile_id
        )
        if self._dialect == "postgresql":
            self._warn_if_no_tx("update_key")
            stmt = stmt.with_for_update(of=T, key_share=False)
        row = (await self._sa.execute(stmt)).scalar_one_or_none()
        if row is None:
            raise WalletNotFoundError(f"Key {name!r} not found")

        new_values: dict[str, Any] = {}
        if metadata is not None:
            if isinstance(metadata, str):
                if not metadata:
                    new_values["metadata_json"] = {}
                else:
                    try:
                        parsed = json.loads(metadata)
                    except (json.JSONDecodeError, TypeError) as err:
                        raise ValueError(
                            "update_key: metadata string must be a JSON object; "
                            f"got malformed JSON ({err})"
                        ) from err
                    if not isinstance(parsed, dict):
                        raise ValueError(
                            "update_key: metadata JSON must decode to an object; "
                            f"got {type(parsed).__name__}"
                        )
                    new_values["metadata_json"] = parsed
            else:
                new_values["metadata_json"] = dict(metadata)

        if tags is not None:
            tags = dict(tags)
            kid = tags.pop("kid", None)
            multikey = tags.pop("multikey", None)
            if tags:
                raise ValueError(
                    f"Unsupported key tag keys: {sorted(tags)}"
                )
            if kid is not None:
                new_values["kid"] = (
                    list(kid)
                    if isinstance(kid, list)
                    else ([kid] if kid else [])
                )
            if multikey is not None:
                new_values["multikey"] = multikey

        if not new_values:
            return

        await self._sa.execute(
            update(T)
            .where(T.id == name, T.profile_id == self._profile_id)
            .values(**new_values)
        )

    async def remove_key(self, name: str) -> None:
        from acapy_agent.wallet.error import WalletNotFoundError

        T = self._key_table()
        result = await self._sa.execute(
            delete(T).where(
                T.id == name, T.profile_id == self._profile_id
            )
        )
        if result.rowcount == 0:
            raise WalletNotFoundError(f"Key {name!r} not found")

    async def commit(self) -> None:
        """Commit the underlying SQLAlchemy session (if a txn is active)."""
        if self._sa.in_transaction():
            await self._sa.commit()

    async def rollback(self) -> None:
        """Rollback the underlying SQLAlchemy session (if a txn is active)."""
        if self._sa.in_transaction():
            await self._sa.rollback()

    async def close(self) -> None:
        """Close the underlying SQLAlchemy session.

        Most callers should let `KanonStorageProfileSession._teardown`
        handle this; provided for parity with `aries_askar.Session.close`.
        """
        await self._sa.close()
