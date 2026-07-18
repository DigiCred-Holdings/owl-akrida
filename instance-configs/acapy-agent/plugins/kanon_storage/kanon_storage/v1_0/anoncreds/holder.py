"""KanonIndyHolder — IndyHolder implementation."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Dict, Optional, Sequence, Tuple, Union

from acapy_agent.core.profile import Profile
from acapy_agent.indy.holder import IndyHolder, IndyHolderError
from acapy_agent.ledger.base import BaseLedger
from indy_credx import (
    Credential,
    CredentialDefinition,
    CredentialOffer,
    CredentialRequest,
    CredentialRevocationState,
    CredxError,
    LinkSecret,
    PresentCredentials,
    Presentation,
    PresentationRequest,
    Schema,
)
from uuid_utils import uuid4

from kanon_storage.v1_0.anoncreds.categories import (
    CATEGORY_CREDENTIAL,
    CATEGORY_LINK_SECRET,
    CATEGORY_MIME_TYPES,
    LINK_SECRET_ID,
)
from kanon_storage.v1_0.storage.errors import (
    RecordDuplicateError,
    RecordNotFoundError,
    StorageError,
)

LOGGER = logging.getLogger(__name__)


def _make_cred_info(cred_id: str, cred: Credential) -> dict:
    """Build the cred_info dict shape ACA-Py callers expect."""
    cred_dict = cred.to_dict()
    rev_info = cred_dict["signature"]["r_credential"]
    return {
        "referent": cred_id,
        "schema_id": cred_dict["schema_id"],
        "cred_def_id": cred_dict["cred_def_id"],
        "rev_reg_id": cred_dict["rev_reg_id"],
        "cred_rev_id": str(rev_info["i"]) if rev_info else None,
        "attrs": {name: val["raw"] for (name, val) in cred_dict["values"].items()},
    }


def _normalize_attr_name(name: str) -> str:
    """Indy/AnonCreds attribute-name canonicalisation.

    Matches `acapy_agent.indy.util.canon` semantics: lowercase + strip
    *all* whitespace categories (not just space). The previous
    "strip spaces only" diverged from the upstream presentation flow,
    so cred lookups for attribute names containing tabs / mixed case
    silently missed.
    """
    try:
        from acapy_agent.indy.util import canon

        return canon(name)
    except Exception:
        # Fallback for environments where the helper has moved.
        return "".join(name.lower().split())


class KanonIndyHolder(IndyHolder):
    """IndyHolder over our session.handle shim — mirrors IndyCredxHolder."""

    LINK_SECRET_ID = LINK_SECRET_ID

    def __init__(self, profile: Profile):
        self._profile = profile

    @property
    def profile(self) -> Profile:
        return self._profile

    async def get_link_secret(self) -> LinkSecret:
        """Get or create the default link secret."""
        while True:
            async with self._profile.transaction() as session:
                try:
                    record = await session.handle.fetch(
                        CATEGORY_LINK_SECRET, self.LINK_SECRET_ID
                    )
                except Exception as err:
                    raise IndyHolderError("Error fetching link secret") from err

                if record:
                    try:
                        secret = LinkSecret.load(record.raw_value)
                    except CredxError:
                        try:
                            ms_string = record.raw_value.decode("ascii")
                            secret = LinkSecret.load({"value": {"ms": ms_string}})
                        except CredxError as decode_err:
                            raise IndyHolderError(
                                "Error loading link secret"
                            ) from decode_err
                    await session.commit()
                    return secret

                try:
                    secret = LinkSecret.create()
                except CredxError as err:
                    raise IndyHolderError("Error creating link secret") from err

                try:
                    await session.handle.insert(
                        CATEGORY_LINK_SECRET,
                        self.LINK_SECRET_ID,
                        secret.to_json_buffer(),
                    )
                except RecordDuplicateError:
                    # Lost the race — roll back and retry to load the winner.
                    await session.rollback()
                    continue
                except Exception as err:
                    raise IndyHolderError("Error saving link secret") from err

                await session.commit()
                return secret

    async def create_credential_request(
        self, credential_offer: dict, credential_definition: dict, holder_did: str
    ) -> Tuple[str, str]:
        link_secret = await self.get_link_secret()
        try:
            cred_def_obj = (
                credential_definition
                if not isinstance(credential_definition, dict)
                else CredentialDefinition.load(json.dumps(credential_definition))
            )
            offer_obj = (
                credential_offer
                if not isinstance(credential_offer, dict)
                else CredentialOffer.load(json.dumps(credential_offer))
            )
            (
                cred_req,
                cred_req_meta,
            ) = await asyncio.get_event_loop().run_in_executor(
                None,
                CredentialRequest.create,
                holder_did,
                cred_def_obj,
                link_secret,
                self.LINK_SECRET_ID,
                offer_obj,
            )
        except CredxError as err:
            raise IndyHolderError("Error creating credential request") from err
        return cred_req.to_json(), cred_req_meta.to_json()

    async def store_credential(
        self,
        credential_definition: dict,
        credential_data: dict,
        credential_request_metadata: dict,
        credential_attr_mime_types: Optional[dict] = None,
        credential_id: Optional[str] = None,
        rev_reg_def: Optional[dict] = None,
    ) -> str:
        link_secret = await self.get_link_secret()
        try:
            cred = Credential.load(
                credential_data
                if not isinstance(credential_data, dict)
                else json.dumps(credential_data)
            )
            cred_def_obj = (
                credential_definition
                if not isinstance(credential_definition, dict)
                else CredentialDefinition.load(json.dumps(credential_definition))
            )
            req_meta = (
                json.loads(credential_request_metadata)
                if isinstance(credential_request_metadata, str)
                else credential_request_metadata
            )
            cred_recvd = await asyncio.get_event_loop().run_in_executor(
                None,
                cred.process,
                req_meta,
                link_secret,
                cred_def_obj,
                rev_reg_def,
            )
        except CredxError as err:
            raise IndyHolderError("Error processing received credential") from err

        # Parse schema_id + cred_def_id to derive the 7 indexable tags ACA-Py's
        # IndyCredxHolder writes. Schema is the standard legacy form
        # `<did>:2:<name>:<version>`; cred_def is `<issuer_did>:3:CL:<schema_ref>:<tag>`,
        # but `<schema_ref>` may itself be a full schema_id with embedded colons
        # (for non-ledger schemas), so we extract issuer_did via prefix-split on
        # ":3:CL:" rather than a strict colon-count regex.
        schema_id = cred_recvd.schema_id
        schema_match = re.match(r"^(\w+):2:([^:]+):([^:]+)$", schema_id)
        if not schema_match:
            raise IndyHolderError(f"Error parsing credential schema ID: {schema_id}")
        cred_def_id = cred_recvd.cred_def_id
        if ":3:CL:" not in cred_def_id:
            raise IndyHolderError(
                f"Error parsing credential definition ID: {cred_def_id}"
            )
        issuer_did = cred_def_id.split(":3:CL:", 1)[0]

        credential_id = credential_id or str(uuid4())
        tags = {
            "schema_id": schema_id,
            "schema_issuer_did": schema_match[1],
            "schema_name": schema_match[2],
            "schema_version": schema_match[3],
            "issuer_did": issuer_did,
            "cred_def_id": cred_def_id,
            "rev_reg_id": cred_recvd.rev_reg_id or "None",
        }

        cred_values = (
            credential_data["values"]
            if isinstance(credential_data, dict)
            else json.loads(credential_data)["values"]
        )
        mime_types = {}
        for k, attr_value in cred_values.items():
            attr_name = _normalize_attr_name(k)
            tags[f"attr::{attr_name}::value"] = attr_value["raw"]
            if credential_attr_mime_types and k in credential_attr_mime_types:
                mime_types[k] = credential_attr_mime_types[k]

        try:
            async with self._profile.transaction() as txn:
                await txn.handle.insert(
                    CATEGORY_CREDENTIAL,
                    credential_id,
                    cred_recvd.to_json_buffer(),
                    tags=tags,
                )
                if mime_types:
                    await txn.handle.insert(
                        CATEGORY_MIME_TYPES,
                        credential_id,
                        value_json=mime_types,
                    )
                await txn.commit()
        except Exception as err:
            raise IndyHolderError("Error storing credential") from err

        return credential_id

    async def get_credentials(
        self, *, offset: int = 0, limit: int = 0, wql: Optional[dict] = None
    ):
        """Return list of cred_info dicts matching `wql` (paginated).

        Mirrors IndyCredxHolder.get_credentials. Used by /credentials and
        the proof flow when no specific referent matters.
        """
        result = []
        try:
            async with self._profile.session() as session:
                rows = await session.handle.fetch_all(
                    CATEGORY_CREDENTIAL,
                    wql or {},
                    limit=limit or None,
                    offset=offset or None,
                )
        except Exception as err:
            raise IndyHolderError("Error retrieving credentials") from err

        for row in rows:
            try:
                cred = Credential.load(row.raw_value)
            except CredxError as err:
                raise IndyHolderError("Error loading stored credential") from err
            result.append(_make_cred_info(row.name, cred))
        return result

    async def get_credentials_for_presentation_request_by_referent(
        self,
        presentation_request: dict,
        referents: Sequence[str],
        *,
        offset: int = 0,
        limit: int = 0,
        extra_query: Optional[dict] = None,
    ):
        """Match credentials in wallet against presentation request referents.

        Port of IndyCredxHolder.get_credentials_for_presentation_request_by_referent.
        Builds WQL per-referent from `requested_attributes` / `requested_predicates`,
        unions matches across referents, returns one entry per matched cred_id
        with `presentation_referents` listing all referents it satisfies.
        """
        extra_query = extra_query or {}
        if not referents:
            referents = (
                *presentation_request["requested_attributes"],
                *presentation_request["requested_predicates"],
            )

        creds: dict = {}

        for reft in referents:
            names: set = set()
            if reft in presentation_request["requested_attributes"]:
                attr = presentation_request["requested_attributes"][reft]
                if "name" in attr:
                    names.add(_normalize_attr_name(attr["name"]))
                elif "names" in attr:
                    names.update(_normalize_attr_name(n) for n in attr["names"])
                restr = attr.get("restrictions")
            elif reft in presentation_request["requested_predicates"]:
                pred = presentation_request["requested_predicates"][reft]
                if "name" in pred:
                    names.add(_normalize_attr_name(pred["name"]))
                restr = pred.get("restrictions")
            else:
                raise IndyHolderError(
                    f"Unknown presentation request referent: {reft}"
                )

            tag_filter: dict = {
                "$exist": [f"attr::{name}::value" for name in names]
            }
            if restr:
                tag_filter = {"$and": [tag_filter] + restr}
            if extra_query:
                tag_filter = {"$and": [tag_filter, extra_query]}

            try:
                async with self._profile.session() as session:
                    rows = await session.handle.fetch_all(
                        CATEGORY_CREDENTIAL,
                        tag_filter,
                        limit=limit or None,
                        offset=offset or None,
                    )
            except Exception as err:
                raise IndyHolderError(
                    "Error retrieving credentials for presentation request"
                ) from err

            for row in rows:
                if row.name in creds:
                    creds[row.name]["presentation_referents"].add(reft)
                else:
                    try:
                        cred = Credential.load(row.raw_value)
                    except CredxError as err:
                        raise IndyHolderError(
                            "Error loading stored credential"
                        ) from err
                    creds[row.name] = {
                        "cred_info": _make_cred_info(row.name, cred),
                        "interval": presentation_request.get("non_revoked"),
                        "presentation_referents": {reft},
                    }

        for cred in creds.values():
            cred["presentation_referents"] = list(cred["presentation_referents"])

        return list(creds.values())

    async def get_credential(self, credential_id: str) -> str:
        """Return cred_info JSON (matches IndyCredxHolder shape)."""
        cred = await self._get_credential(credential_id)
        return json.dumps(_make_cred_info(credential_id, cred))

    async def _get_credential(self, credential_id: str) -> Credential:
        """Fetch the underlying Credential object from storage."""
        try:
            async with self._profile.session() as session:
                rec = await session.handle.fetch(CATEGORY_CREDENTIAL, credential_id)
        except Exception as err:
            raise IndyHolderError("Error retrieving credential") from err

        if rec is None:
            raise IndyHolderError(
                f"Credential {credential_id!r} not found in wallet"
            )

        try:
            return Credential.load(rec.raw_value)
        except CredxError as err:
            raise IndyHolderError("Error loading requested credential") from err

    async def credential_revoked(
        self,
        ledger: BaseLedger,
        credential_id: str,
        timestamp_from: Optional[int] = None,
        timestamp_to: Optional[int] = None,
    ) -> bool:
        """Check ledger for revocation status of credential by cred id."""
        cred = await self._get_credential(credential_id)
        rev_reg_id = cred.rev_reg_id
        if not rev_reg_id:
            return False

        cred_rev_id = cred.rev_reg_index
        (rev_reg_delta, _) = await ledger.get_revoc_reg_delta(
            rev_reg_id,
            timestamp_from,
            timestamp_to,
        )
        return cred_rev_id in rev_reg_delta["value"].get("revoked", [])

    async def delete_credential(self, credential_id: str) -> None:
        # Narrow catch: only real storage errors get wrapped under the
        # generic `IndyHolderError("Error deleting credential")`. Letting
        # other exceptions (assertion errors, programming bugs) propagate
        # makes their cause obvious instead of buried in `__cause__`.
        try:
            async with self._profile.transaction() as session:
                # remove() raises RecordNotFoundError if missing — mirror
                # IndyCredxHolder which swallows NOT_FOUND from askar.
                try:
                    await session.handle.remove(CATEGORY_CREDENTIAL, credential_id)
                except RecordNotFoundError:
                    pass
                try:
                    await session.handle.remove(CATEGORY_MIME_TYPES, credential_id)
                except RecordNotFoundError:
                    pass
                await session.commit()
        except StorageError as err:
            raise IndyHolderError("Error deleting credential") from err

    async def get_mime_type(
        self, credential_id: str, attr: Optional[str] = None
    ) -> Union[dict, str, None]:
        try:
            async with self._profile.session() as session:
                rec = await session.handle.fetch(CATEGORY_MIME_TYPES, credential_id)
        except Exception as err:
            raise IndyHolderError("Error retrieving credential mime types") from err
        if rec is None:
            return None
        values = rec.value_json
        if not values:
            return None
        return values.get(attr) if attr else values

    async def create_presentation(
        self,
        presentation_request: dict,
        requested_credentials: dict,
        schemas: dict,
        credential_definitions: dict,
        rev_states: Optional[dict] = None,
    ) -> str:
        """Create a presentation, honoring rev_states when timestamps are set."""
        creds: Dict[str, Credential] = {}

        def get_rev_state(cred_id: str, detail: dict):
            cred = creds[cred_id]
            rev_reg_id = cred.rev_reg_id
            timestamp = detail.get("timestamp") if rev_reg_id else None
            rev_state = None
            if timestamp:
                if not rev_states or rev_reg_id not in rev_states:
                    raise IndyHolderError(
                        f"No revocation states provided for credential '{cred_id}' "
                        f"with rev_reg_id '{rev_reg_id}'"
                    )
                rev_state = rev_states[rev_reg_id].get(timestamp)
                if not rev_state:
                    raise IndyHolderError(
                        f"No revocation states provided for credential '{cred_id}' "
                        f"with rev_reg_id '{rev_reg_id}' at timestamp {timestamp}"
                    )
            return timestamp, rev_state

        self_attest = requested_credentials.get("self_attested_attributes") or {}
        present_creds = PresentCredentials()

        req_attrs = requested_credentials.get("requested_attributes") or {}
        for reft, detail in req_attrs.items():
            cred_id = detail["cred_id"]
            if cred_id not in creds:
                creds[cred_id] = await self._get_credential(cred_id)
            timestamp, rev_state = get_rev_state(cred_id, detail)
            present_creds.add_attributes(
                creds[cred_id],
                reft,
                reveal=detail.get("revealed", True),
                timestamp=timestamp,
                rev_state=rev_state,
            )

        req_preds = requested_credentials.get("requested_predicates") or {}
        for reft, detail in req_preds.items():
            cred_id = detail["cred_id"]
            if cred_id not in creds:
                creds[cred_id] = await self._get_credential(cred_id)
            timestamp, rev_state = get_rev_state(cred_id, detail)
            present_creds.add_predicates(
                creds[cred_id],
                reft,
                timestamp=timestamp,
                rev_state=rev_state,
            )

        try:
            link_secret = await self.get_link_secret()
            # indy_credx Presentation.create accepts native dicts/objects for
            # schemas + cred_defs. Pre-load any raw dicts into typed objects
            # so the FFI layer is happy.
            schemas_objs = [
                Schema.load(json.dumps(s)) if isinstance(s, dict) else s
                for s in schemas.values()
            ]
            cred_defs_objs = [
                CredentialDefinition.load(json.dumps(c)) if isinstance(c, dict) else c
                for c in credential_definitions.values()
            ]
            pres_req = (
                PresentationRequest.load(json.dumps(presentation_request))
                if isinstance(presentation_request, dict)
                else presentation_request
            )
            presentation = await asyncio.get_event_loop().run_in_executor(
                None,
                Presentation.create,
                pres_req,
                present_creds,
                self_attest,
                link_secret,
                schemas_objs,
                cred_defs_objs,
            )
        except CredxError as err:
            raise IndyHolderError("Error creating presentation") from err

        return presentation.to_json()

    async def create_revocation_state(
        self,
        cred_rev_id: str,
        rev_reg_def: dict,
        rev_reg_delta: dict,
        timestamp: int,
        tails_file_path: str,
    ) -> str:
        """Create current revocation state for a received credential."""
        try:
            rev_state = await asyncio.get_event_loop().run_in_executor(
                None,
                CredentialRevocationState.create,
                rev_reg_def,
                rev_reg_delta,
                int(cred_rev_id),
                timestamp,
                tails_file_path,
            )
        except CredxError as err:
            raise IndyHolderError("Error creating revocation state") from err
        return rev_state.to_json()
