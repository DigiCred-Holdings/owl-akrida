"""DIDComm v1 pack/unpack."""

from __future__ import annotations

import asyncio
import json
import logging
from collections import OrderedDict
from typing import Optional, Sequence, Tuple

from acapy_agent.wallet.error import WalletError, WalletNotFoundError
from acapy_agent.wallet.util import b58_to_bytes, b64_to_bytes, bytes_to_b64
from aries_askar import Key, KeyAlg, crypto_box

LOGGER = logging.getLogger(__name__)

ENC_TYP = "xchacha20poly1305_ietf"
ALG_AUTHCRYPT = "Authcrypt"
ALG_ANONCRYPT = "Anoncrypt"


def _b64u(value: bytes) -> str:
    """base64url encode without padding (the JWE convention)."""
    return bytes_to_b64(value, urlsafe=True, pad=False)


def _from_b64u(value: str) -> bytes:
    return b64_to_bytes(value, urlsafe=True)


async def pack_message(
    wallet,
    message: str,
    to_verkeys: Sequence[str],
    from_verkey: Optional[str] = None,
) -> bytes:
    """Pack a JSON message string into a DIDComm v1 JWE envelope."""

    # Pre-fetch sender secret synchronously (the heavy crypto runs in a
    # thread executor and must not touch the async DB session).
    sender_secret: Optional[bytes] = None
    if from_verkey:
        secret, _, _, _ = await wallet._keystore.fetch(wallet._sa, from_verkey)
        sender_secret = secret

    return await asyncio.get_event_loop().run_in_executor(
        None, _pack_sync, message, list(to_verkeys), from_verkey, sender_secret
    )


def _pack_sync(
    message: str,
    to_verkeys: Sequence[str],
    from_verkey: Optional[str],
    sender_secret: Optional[bytes],
) -> bytes:
    """Synchronous core of pack_message; runs in an executor."""
    if (from_verkey is None) != (sender_secret is None):
        raise WalletError("from_verkey and sender_secret must be both set or both None")

    cek = Key.generate(KeyAlg.C20P)
    cek_bytes = bytes(cek.get_secret_bytes())

    sender_xk: Optional[Key] = None
    sender_vk_bytes: Optional[bytes] = None
    if from_verkey:
        sender_ed = Key.from_secret_bytes(KeyAlg.ED25519, sender_secret)
        sender_xk = sender_ed.convert_key(KeyAlg.X25519)
        sender_vk_bytes = from_verkey.encode("utf-8")

    recipients = []
    for target_vk in to_verkeys:
        target_ed = Key.from_public_bytes(KeyAlg.ED25519, b58_to_bytes(target_vk))
        target_xk = target_ed.convert_key(KeyAlg.X25519)

        if sender_xk is not None:
            enc_sender = crypto_box.crypto_box_seal(target_xk, sender_vk_bytes)
            nonce = crypto_box.random_nonce()
            enc_cek = crypto_box.crypto_box(target_xk, sender_xk, cek_bytes, nonce)
            recipients.append(
                OrderedDict(
                    [
                        ("encrypted_key", _b64u(enc_cek)),
                        (
                            "header",
                            OrderedDict(
                                [
                                    ("kid", target_vk),
                                    ("sender", _b64u(enc_sender)),
                                    ("iv", _b64u(nonce)),
                                ]
                            ),
                        ),
                    ]
                )
            )
        else:
            enc_cek = crypto_box.crypto_box_seal(target_xk, cek_bytes)
            recipients.append(
                OrderedDict(
                    [
                        ("encrypted_key", _b64u(enc_cek)),
                        ("header", OrderedDict([("kid", target_vk)])),
                    ]
                )
            )

    protected = OrderedDict(
        [
            ("enc", ENC_TYP),
            ("typ", "JWM/1.0"),
            ("alg", ALG_AUTHCRYPT if sender_xk else ALG_ANONCRYPT),
            ("recipients", recipients),
        ]
    )
    protected_b64 = _b64u(json.dumps(protected).encode("utf-8"))
    aad = protected_b64.encode("ascii")

    enc = cek.aead_encrypt(message.encode("utf-8"), aad=aad)
    ciphertext, tag, nonce = enc.parts

    envelope = OrderedDict(
        [
            ("protected", protected_b64),
            ("iv", _b64u(bytes(nonce))),
            ("ciphertext", _b64u(bytes(ciphertext))),
            ("tag", _b64u(bytes(tag))),
        ]
    )
    return json.dumps(envelope).encode("utf-8")


