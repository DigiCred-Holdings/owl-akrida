"""aries_askar primitive wrappers — function-level only, no Store."""

from __future__ import annotations

from typing import List, Optional, Union

from acapy_agent.wallet.key_type import (
    BLS12381G2,
    ED25519,
    KeyType,
    P256,
    X25519,
)
from acapy_agent.wallet.util import b58_to_bytes, bytes_to_b58
from aries_askar import Key, KeyAlg
from aries_askar import SeedMethod

from kanon_storage.v1_0.wallet.errors import WalletKeyTypeError

_KEY_TYPE_TO_ALG: dict[str, KeyAlg] = {
    ED25519.key_type: KeyAlg.ED25519,
    P256.key_type: KeyAlg.P256,
    X25519.key_type: KeyAlg.X25519,
    BLS12381G2.key_type: KeyAlg.BLS12_381_G2,
}

_ALG_TO_KEY_TYPE: dict[str, KeyType] = {
    alg.value: kt for kt, alg in _KEY_TYPE_TO_ALG.items()
}
_ALG_NAME_TO_KEY_TYPE: dict[str, KeyType] = {
    "ed25519": ED25519,
    "p256": P256,
    "x25519": X25519,
    "bls12381g2": BLS12381G2,
}
_KEY_TYPE_TO_ALG_NAME: dict[str, str] = {
    ED25519.key_type: "ed25519",
    P256.key_type: "p256",
    X25519.key_type: "x25519",
    BLS12381G2.key_type: "bls12381g2",
}


def alg_for_key_type(key_type: KeyType) -> KeyAlg:
    try:
        return _KEY_TYPE_TO_ALG[key_type.key_type]
    except KeyError as exc:
        raise WalletKeyTypeError(f"Unsupported key type: {key_type.key_type!r}") from exc


def alg_name(key_type: KeyType) -> str:
    """Stable lowercase name used as the DB `key_alg` column value."""
    try:
        return _KEY_TYPE_TO_ALG_NAME[key_type.key_type]
    except KeyError as exc:
        raise WalletKeyTypeError(f"Unsupported key type: {key_type.key_type!r}") from exc


def key_type_from_alg_name(name: str) -> KeyType:
    try:
        return _ALG_NAME_TO_KEY_TYPE[name]
    except KeyError as exc:
        raise WalletKeyTypeError(f"Unknown stored key alg: {name!r}") from exc


def create_keypair(key_type: KeyType, seed: Optional[Union[str, bytes]] = None) -> Key:
    """Create a Key (private+public). With no seed: random. With seed: deterministic."""
    alg = alg_for_key_type(key_type)
    if seed is None:
        return Key.generate(alg)
    seed_bytes = seed.encode("utf-8") if isinstance(seed, str) else seed
    if alg == KeyAlg.BLS12_381_G2:
        return Key.from_seed(alg, seed_bytes, method=SeedMethod.BlsKeyGen)
    return Key.from_secret_bytes(alg, seed_bytes)


def public_key_from_verkey(verkey: str, key_type: KeyType) -> Key:
    """Construct a public-only Key from a base58 verkey (for verify)."""
    alg = alg_for_key_type(key_type)
    return Key.from_public_bytes(alg, b58_to_bytes(verkey))


def verkey_for(key: Key) -> str:
    return bytes_to_b58(key.get_public_bytes())


def secret_bytes_for(key: Key) -> bytes:
    return bytes(key.get_secret_bytes())


def key_from_secret(alg_name_str: str, secret: bytes) -> Key:
    """Reconstruct a Key from stored secret bytes."""
    key_type = key_type_from_alg_name(alg_name_str)
    alg = alg_for_key_type(key_type)
    return Key.from_secret_bytes(alg, secret)


def sign(key: Key, message: Union[bytes, List[bytes]]) -> bytes:
    """Sign message bytes with the private key."""
    return bytes(key.sign_message(message))


def verify(public_key: Key, message: Union[bytes, List[bytes]], signature: bytes) -> bool:
    return bool(public_key.verify_signature(message, signature))
