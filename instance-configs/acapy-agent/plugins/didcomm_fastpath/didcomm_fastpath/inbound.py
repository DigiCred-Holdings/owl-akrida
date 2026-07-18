"""Inbound DIDComm v1 unpack fast path backed by the shared connection cache.

The outbound cache already owns the local private X25519 key and remote public
X25519 keys for a connection. This module indexes that same object by the local
recipient verkey and reuses the handles during inbound authcrypt unpack. It
does not cache plaintext, CEKs, nonces, or authentication results.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Optional, Tuple, Union

from aries_askar import Key, KeyAlg, crypto_box
from acapy_agent.connections.base_manager import BaseConnectionManager
from acapy_agent.messaging.base_message import DIDCommVersion
from acapy_agent.transport.error import WireFormatParseError
from acapy_agent.transport.inbound.receipt import MessageReceipt
from acapy_agent.transport.pack_format import (
    V1PackWireFormat,
    get_version_for_packed_msg,
)
from acapy_agent.transport.wire_format import BaseWireFormat
from acapy_agent.utils.jwe import JweEnvelope
from acapy_agent.wallet.base import WalletError
from acapy_agent.wallet.crypto import extract_pack_recipients

from .core import (
    STATE,
    CachedConnectionCrypto,
    cache_key,
    resolve_target,
    wallet_id_for_profile,
)

LOGGER = logging.getLogger(__name__)
_WARMING_RECIPIENTS: set[str] = set()
_ORIGINAL_FIND_INBOUND_CONNECTION = None


@dataclass
class InboundCryptoSnapshot:
    """Temporary references protecting active crypto from TTL disposal."""

    target: CachedConnectionCrypto
    local_xk: Key
    remotes: dict[str, Key]


def inbound_enabled() -> bool:
    """Whether shared-cache inbound unpack is enabled (default true)."""
    return os.environ.get("FASTPATH_INBOUND", "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def inbound_warm_on_miss() -> bool:
    """Whether the first stock unpack should populate the shared object."""
    return os.environ.get("FASTPATH_INBOUND_WARM_ON_MISS", "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def _remote_crypto(
    candidates: list[InboundCryptoSnapshot], sender_verkey: str
) -> Optional[Tuple[InboundCryptoSnapshot, Key]]:
    """Find the cached connection and remote public key authenticated by JWE."""
    for candidate in candidates:
        remote_xk = candidate.remotes.get(sender_verkey)
        if remote_xk is not None:
            return candidate, remote_xk
    return None


def _unpack_cached_sync(
    wrapper: JweEnvelope,
    candidates_by_recipient: list[
        Tuple[str, dict, list[InboundCryptoSnapshot]]
    ],
) -> Optional[Tuple[str, str, Optional[str], CachedConnectionCrypto]]:
    """Decrypt one DIDComm v1 envelope from shared cached key handles.

    Returns ``(plaintext, recipient_vk, sender_vk, connection)``. ``None``
    means no complete cached connection matched and the caller must use stock
    wallet unpack. Crypto failures on a matched object are raised, never
    converted into an unauthenticated success.
    """
    alg = wrapper.protected.get("alg")
    is_authcrypt = alg == "Authcrypt"
    if not is_authcrypt and alg != "Anoncrypt":
        raise WalletError(f"Unsupported pack algorithm: {alg}")

    for recipient_verkey, recipient_data, candidates in candidates_by_recipient:
        if not candidates:
            continue

        # Every candidate indexed under this recipient verkey owns equivalent
        # local private key material. Pairwise connections normally have one.
        local_xk = candidates[0].local_xk

        sender_verkey = None
        matched = candidates[0]
        if recipient_data["nonce"] and recipient_data["sender"]:
            sender_verkey = crypto_box.crypto_box_seal_open(
                local_xk, recipient_data["sender"]
            ).decode("utf-8")
            remote_match = _remote_crypto(candidates, sender_verkey)
            if not remote_match:
                # The local key is known but this remote sender's connection is
                # not warm yet. Stock unpack will validate it and warm the
                # correct shared object.
                continue
            matched, remote_xk = remote_match
            payload_key = crypto_box.crypto_box_open(
                matched.local_xk,
                remote_xk,
                recipient_data["key"],
                recipient_data["nonce"],
            )
        else:
            if is_authcrypt:
                raise WalletError("Sender public key not provided for Authcrypt message")
            payload_key = crypto_box.crypto_box_seal_open(
                local_xk, recipient_data["key"]
            )

        cek = Key.from_secret_bytes(KeyAlg.C20P, payload_key)
        plaintext = cek.aead_decrypt(
            wrapper.ciphertext,
            nonce=wrapper.iv,
            tag=wrapper.tag,
            aad=wrapper.protected_bytes,
        )
        return (
            plaintext.decode("utf-8"),
            recipient_verkey,
            sender_verkey,
            matched.target,
        )

    return None


async def unpack_from_shared_cache(
    profile, message_body: Union[str, bytes]
) -> Optional[Tuple[str, Optional[str], str, object, bool]]:
    """Try shared-cache unpack without opening/fetching another wallet key."""
    try:
        wrapper = JweEnvelope.from_json(message_body)
        recipients = extract_pack_recipients(wrapper.recipients)
    except Exception as err:
        raise WalletError("Invalid packed message") from err

    wallet_id = wallet_id_for_profile(profile)
    candidates_by_recipient = []
    for recipient_vk in recipients:
        snapshots = []
        for target in STATE.get_by_recipient(wallet_id, recipient_vk):
            if target.sender_xk is None:
                continue
            snapshots.append(
                InboundCryptoSnapshot(
                    target=target,
                    local_xk=target.sender_xk,
                    remotes={
                        remote.verkey: remote.xk
                        for remote in target.recipients
                        if remote.xk is not None
                    },
                )
            )
        candidates_by_recipient.append(
            (recipient_vk, recipients[recipient_vk], snapshots)
        )
    if not any(candidates for _, _, candidates in candidates_by_recipient):
        return None

    started = time.perf_counter_ns()
    try:
        # These native decrypt operations are short; an executor hop costs more
        # than it saves and reduced measured inbound throughput substantially.
        result = _unpack_cached_sync(wrapper, candidates_by_recipient)
    except Exception as err:
        if isinstance(err, WalletError):
            raise
        raise WalletError("Cached message unpack failed") from err

    if result is None:
        return None

    plaintext, recipient_vk, sender_vk, matched = result
    # Touch and verify the exact connection selected by authenticated sender
    # before exposing its cached record to the dispatcher lookup shortcut.
    # Anoncrypt has no authenticated sender, so never attach a record for it.
    live = STATE.get(matched.wallet_id, matched.connection_id)
    STATE.record("inbound_unpack", time.perf_counter_ns() - started)
    authenticated = sender_vk is not None and live is matched
    connection_record = matched.connection_record if authenticated else None
    return (
        plaintext,
        sender_vk,
        recipient_vk,
        connection_record,
        matched.recipient_did_public if authenticated else False,
    )


async def warm_shared_cache(profile, receipt: MessageReceipt) -> None:
    """Populate the unified object after a correct stock cold unpack."""
    if not (
        inbound_warm_on_miss()
        and receipt.sender_verkey
        and receipt.recipient_verkey
    ):
        return
    warm_key = cache_key(
        wallet_id_for_profile(profile), receipt.recipient_verkey
    )
    if warm_key in _WARMING_RECIPIENTS:
        return
    _WARMING_RECIPIENTS.add(warm_key)
    try:
        manager = profile.inject(BaseConnectionManager)
        connection = await manager.find_inbound_connection(receipt)
        if connection:
            # The stock unpack authenticated these keys and the manager resolved
            # the record. Let the current dispatcher invocation reuse it too.
            receipt._fastpath_connection_record = connection
            receipt._fastpath_recipient_did_public = bool(
                receipt.recipient_did_public
            )
            await resolve_target(profile, connection.connection_id)
    except Exception:
        # Cache warming is optional and must never reject a valid inbound.
        LOGGER.debug("didcomm_fastpath: inbound cold-cache warm failed", exc_info=True)
    finally:
        _WARMING_RECIPIENTS.discard(warm_key)


class FastpathV1PackWireFormat(V1PackWireFormat):
    """Stock V1 parser with shared-object cached unpack on the hot path."""

    async def unpack(self, session, message_body, receipt):
        """Use cached handles when available, otherwise stock wallet unpack."""
        try:
            unpacked = await unpack_from_shared_cache(session.profile, message_body)
        except WalletError as err:
            raise WireFormatParseError("Message unpack failed") from err

        if unpacked is not None:
            (
                message_json,
                receipt.sender_verkey,
                receipt.recipient_verkey,
                connection_record,
                recipient_did_public,
            ) = unpacked
            if connection_record is not None:
                receipt._fastpath_connection_record = connection_record
                receipt._fastpath_recipient_did_public = recipient_did_public
            return message_json

        started = time.perf_counter_ns()
        message_json = await super().unpack(session, message_body, receipt)
        STATE.record("inbound_unpack_cold", time.perf_counter_ns() - started)
        await warm_shared_cache(session.profile, receipt)
        return message_json


class FastpathPackWireFormat(BaseWireFormat):
    """Wire-format decorator preserving stock encode and DIDComm v2 behavior."""

    def __init__(self, original: BaseWireFormat):
        self.original = original
        self.v1 = FastpathV1PackWireFormat()

    async def parse_message(self, session, message_body):
        if session.profile.settings.get("experiment.didcomm_v2"):
            try:
                if get_version_for_packed_msg(message_body) != DIDCommVersion.v1:
                    return await self.original.parse_message(session, message_body)
            except ValueError:
                return await self.original.parse_message(session, message_body)
        return await self.v1.parse_message(session, message_body)

    async def encode_message(
        self,
        session,
        message_json,
        recipient_keys,
        routing_keys,
        sender_key,
    ):
        return await self.original.encode_message(
            session, message_json, recipient_keys, routing_keys, sender_key
        )

    def get_recipient_keys(self, message_body):
        return self.original.get_recipient_keys(message_body)


def install_inbound_wire_format(context) -> bool:
    """Replace the root wire-format binding with the inbound decorator once."""
    if not inbound_enabled():
        LOGGER.info("didcomm_fastpath: inbound fast path disabled")
        return False
    original = context.inject(BaseWireFormat)
    if isinstance(original, FastpathPackWireFormat):
        return False
    context.injector.bind_instance(
        BaseWireFormat, FastpathPackWireFormat(original)
    )
    install_connection_lookup_fastpath()
    LOGGER.info("didcomm_fastpath: installed shared-cache inbound wire format")
    return True


def install_connection_lookup_fastpath() -> bool:
    """Reuse the authenticated cached ConnRecord in ACA-Py's dispatcher.

    ACA-Py constructs ``BaseConnectionManager`` directly and, even on its own
    verkey-cache hit, opens a wallet session to retrieve the same ConnRecord for
    every inbound message. The wire fast path attaches a record only after
    successful authcrypt sender matching. All other receipts delegate unchanged
    to ACA-Py's original method.
    """
    global _ORIGINAL_FIND_INBOUND_CONNECTION

    current = BaseConnectionManager.find_inbound_connection
    if getattr(current, "_didcomm_fastpath", False):
        return False
    _ORIGINAL_FIND_INBOUND_CONNECTION = current

    async def find_inbound_connection_fastpath(manager, receipt):
        connection = getattr(receipt, "_fastpath_connection_record", None)
        if connection is not None:
            # Preserve the receipt fields normally populated by stock lookup.
            receipt.sender_did = getattr(connection, "their_did", None)
            receipt.recipient_did = getattr(connection, "my_did", None)
            receipt.recipient_did_public = bool(
                getattr(receipt, "_fastpath_recipient_did_public", False)
            )
            return connection
        return await _ORIGINAL_FIND_INBOUND_CONNECTION(manager, receipt)

    find_inbound_connection_fastpath._didcomm_fastpath = True
    BaseConnectionManager.find_inbound_connection = (
        find_inbound_connection_fastpath
    )
    return True