async def unpack_message(wallet, enc_message: bytes) -> Tuple[str, str, str]:
    """Decrypt a DIDComm v1 JWE envelope.

    Returns ``(message, sender_verkey, recipient_verkey)``. ``sender_verkey``
    is an empty string for Anoncrypt envelopes.
    """
    try:
        envelope = json.loads(enc_message)
    except (TypeError, ValueError) as err:
        raise WalletError("Invalid JWE: not JSON") from err

    try:
        protected_b64 = envelope["protected"]
        iv = _from_b64u(envelope["iv"])
        ciphertext = _from_b64u(envelope["ciphertext"])
        tag = _from_b64u(envelope["tag"])
        protected = json.loads(_from_b64u(protected_b64))
        alg = protected["alg"]
        recipients = protected["recipients"]
    except (KeyError, TypeError, ValueError) as err:
        raise WalletError("Invalid JWE: malformed envelope") from err

    if alg not in (ALG_AUTHCRYPT, ALG_ANONCRYPT):
        raise WalletError(f"Unsupported pack algorithm: {alg!r}")

    found = None
    for recip in recipients:
        kid = recip.get("header", {}).get("kid")
        if not kid:
            continue
        try:
            secret, _alg_name, _md, _kid = await wallet._keystore.fetch(
                wallet._sa, kid
            )
        except WalletNotFoundError:
            # `kid` simply isn't a key we hold — try the next recipient.
            continue
        except Exception:
            # Something genuinely went wrong (DB blip, AEAD tag mismatch,
            # decryption failure). Surface it instead of silently treating
            # it as "not our key" — otherwise an operational outage looks
            # like a routing error far from the cause.
            LOGGER.warning(
                "unpack_message: keystore fetch failed for kid=%r",
                kid,
                exc_info=True,
            )
            raise
        found = (kid, secret, recip)
        break

    if not found:
        raise WalletError(
            "No corresponding recipient key found in {}".format(
                tuple(r.get("header", {}).get("kid") for r in recipients)
            )
        )

    kid, recip_secret, recip = found

    return await asyncio.get_event_loop().run_in_executor(
        None,
        _unpack_sync,
        ciphertext,
        iv,
        tag,
        protected_b64,
        alg,
        recip,
        kid,
        recip_secret,
    )


def _unpack_sync(
    ciphertext: bytes,
    iv: bytes,
    tag: bytes,
    protected_b64: str,
    alg: str,
    recip: dict,
    kid: str,
    recip_secret: bytes,
) -> Tuple[str, str, str]:
    recipient_ed = Key.from_secret_bytes(KeyAlg.ED25519, recip_secret)
    recipient_xk = recipient_ed.convert_key(KeyAlg.X25519)

    encrypted_key = _from_b64u(recip["encrypted_key"])
    header = recip.get("header", {})

    if alg == ALG_AUTHCRYPT:
        try:
            sender_seal = _from_b64u(header["sender"])
            box_nonce = _from_b64u(header["iv"])
        except KeyError as err:
            raise WalletError("Authcrypt recipient missing sender/iv") from err
        sender_vk_bytes = crypto_box.crypto_box_seal_open(recipient_xk, sender_seal)
        sender_vk = bytes(sender_vk_bytes).decode("utf-8")
        sender_ed = Key.from_public_bytes(KeyAlg.ED25519, b58_to_bytes(sender_vk))
        sender_xk = sender_ed.convert_key(KeyAlg.X25519)
        cek_bytes = crypto_box.crypto_box_open(
            recipient_xk, sender_xk, encrypted_key, box_nonce
        )
    else:
        cek_bytes = crypto_box.crypto_box_seal_open(recipient_xk, encrypted_key)
        sender_vk = ""

    cek = Key.from_secret_bytes(KeyAlg.C20P, bytes(cek_bytes))
    plaintext = cek.aead_decrypt(
        ciphertext, nonce=iv, tag=tag, aad=protected_b64.encode("ascii")
    )
    return bytes(plaintext).decode("utf-8"), sender_vk, kid
