"""KanonIndyIssuer — IndyIssuer implementation."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Optional, Sequence, Tuple

from acapy_agent.core.profile import Profile
from acapy_agent.indy.issuer import (
    DEFAULT_CRED_DEF_TAG,
    DEFAULT_SIGNATURE_TYPE,
    IndyIssuer,
    IndyIssuerError,
    IndyIssuerRevocationRegistryFullError,
)
from indy_credx import (
    Credential,
    CredentialDefinition,
    CredentialOffer,
    CredentialRevocationConfig,
    CredxError,
    RevocationRegistry,
    RevocationRegistryDefinition,
    RevocationRegistryDefinitionPrivate,
    RevocationRegistryDelta,
    Schema,
)

from kanon_storage.v1_0.anoncreds.categories import (
    CATEGORY_CRED_DEF,
    CATEGORY_CRED_DEF_KEY_PROOF,
    CATEGORY_CRED_DEF_PRIVATE,
    CATEGORY_REV_REG,
    CATEGORY_REV_REG_DEF,
    CATEGORY_REV_REG_DEF_PRIVATE,
    CATEGORY_REV_REG_INFO,
    CATEGORY_SCHEMA,
)

LOGGER = logging.getLogger(__name__)


class KanonIndyIssuer(IndyIssuer):
    """IndyIssuer implementation over our session.handle shim."""

    def __init__(self, profile: Profile):
        self._profile = profile

    @property
    def profile(self) -> Profile:
        return self._profile

    async def create_schema(
        self,
        origin_did: str,
        schema_name: str,
        schema_version: str,
        attribute_names: Sequence[str],
    ) -> Tuple[str, str]:
        """Create and store a new schema in the wallet."""
        try:
            schema = Schema.create(
                origin_did, schema_name, schema_version, attribute_names
            )
            schema_id = schema.id
            schema_json = schema.to_json()
            async with self._profile.session() as session:
                await session.handle.insert(
                    CATEGORY_SCHEMA,
                    schema_id,
                    schema_json,
                    tags={
                        "name": schema_name,
                        "version": schema_version,
                        "issuer_id": origin_did,
                        "state": "finished",
                    },
                )
        except CredxError as err:
            raise IndyIssuerError("Error creating schema") from err
        return (schema_id, schema_json)

    async def credential_definition_in_wallet(
        self, credential_definition_id: str
    ) -> bool:
        """Whether the cred def's private record is present in the wallet."""
        async with self._profile.session() as session:
            return (
                await session.handle.fetch(
                    CATEGORY_CRED_DEF_PRIVATE, credential_definition_id
                )
            ) is not None

    async def create_and_store_credential_definition(
        self,
        origin_did: str,
        schema: dict,
        signature_type: Optional[str] = None,
        tag: Optional[str] = None,
        support_revocation: bool = False,
    ) -> Tuple[str, str]:
        """Create and store a new credential definition.

        Mirrors IndyCredxIssuer: the cred_def record is tagged with
        ``schema_id`` so create_credential_offer can resolve the full
        schema id (vs. the seqno embedded in cred_def.schema_id).
        """
        try:
            (
                cred_def,
                cred_def_private,
                key_proof,
            ) = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: CredentialDefinition.create(
                    origin_did,
                    schema,
                    signature_type or DEFAULT_SIGNATURE_TYPE,
                    tag or DEFAULT_CRED_DEF_TAG,
                    support_revocation=support_revocation,
                ),
            )
            cred_def_id = cred_def.id
            cred_def_json = cred_def.to_json()
        except CredxError as err:
            raise IndyIssuerError("Error creating credential definition") from err

        # indy_credx native `to_json()` emits the legacy indy format
        # (`id`/`schemaId` only). The anoncreds-format issue-credential
        # path (workflow_protocol issue-credential action when
        # is_anoncreds=True) deserialises with the anoncreds CredDef
        # marshmallow schema, which requires `issuerId`. Augment the
        # stored JSON with the anoncreds-style fields so both code
        # paths can read it. Extra fields are ignored by indy_credx.
        try:
            cred_def_dict = json.loads(cred_def_json)
            cred_def_dict.setdefault("issuerId", origin_did)
            cred_def_dict.setdefault(
                "schemaId", schema.get("id") or cred_def_dict.get("schemaId")
            )
            cred_def_json = json.dumps(cred_def_dict)
        except Exception:
            # Fall back to the original legacy JSON if augmentation fails
            # — better to keep the legacy path working than block startup.
            LOGGER.debug(
                "failed to augment cred_def_json with anoncreds fields",
                exc_info=True,
            )

        async with self._profile.transaction() as txn:
            await txn.handle.insert(
                CATEGORY_CRED_DEF,
                cred_def_id,
                cred_def_json,
                # IndyCredxIssuer pattern: tag with schema_id so the offer
                # path can resolve the full id (cred_def.schema_id may be
                # a seqno on Indy ledgers). Also tag issuer_id and
                # state="finished" so AnonCredsIssuer.match_created_*
                # can find this cred def via the anoncreds query path.
                tags={
                    "schema_id": schema["id"],
                    "issuer_id": origin_did,
                    "schema_issuer_id": schema["id"].split(":")[0],
                    "schema_name": schema.get("name", ""),
                    "schema_version": schema.get("version", ""),
                    "state": "finished",
                    "epoch": str(int(time.time())),
                },
            )
            await txn.handle.insert(
                CATEGORY_CRED_DEF_PRIVATE,
                cred_def_id,
                cred_def_private.to_json_buffer(),
            )
            await txn.handle.insert(
                CATEGORY_CRED_DEF_KEY_PROOF,
                cred_def_id,
                key_proof.to_json_buffer(),
            )
            await txn.commit()
        return (cred_def_id, cred_def_json)

    async def create_credential_offer(self, credential_definition_id: str) -> str:
        """Create a credential offer for the given cred def id."""
        async with self._profile.session() as session:
            cred_def = await session.handle.fetch(
                CATEGORY_CRED_DEF, credential_definition_id
            )
            key_proof = await session.handle.fetch(
                CATEGORY_CRED_DEF_KEY_PROOF, credential_definition_id
            )
        if not cred_def or not key_proof:
            raise IndyIssuerError(
                "Credential definition not found for credential offer"
            )
        try:
            schema_id = cred_def.tags.get("schema_id")
            cred_def_obj = CredentialDefinition.load(cred_def.raw_value)
            credential_offer = CredentialOffer.create(
                schema_id or cred_def_obj.schema_id,
                cred_def_obj,
                key_proof.raw_value,
            )
        except CredxError as err:
            raise IndyIssuerError("Error creating credential offer") from err
        return credential_offer.to_json()

    async def create_credential(
        self,
        schema: dict,
        credential_offer: dict,
        credential_request: dict,
        credential_values: dict,
        revoc_reg_id: Optional[str] = None,
        tails_file_path: Optional[str] = None,
    ) -> Tuple[str, Optional[str]]:
        """Create a credential, optionally with revocation handles.

        When ``revoc_reg_id`` is set: load rev_reg + rev_reg_def +
        rev_reg_def_private + rev_reg_info, atomically bump curr_id,
        issue with a CredentialRevocationConfig. Mirrors IndyCredxIssuer.
        """
        credential_definition_id = credential_offer["cred_def_id"]
        async with self._profile.session() as session:
            cred_def = await session.handle.fetch(
                CATEGORY_CRED_DEF, credential_definition_id
            )
            cred_def_private = await session.handle.fetch(
                CATEGORY_CRED_DEF_PRIVATE, credential_definition_id
            )
        if not cred_def or not cred_def_private:
            raise IndyIssuerError(
                "Credential definition not found for credential issuance"
            )

        raw_values = {}
        schema_attributes = schema["attrNames"]
        for attribute in schema_attributes:
            try:
                credential_value = credential_values[attribute]
            except KeyError:
                raise IndyIssuerError(
                    "Provided credential values are missing a value "
                    f"for the schema attribute '{attribute}'"
                )
            raw_values[attribute] = str(credential_value)

        if revoc_reg_id:
            try:
                async with self._profile.transaction() as txn:
                    rev_reg = await txn.handle.fetch(CATEGORY_REV_REG, revoc_reg_id)
                    rev_reg_info = await txn.handle.fetch(
                        CATEGORY_REV_REG_INFO, revoc_reg_id, for_update=True
                    )
                    rev_reg_def = await txn.handle.fetch(
                        CATEGORY_REV_REG_DEF, revoc_reg_id
                    )
                    rev_key = await txn.handle.fetch(
                        CATEGORY_REV_REG_DEF_PRIVATE, revoc_reg_id
                    )
                    if not rev_reg:
                        raise IndyIssuerError("Revocation registry not found")
                    if not rev_reg_info:
                        raise IndyIssuerError(
                            "Revocation registry metadata not found"
                        )
                    if not rev_reg_def:
                        raise IndyIssuerError(
                            "Revocation registry definition not found"
                        )
                    if not rev_key:
                        raise IndyIssuerError(
                            "Revocation registry definition private data not found"
                        )

                    rev_info = rev_reg_info.value_json
                    rev_reg_index = rev_info["curr_id"] + 1
                    try:
                        rev_reg_def_obj = RevocationRegistryDefinition.load(
                            rev_reg_def.raw_value
                        )
                    except CredxError as err:
                        raise IndyIssuerError(
                            "Error loading revocation registry definition"
                        ) from err
                    if rev_reg_index > rev_reg_def_obj.max_cred_num:
                        raise IndyIssuerRevocationRegistryFullError(
                            "Revocation registry is full"
                        )
                    rev_info["curr_id"] = rev_reg_index
                    await txn.handle.replace(
                        CATEGORY_REV_REG_INFO,
                        revoc_reg_id,
                        value_json=rev_info,
                    )
                    await txn.commit()
            except CredxError as err:
                raise IndyIssuerError(
                    "Error updating revocation registry index"
                ) from err

            revoc = CredentialRevocationConfig(
                rev_reg_def_obj,
                rev_key.raw_value,
                rev_reg.raw_value,
                rev_reg_index,
                rev_info.get("used_ids") or [],
            )
            credential_revocation_id = str(rev_reg_index)
        else:
            revoc = None
            credential_revocation_id = None

        # anoncreds-holder compatibility shim (entropy → prover_did)
        if not credential_request.get("prover_did"):
            if "entropy" in credential_request:
                credential_request = dict(credential_request)
                credential_request["prover_did"] = credential_request["entropy"]
                del credential_request["entropy"]

        try:
            (
                credential,
                _upd_rev_reg,
                _delta,
            ) = await asyncio.get_event_loop().run_in_executor(
                None,
                Credential.create,
                cred_def.raw_value,
                cred_def_private.raw_value,
                credential_offer,
                credential_request,
                raw_values,
                None,
                revoc,
            )
        except CredxError as err:
            raise IndyIssuerError("Error creating credential") from err

        return credential.to_json(), credential_revocation_id

    async def revoke_credentials(
        self,
        cred_def_id: str,
        revoc_reg_id: str,
        tails_file_path: str,
        cred_revoc_ids: Sequence[str],
    ) -> Tuple[Optional[str], Sequence[str]]:
        """Revoke a set of credentials in a revocation registry.

        Returns the merged delta JSON (or None if nothing was revoked)
        and the list of cred_rev_ids that could not be revoked.

        Mirrors IndyCredxIssuer.revoke_credentials including its
        retry-on-concurrent-update loop.
        """
        delta = None
        failed_crids: set = set()
        max_attempt = 5
        attempt = 0

        while True:
            attempt += 1
            if attempt >= max_attempt:
                raise IndyIssuerError(
                    "Repeated conflict attempting to update registry"
                )

            async with self._profile.session() as session:
                cred_def_rec = await session.handle.fetch(
                    CATEGORY_CRED_DEF, cred_def_id
                )
                rev_reg_def_rec = await session.handle.fetch(
                    CATEGORY_REV_REG_DEF, revoc_reg_id
                )
                rev_reg_def_priv_rec = await session.handle.fetch(
                    CATEGORY_REV_REG_DEF_PRIVATE, revoc_reg_id
                )
                rev_reg_rec = await session.handle.fetch(
                    CATEGORY_REV_REG, revoc_reg_id
                )
                rev_reg_info_rec = await session.handle.fetch(
                    CATEGORY_REV_REG_INFO, revoc_reg_id
                )
            if not cred_def_rec:
                raise IndyIssuerError("Credential definition not found")
            if not rev_reg_def_rec:
                raise IndyIssuerError("Revocation registry definition not found")
            if not rev_reg_def_priv_rec:
                raise IndyIssuerError(
                    "Revocation registry definition private key not found"
                )
            if not rev_reg_rec:
                raise IndyIssuerError("Revocation registry not found")
            if not rev_reg_info_rec:
                raise IndyIssuerError("Revocation registry metadata not found")

            try:
                cred_def_obj = CredentialDefinition.load(cred_def_rec.raw_value)
            except CredxError as err:
                raise IndyIssuerError(
                    "Error loading credential definition"
                ) from err
            try:
                rev_reg_def_obj = RevocationRegistryDefinition.load(
                    rev_reg_def_rec.raw_value
                )
            except CredxError as err:
                raise IndyIssuerError(
                    "Error loading revocation registry definition"
                ) from err
            try:
                rev_reg_def_priv_obj = RevocationRegistryDefinitionPrivate.load(
                    rev_reg_def_priv_rec.raw_value
                )
            except CredxError as err:
                raise IndyIssuerError(
                    "Error loading revocation registry private key"
                ) from err
            try:
                rev_reg_obj = RevocationRegistry.load(rev_reg_rec.raw_value)
            except CredxError as err:
                raise IndyIssuerError(
                    "Error loading revocation registry"
                ) from err

            rev_crids: set = set()
            failed_crids = set()
            max_cred_num = rev_reg_def_obj.max_cred_num
            rev_info = rev_reg_info_rec.value_json
            used_ids = set(rev_info.get("used_ids") or [])

            for rev_id in cred_revoc_ids:
                rev_id = int(rev_id)
                if rev_id < 1 or rev_id > max_cred_num:
                    LOGGER.error(
                        "Skipping requested credential revocation"
                        " on rev reg id %s, cred rev id=%s not in range",
                        revoc_reg_id,
                        rev_id,
                    )
                    failed_crids.add(rev_id)
                elif rev_id > rev_info["curr_id"]:
                    LOGGER.warning(
                        "Skipping requested credential revocation"
                        " on rev reg id %s, cred rev id=%s not yet issued",
                        revoc_reg_id,
                        rev_id,
                    )
                    failed_crids.add(rev_id)
                elif rev_id in used_ids:
                    LOGGER.warning(
                        "Skipping requested credential revocation"
                        " on rev reg id %s, cred rev id=%s already revoked",
                        revoc_reg_id,
                        rev_id,
                    )
                    failed_crids.add(rev_id)
                else:
                    rev_crids.add(rev_id)

            if not rev_crids:
                break

            try:
                delta = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: rev_reg_obj.update(
                        cred_def_obj,
                        rev_reg_def_obj,
                        rev_reg_def_priv_obj,
                        issued=None,
                        revoked=list(rev_crids),
                    ),
                )
            except CredxError as err:
                raise IndyIssuerError(
                    "Error updating revocation registry"
                ) from err

            async with self._profile.transaction() as txn:
                rev_reg_upd = await txn.handle.fetch(
                    CATEGORY_REV_REG, revoc_reg_id, for_update=True
                )
                rev_info_upd = await txn.handle.fetch(
                    CATEGORY_REV_REG_INFO, revoc_reg_id, for_update=True
                )
                if not rev_reg_upd or not rev_info_upd:
                    LOGGER.warning(
                        "Revocation registry missing, skipping update: %s",
                        revoc_reg_id,
                    )
                    delta = None
                    break
                rev_info_upd_json = rev_info_upd.value_json
                if rev_info_upd_json != rev_info:
                    # Concurrent update — retry from the top.
                    continue
                await txn.handle.replace(
                    CATEGORY_REV_REG,
                    revoc_reg_id,
                    rev_reg_obj.to_json_buffer(),
                )
                used_ids.update(rev_crids)
                rev_info_upd_json["used_ids"] = sorted(used_ids)
                await txn.handle.replace(
                    CATEGORY_REV_REG_INFO,
                    revoc_reg_id,
                    value_json=rev_info_upd_json,
                )
                await txn.commit()
            break

        return (
            delta and delta.to_json(),
            [str(rev_id) for rev_id in sorted(failed_crids)],
        )

    async def merge_revocation_registry_deltas(
        self, fro_delta: str, to_delta: str
    ) -> str:
        """Merge two revocation registry deltas."""

        def update(d1, d2):
            try:
                merged = RevocationRegistryDelta.load(d1)
                merged.update_with(d2)
                return merged.to_json()
            except CredxError as err:
                raise IndyIssuerError(
                    "Error merging revocation registry deltas"
                ) from err

        return await asyncio.get_event_loop().run_in_executor(
            None, update, fro_delta, to_delta
        )

    async def create_and_store_revocation_registry(
        self,
        origin_did: str,
        cred_def_id: str,
        revoc_def_type: str,
        tag: str,
        max_cred_num: int,
        tails_base_path: str,
    ) -> Tuple[str, str, str]:
        """Create a new revocation registry and store it in the wallet.

        Returns ``(rev_reg_def_id, rev_reg_def_json, rev_reg_json)``.
        Persists rev_reg, rev_reg_info (curr_id=0, used_ids=[]),
        rev_reg_def, rev_reg_def_private.
        """
        async with self._profile.session() as session:
            cred_def = await session.handle.fetch(CATEGORY_CRED_DEF, cred_def_id)
        if not cred_def:
            raise IndyIssuerError(
                "Credential definition not found for revocation registry"
            )

        try:
            (
                rev_reg_def,
                rev_reg_def_private,
                rev_reg,
                _rev_reg_delta,
            ) = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: RevocationRegistryDefinition.create(
                    origin_did,
                    cred_def.raw_value,
                    tag,
                    revoc_def_type,
                    max_cred_num,
                    tails_dir_path=tails_base_path,
                ),
            )
        except CredxError as err:
            raise IndyIssuerError("Error creating revocation registry") from err

        rev_reg_def_id = rev_reg_def.id
        rev_reg_def_json = rev_reg_def.to_json()
        rev_reg_json = rev_reg.to_json()

        async with self._profile.transaction() as txn:
            await txn.handle.insert(
                CATEGORY_REV_REG, rev_reg_def_id, rev_reg_json
            )
            await txn.handle.insert(
                CATEGORY_REV_REG_INFO,
                rev_reg_def_id,
                value_json={"curr_id": 0, "used_ids": []},
            )
            await txn.handle.insert(
                CATEGORY_REV_REG_DEF, rev_reg_def_id, rev_reg_def_json
            )
            await txn.handle.insert(
                CATEGORY_REV_REG_DEF_PRIVATE,
                rev_reg_def_id,
                rev_reg_def_private.to_json_buffer(),
            )
            await txn.commit()

        return (rev_reg_def_id, rev_reg_def_json, rev_reg_json)
