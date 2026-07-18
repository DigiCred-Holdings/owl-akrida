"""KanonWallet — concrete BaseWallet over our keystore + DID table."""

from __future__ import annotations

import json
from typing import List, Optional, Sequence, Tuple, Union

from acapy_agent.core.profile import ProfileSession
from acapy_agent.wallet.base import BaseWallet
from acapy_agent.wallet.did_info import DIDInfo, KeyInfo
from acapy_agent.wallet.did_method import DIDMethod, DIDMethods
from acapy_agent.wallet.error import (
    WalletDuplicateError,
    WalletError,
    WalletNotFoundError,
)
from acapy_agent.wallet.key_type import ED25519, KeyType, KeyTypes
from sqlalchemy import select

from kanon_storage.v1_0.wallet import crypto


class KanonWallet(BaseWallet):
    """BaseWallet impl over our SQLAlchemy + AES-GCM keystore."""

    def __init__(self, session: ProfileSession):
        self._session = session
        self._profile = session.profile
        # Lazy-imported to avoid circular import at module load
        from kanon_storage.v1_0.wallet.keystore import Keystore

        self._keystore = Keystore(
            profile_id=self._profile.profile_id,
            master_key=self._profile.config.master_key,
            dialect=self._profile.config.dialect,
        )
        self._did_methods: DIDMethods = self._profile.context.inject_or(DIDMethods) or DIDMethods()
        self._key_types: KeyTypes = self._profile.context.inject_or(KeyTypes) or KeyTypes()

    def _did_table(self):
        if self._profile.config.dialect == "postgresql":
            from kanon_storage.v1_0.db.models.did_pg import DidPg

            return DidPg
        from kanon_storage.v1_0.db.models.did_sqlite import DidSqlite

        return DidSqlite

    @property
    def _sa(self):
        return self._session.sa_session

    async def create_signing_key(
        self,
        key_type: KeyType,
        seed: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> KeyInfo:
        return await self.create_key(key_type, seed, metadata)

    async def create_key(
        self,
        key_type: KeyType,
        seed: Optional[str] = None,
        metadata: Optional[dict] = None,
        kid: Optional[str] = None,
    ) -> KeyInfo:
        keypair = crypto.create_keypair(key_type, seed)
        verkey = crypto.verkey_for(keypair)
        secret = crypto.secret_bytes_for(keypair)
        await self._keystore.insert(
            self._sa,
            verkey=verkey,
            secret=secret,
            key_alg=crypto.alg_name(key_type),
            metadata=metadata or {},
            kid=[kid] if kid else [],
        )
        return KeyInfo(verkey=verkey, metadata=metadata or {}, key_type=key_type, kid=kid)

    async def get_signing_key(self, verkey: str) -> KeyInfo:
        _, alg_name, metadata, kids = await self._keystore.fetch(self._sa, verkey)
        return KeyInfo(
            verkey=verkey,
            metadata=metadata or {},
            key_type=crypto.key_type_from_alg_name(alg_name),
            kid=kids,
        )

    async def get_key_by_kid(self, kid: str) -> KeyInfo:
        # Mirror askar/kanon — fetch all matches, error on duplicates.
        rows = await self._keystore.fetch_all_by_kid(self._sa, kid, limit=2)
        if len(rows) > 1:
            raise WalletDuplicateError(f"More than one key found by kid {kid}")
        if not rows:
            raise WalletNotFoundError(f"No key found for kid {kid}")
        row = rows[0]
        return KeyInfo(
            verkey=row.id,
            metadata=row.metadata_json or {},
            key_type=crypto.key_type_from_alg_name(row.key_alg),
            kid=kid,
        )

    async def replace_signing_key_metadata(self, verkey: str, metadata: dict):
        await self._keystore.update_metadata(self._sa, verkey, metadata or {})

    async def assign_kid_to_key(self, verkey: str, kid: str) -> KeyInfo:
        await self._keystore.assign_kid(self._sa, verkey, kid)
        # Re-fetch to get current alg/metadata; mirrors askar shape.
        info = await self.get_signing_key(verkey)
        return KeyInfo(
            verkey=info.verkey,
            metadata=info.metadata,
            key_type=info.key_type,
            kid=kid,
        )

    async def unassign_kid_from_key(self, verkey: str, kid: str) -> KeyInfo:
        await self._keystore.unassign_kid(self._sa, verkey, kid)
        info = await self.get_signing_key(verkey)
        return KeyInfo(
            verkey=info.verkey,
            metadata=info.metadata,
            key_type=info.key_type,
            kid=kid,
        )

    async def create_local_did(
        self,
        method: DIDMethod,
        key_type: KeyType,
        seed: Optional[str] = None,
        did: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> DIDInfo:
        from acapy_agent.wallet.did_parameters_validation import DIDParametersValidation

        validator = DIDParametersValidation(self._did_methods)
        validator.validate_key_type(method, key_type)

        keypair = crypto.create_keypair(key_type, seed)
        verkey = crypto.verkey_for(keypair)
        secret = crypto.secret_bytes_for(keypair)
        verkey_bytes = bytes(keypair.get_public_bytes())

        # Derive DID via the validator (handles did:key, did:peer, did:sov, etc.)
        did = validator.validate_or_derive_did(method, key_type, verkey_bytes, did)

        # Insert key first. The DID itself is a natural kid for this key —
        # askar/kanon do the same so DIDComm-v2 lookups via did# fragments
        # work without an explicit `assign_kid_to_key` call.
        try:
            await self._keystore.insert(
                self._sa,
                verkey=verkey,
                secret=secret,
                key_alg=crypto.alg_name(key_type),
                metadata={"did": did},
                kid=[],
            )
        except WalletDuplicateError:
            # Same verkey already known (e.g. a key was created bare and is
            # now being upgraded into a DID). Mirrors askar's behaviour:
            # ignore and proceed to the DID record.
            pass

        did_table = self._did_table()
        row = did_table(
            id=did,
            profile_id=self._profile.profile_id,
            method=method.method_name,
            verkey=verkey,
            key_type=key_type.key_type,
            metadata_json=metadata or {},
        )
        from sqlalchemy.exc import IntegrityError

        try:
            self._sa.add(row)
            await self._sa.flush()
        except IntegrityError as err:
            raise WalletDuplicateError(f"DID {did!r} already exists") from err

        return DIDInfo(
            did=did,
            verkey=verkey,
            metadata=metadata or {},
            method=method,
            key_type=key_type,
        )

    async def store_did(self, did_info: DIDInfo) -> DIDInfo:
        """Insert a pre-constructed DIDInfo (no keypair material expected)."""
        did_table = self._did_table()
        row = did_table(
            id=did_info.did,
            profile_id=self._profile.profile_id,
            method=did_info.method.method_name,
            verkey=did_info.verkey,
            key_type=did_info.key_type.key_type,
            metadata_json=did_info.metadata or {},
        )
        from sqlalchemy.exc import IntegrityError

        try:
            self._sa.add(row)
            await self._sa.flush()
        except IntegrityError as err:
            raise WalletDuplicateError(f"DID {did_info.did!r} already exists") from err
        return did_info

    async def get_local_did(self, did: str) -> DIDInfo:
        row = await self._fetch_did(did=did)
        if row is None:
            raise WalletNotFoundError(f"DID {did!r} not found")
        return self._row_to_did_info(row)

    async def get_local_did_for_verkey(self, verkey: str) -> DIDInfo:
        # A single verkey can map to multiple DID rows. The relevant
        # case is `did:peer:4`, which is stored as both a long form
        # (with hash + encoded public key) and a short form (hash only).
        # Connection records hold the short form; consumer code expects
        # us to return that one. Mirrors acapy_agent/wallet/askar.py
        # and acapy_agent/wallet/kanon_wallet.py — fetch all matches,
        # default to the first, and if it's a did:peer:4 with siblings,
        # swap to whichever has the shorter id.
        did_table = self._did_table()
        stmt = select(did_table).where(
            did_table.verkey == verkey,
            did_table.profile_id == self._profile.profile_id,
        )
        rows = (await self._sa.execute(stmt)).scalars().all()
        if not rows:
            raise WalletNotFoundError(f"DID for verkey {verkey!r} not found")

        # A single verkey legitimately maps to *at most two* rows
        # (peer:did:4 long + short). More than two rows means a corrupt
        # database — log a warning so operators can spot it before it
        # silently picks an arbitrary row.
        if len(rows) > 2:
            import logging as _logging

            _logging.getLogger(__name__).warning(
                "get_local_did_for_verkey: %d DID rows for verkey=%r "
                "(expected at most 2 for peer:did:4 long/short); "
                "database likely contains stale duplicates",
                len(rows),
                verkey,
            )

        chosen = rows[0]
        if len(rows) > 1 and chosen.id.startswith("did:peer:4"):
            # Pick the shortest deterministically — handles the >2 case
            # too if duplicates somehow accumulated.
            shortest = min(rows, key=lambda r: len(r.id))
            chosen = shortest
        return self._row_to_did_info(chosen)

    async def get_local_dids(self) -> Sequence[DIDInfo]:
        did_table = self._did_table()
        stmt = select(did_table).where(
            did_table.profile_id == self._profile.profile_id
        )
        rows = (await self._sa.execute(stmt)).scalars().all()
        return [self._row_to_did_info(r) for r in rows]

    async def replace_local_did_metadata(self, did: str, metadata: dict):
        from sqlalchemy import update

        did_table = self._did_table()
        stmt = (
            update(did_table)
            .where(
                did_table.id == did,
                did_table.profile_id == self._profile.profile_id,
            )
            .values(metadata_json=metadata or {})
        )
        result = await self._sa.execute(stmt)
        if result.rowcount == 0:
            raise WalletNotFoundError(f"DID {did!r} not found")

    async def get_public_did(self) -> Optional[DIDInfo]:
        public_did = await self._fetch_config("default_public_did")
        if not public_did:
            return None
        try:
            return await self.get_local_did(public_did)
        except WalletNotFoundError:
            return None

    async def set_public_did(self, did: Union[str, DIDInfo]) -> DIDInfo:
        did_str = did.did if isinstance(did, DIDInfo) else did
        info = await self.get_local_did(did_str)
        meta = dict(info.metadata or {})
        meta["posted"] = True
        await self.replace_local_did_metadata(did_str, meta)
        await self._set_config("default_public_did", did_str)
        return DIDInfo(
            did=info.did,
            verkey=info.verkey,
            metadata=meta,
            method=info.method,
            key_type=info.key_type,
        )

    async def sign_message(
        self,
        message: Union[List[bytes], bytes],
        from_verkey: str,
    ) -> bytes:
        secret, alg_name, _, _ = await self._keystore.fetch(self._sa, from_verkey)
        key = crypto.key_from_secret(alg_name, secret)
        return crypto.sign(key, message)

    async def verify_message(
        self,
        message: Union[List[bytes], bytes],
        signature: bytes,
        from_verkey: str,
        key_type: KeyType,
    ) -> bool:
        public_key = crypto.public_key_from_verkey(from_verkey, key_type)
        return crypto.verify(public_key, message, signature)

    async def pack_message(
        self,
        message: str,
        to_verkeys: Sequence[str],
        from_verkey: Optional[str] = None,
    ) -> bytes:
        from kanon_storage.v1_0.wallet.didcomm_v1 import pack_message as _pack

        return await _pack(self, message, to_verkeys, from_verkey)

    async def unpack_message(
        self, enc_message: bytes
    ) -> Tuple[str, str, str]:
        from kanon_storage.v1_0.wallet.didcomm_v1 import unpack_message as _unpack

        return await _unpack(self, enc_message)

    async def rotate_did_keypair_start(
        self, did: str, next_seed: Optional[str] = None
    ) -> str:
        """Begin rotation: create new keypair and stash next_verkey on the DID.

        Mirrors `acapy_agent.wallet.askar.AskarWallet.rotate_did_keypair_start`.
        """
        did_methods = self._profile.context.inject_or(DIDMethods) or self._did_methods
        did_method: DIDMethod = did_methods.from_did(did)
        if not did_method.supports_rotation:
            raise WalletError(
                f"DID method '{did_method.method_name}' does not support key rotation."
            )

        # Currently only ED25519 rotations are supported (matches askar).
        keypair = crypto.create_keypair(ED25519, next_seed)
        verkey = crypto.verkey_for(keypair)
        secret = crypto.secret_bytes_for(keypair)
        try:
            await self._keystore.insert(
                self._sa,
                verkey=verkey,
                secret=secret,
                key_alg=crypto.alg_name(ED25519),
                metadata={},
                kid=[],
            )
        except WalletDuplicateError:
            # Already present — idempotent restart of a rotation.
            pass

        info = await self.get_local_did(did)
        meta = dict(info.metadata or {})
        meta["next_verkey"] = verkey
        await self.replace_local_did_metadata(did, meta)
        return verkey

    async def rotate_did_keypair_apply(self, did: str) -> DIDInfo:
        """Promote the staged `next_verkey` to be the DID's primary verkey."""
        from sqlalchemy import update as _update

        info = await self.get_local_did(did)
        meta = dict(info.metadata or {})
        next_verkey = meta.pop("next_verkey", None)
        if not next_verkey:
            raise WalletError("Cannot rotate DID key: no next key established")

        did_table = self._did_table()
        stmt = (
            _update(did_table)
            .where(
                did_table.id == did,
                did_table.profile_id == self._profile.profile_id,
            )
            .values(verkey=next_verkey, metadata_json=meta)
        )
        result = await self._sa.execute(stmt)
        if result.rowcount == 0:
            raise WalletNotFoundError(f"DID {did!r} not found")

        return DIDInfo(
            did=did,
            verkey=next_verkey,
            metadata=meta,
            method=info.method,
            key_type=info.key_type,
        )

    async def _fetch_did(self, *, did: str):
        did_table = self._did_table()
        stmt = select(did_table).where(
            did_table.id == did,
            did_table.profile_id == self._profile.profile_id,
        )
        return (await self._sa.execute(stmt)).scalar_one_or_none()

    def _row_to_did_info(self, row) -> DIDInfo:
        return DIDInfo(
            did=row.id,
            verkey=row.verkey,
            metadata=row.metadata_json or {},
            method=self._did_methods.from_method(row.method),
            key_type=self._key_types.from_key_type(row.key_type),
        )

    async def _fetch_config(self, key: str) -> Optional[str]:
        """Read a profile-level scalar config from a special generic record."""
        from acapy_agent.storage.base import BaseStorage
        from acapy_agent.storage.error import StorageNotFoundError

        storage = self._session.inject(BaseStorage)
        try:
            rec = await storage.get_record("kanon_config", key)
        except StorageNotFoundError:
            return None
        try:
            return json.loads(rec.value).get("value")
        except Exception:
            return None

    async def _set_config(self, key: str, value: str) -> None:
        from acapy_agent.storage.base import BaseStorage
        from acapy_agent.storage.error import StorageNotFoundError
        from acapy_agent.storage.record import StorageRecord

        storage = self._session.inject(BaseStorage)
        body = json.dumps({"value": value})
        try:
            existing = await storage.get_record("kanon_config", key)
            await storage.update_record(existing, body, {})
        except StorageNotFoundError:
            await storage.add_record(
                StorageRecord("kanon_config", body, {}, key)
            )
