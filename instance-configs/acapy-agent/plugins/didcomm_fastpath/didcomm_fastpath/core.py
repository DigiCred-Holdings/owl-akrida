"""Fast-path DIDComm v1 send pipeline.

Per-connection crypto material (endpoint, recipient/routing keys, sender key
converted to X25519, sealed sender blob) is resolved once and cached in memory.
Subsequent sends skip the ConnRecord fetch, the per-send Askar profile session,
the per-send sender-key fetch, and the per-send Ed25519->X25519 conversions.

Supports:
- Basic messages (admin route helper)
- Arbitrary AgentMessage / raw JSON bytes (used by workflow_protocol)

Every message still gets a fresh CEK, fresh nonces, and a fresh AEAD pass, so
wire messages remain protocol-valid DIDComm v1 envelopes.

Cache invalidation: any ConnRecord event (update, DID rotation, deletion)
evicts that connection's cache entry via an event-bus subscription (see
v1_0.__init__.setup). FASTPATH_CACHE_TTL (default 300, 0 disables) is
checked lazily on access; it refreshes active entries but does not sweep idle
ones. DELETE /didcomm-fastpath/cache still clears everything manually.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Union

from aiohttp import ClientSession, DummyCookieJar, TCPConnector
from aries_askar import Key, KeyAlg, crypto_box
from aries_askar.bindings import key_get_secret_bytes

from acapy_agent.connections.base_manager import BaseConnectionManager
from acapy_agent.core.profile import Profile
from acapy_agent.utils.jwe import JweEnvelope, JweRecipient, b64url
from acapy_agent.wallet.util import b58_to_bytes

LOGGER = logging.getLogger(__name__)

BASICMESSAGE_TYPE_NEW = "https://didcomm.org/basicmessage/1.0/message"
BASICMESSAGE_TYPE_OLD = "did:sov:BzCbsNYhMrjHiqZDTUASHg;spec/basicmessage/1.0/message"
DIDCOMM_V0_MIME = "application/ssi-agent-wire"
DIDCOMM_V1_MIME = "application/didcomm-envelope-enc"


@dataclass
class RecipientCrypto:
    """Precomputed crypto material for one recipient verkey."""

    verkey: str
    xk: Key  # recipient X25519 public key
    enc_sender: bytes  # crypto_box_seal(target_xk, sender_vk), reusable


@dataclass
class CachedTarget:
    """Everything needed to pack and deliver to one connection."""

    endpoint: str
    recipients: List[RecipientCrypto]
    routing_recipients: List[RecipientCrypto]  # for mediator forward wrapping
    routing_verkeys: List[str]
    sender_vk_b: bytes  # base58 sender verkey, utf-8 encoded
    sender_xk: Key  # sender X25519 keypair (independent handle)
    cached_at: float = 0.0  # time.monotonic() at cache insert, for TTL expiry


@dataclass
class StageStats:
    """Cumulative timing for one pipeline stage."""

    count: int = 0
    total_ns: int = 0
    min_ns: int = 0
    max_ns: int = 0

    def add(self, ns: int):
        self.count += 1
        self.total_ns += ns
        if self.min_ns == 0 or ns < self.min_ns:
            self.min_ns = ns
        if ns > self.max_ns:
            self.max_ns = ns

    def as_dict(self) -> dict:
        mean = self.total_ns / self.count / 1e6 if self.count else 0.0
        return {
            "count": self.count,
            "total_ms": round(self.total_ns / 1e6, 3),
            "mean_ms": round(mean, 4),
            "min_ms": round(self.min_ns / 1e6, 4),
            "max_ms": round(self.max_ns / 1e6, 4),
        }


class FastpathState:
    """Singleton plugin state: target cache, HTTP session, stage stats."""

    STAGES = ("resolve_cold", "build", "pack", "deliver", "total")

    def __init__(self):
        self.targets: Dict[str, CachedTarget] = {}
        self._http: Optional[ClientSession] = None
        self.stats: Dict[str, StageStats] = {s: StageStats() for s in self.STAGES}
        self.first_send_ts: Optional[float] = None
        self.last_send_ts: Optional[float] = None
        # Optional external timing dict (e.g. workflow_protocol stages)
        self.external_stats: Dict[str, Any] = {}

    def record(self, stage: str, ns: int):
        if stage not in self.stats:
            self.stats[stage] = StageStats()
        self.stats[stage].add(ns)

    def stats_dict(self) -> dict:
        out = {s: st.as_dict() for s, st in self.stats.items()}
        sends = self.stats["total"].count
        if sends and self.first_send_ts and self.last_send_ts != self.first_send_ts:
            window = self.last_send_ts - self.first_send_ts
            out["observed_rps"] = round(sends / window, 2)
            out["window_seconds"] = round(window, 3)
        out["cached_connections"] = len(self.targets)
        out["pack_workers"] = _PACK_POOL._max_workers  # type: ignore[attr-defined]
        if self.external_stats:
            out["workflow"] = self.external_stats
        return out

    def reset_stats(self):
        self.stats = {s: StageStats() for s in self.STAGES}
        self.first_send_ts = None
        self.last_send_ts = None

    def invalidate(self, connection_id: str) -> bool:
        """Drop one connection's cached target (DID rotation, deletion, etc.)."""
        return self.targets.pop(connection_id, None) is not None

    def http(self) -> ClientSession:
        if self._http is None or self._http.closed:
            self._http = ClientSession(
                cookie_jar=DummyCookieJar(),
                connector=TCPConnector(limit=200, limit_per_host=100),
            )
        return self._http


STATE = FastpathState()

_PACK_POOL = ThreadPoolExecutor(
    max_workers=int(os.environ.get("FASTPATH_PACK_WORKERS", "32")),
    thread_name_prefix="fastpath-pack",
)


def cache_ttl_seconds() -> float:
    """TTL for cached targets; fallback safety net behind event-bus eviction.

    0 (or negative) disables expiry.
    """
    try:
        return float(os.environ.get("FASTPATH_CACHE_TTL", "300"))
    except ValueError:
        return 300.0


def deliver_override_endpoint() -> Optional[str]:
    """Optional sink URL that replaces the connection endpoint on deliver."""
    value = (os.environ.get("FASTPATH_DELIVER_OVERRIDE") or "").strip()
    return value or None


def _independent_key(key: Key) -> Key:
    """Return a Key we own, detached from any Askar entry-list buffer."""
    return Key.from_secret_bytes(KeyAlg.ED25519, key_get_secret_bytes(key._handle))


def _recipient_crypto(verkey: str, sender_vk_b: bytes) -> RecipientCrypto:
    xk = Key.from_public_bytes(KeyAlg.ED25519, b58_to_bytes(verkey)).convert_key(
        KeyAlg.X25519
    )
    # The sealed sender blob only conveys the sender verkey; reusing it across
    # messages is protocol-valid and skips one ephemeral keygen+ECDH per message.
    enc_sender = crypto_box.crypto_box_seal(xk, sender_vk_b)
    return RecipientCrypto(verkey=verkey, xk=xk, enc_sender=enc_sender)


async def resolve_target(profile: Profile, connection_id: str) -> CachedTarget:
    """Resolve and cache all send material for a connection (cold path)."""
    cached = STATE.targets.get(connection_id)
    if cached:
        ttl = cache_ttl_seconds()
        if ttl <= 0 or (time.monotonic() - cached.cached_at) < ttl:
            return cached
        STATE.invalidate(connection_id)

    start = time.perf_counter_ns()
    conn_mgr = profile.inject(BaseConnectionManager)
    targets = await conn_mgr.get_connection_targets(connection_id=connection_id)
    if not targets:
        raise LookupError(f"No connection targets for {connection_id}")
    target = targets[0]
    if not target.endpoint or not target.recipient_keys or not target.sender_key:
        raise LookupError(f"Incomplete connection target for {connection_id}")

    # Fetch the sender signing key once; keep independent handles so nothing
    # references a closed session or freed entry list.
    async with profile.session() as session:
        entry = await session.handle.fetch_key(target.sender_key)
        if not entry:
            raise LookupError(f"Missing sender key {target.sender_key}")
        sender_ed = _independent_key(entry.key)

    sender_vk_b = target.sender_key.encode("utf-8")
    sender_xk = sender_ed.convert_key(KeyAlg.X25519)

    cached = CachedTarget(
        endpoint=target.endpoint,
        recipients=[_recipient_crypto(vk, sender_vk_b) for vk in target.recipient_keys],
        routing_recipients=[
            _recipient_crypto(vk, sender_vk_b) for vk in (target.routing_keys or [])
        ],
        routing_verkeys=list(target.routing_keys or []),
        sender_vk_b=sender_vk_b,
        sender_xk=sender_xk,
        cached_at=time.monotonic(),
    )
    STATE.targets[connection_id] = cached
    STATE.record("resolve_cold", time.perf_counter_ns() - start)
    return cached


def build_basicmessage(content: str, use_new_prefix: bool) -> bytes:
    """Minimal DIDComm v1 basic message body (no marshmallow serialization)."""
    msg_type = BASICMESSAGE_TYPE_NEW if use_new_prefix else BASICMESSAGE_TYPE_OLD
    now = time.strftime("%Y-%m-%dT%H:%M:%S.000000Z", time.gmtime())
    return json.dumps(
        {
            "@type": msg_type,
            "@id": str(uuid.uuid4()),
            "~l10n": {"locale": "en"},
            "sent_time": now,
            "content": content,
        }
    ).encode("utf-8")


def serialize_agent_message(message: Any) -> bytes:
    """Serialize an ACA-Py AgentMessage (or mapping) to DIDComm plaintext bytes."""
    if isinstance(message, (bytes, bytearray)):
        return bytes(message)
    if isinstance(message, str):
        return message.encode("utf-8")
    if isinstance(message, dict):
        return json.dumps(message).encode("utf-8")
    # AgentMessage.serialize() -> OrderedDict / dict
    if hasattr(message, "serialize"):
        return json.dumps(message.serialize()).encode("utf-8")
    raise TypeError(f"Unsupported message type for fastpath pack: {type(message)!r}")


def _pack_authcrypt(
    message: bytes,
    recipients: List[RecipientCrypto],
    sender_vk_b: bytes,
    sender_xk: Key,
) -> bytes:
    """DIDComm v1 authcrypt pack using precomputed recipient/sender material."""
    wrapper = JweEnvelope(with_protected_recipients=True, with_flatten_recipients=False)
    cek = Key.generate(KeyAlg.C20P)
    cek_b = key_get_secret_bytes(cek._handle)

    for rec in recipients:
        nonce = crypto_box.random_nonce()
        enc_cek = crypto_box.crypto_box(rec.xk, sender_xk, cek_b, nonce)
        wrapper.add_recipient(
            JweRecipient(
                encrypted_key=enc_cek,
                header=OrderedDict(
                    [
                        ("kid", rec.verkey),
                        ("sender", b64url(rec.enc_sender)),
                        ("iv", b64url(nonce)),
                    ]
                ),
            )
        )
    wrapper.set_protected(
        OrderedDict(
            [
                ("enc", "xchacha20poly1305_ietf"),
                ("typ", "JWM/1.0"),
                ("alg", "Authcrypt"),
            ]
        ),
    )
    enc = cek.aead_encrypt(message, aad=wrapper.protected_bytes)
    ciphertext, tag, nonce = enc.parts
    wrapper.set_payload(ciphertext, nonce, tag)
    return wrapper.to_json().encode("utf-8")


def _pack_anoncrypt(message: bytes, recipients: List[RecipientCrypto]) -> bytes:
    """DIDComm v1 anoncrypt pack (used for mediator forward wrapping)."""
    wrapper = JweEnvelope(with_protected_recipients=True, with_flatten_recipients=False)
    cek = Key.generate(KeyAlg.C20P)
    cek_b = key_get_secret_bytes(cek._handle)
    for rec in recipients:
        enc_cek = crypto_box.crypto_box_seal(rec.xk, cek_b)
        wrapper.add_recipient(
            JweRecipient(encrypted_key=enc_cek, header={"kid": rec.verkey})
        )
    wrapper.set_protected(
        OrderedDict(
            [
                ("enc", "xchacha20poly1305_ietf"),
                ("typ", "JWM/1.0"),
                ("alg", "Anoncrypt"),
            ]
        ),
    )
    enc = cek.aead_encrypt(message, aad=wrapper.protected_bytes)
    ciphertext, tag, nonce = enc.parts
    wrapper.set_payload(ciphertext, nonce, tag)
    return wrapper.to_json().encode("utf-8")


def pack_for_target(message: bytes, target: CachedTarget) -> bytes:
    """Full pack, including mediator forward wrapping when routing keys exist."""
    packed = _pack_authcrypt(
        message, target.recipients, target.sender_vk_b, target.sender_xk
    )
    # Wrap in forward messages for each routing key (mediated connections).
    to_key = target.recipients[0].verkey
    for routing_rec in target.routing_recipients:
        forward = json.dumps(
            {
                "@type": "https://didcomm.org/routing/1.0/forward",
                "@id": str(uuid.uuid4()),
                "to": to_key,
                "msg": json.loads(packed.decode("utf-8")),
            }
        ).encode("utf-8")
        packed = _pack_anoncrypt(forward, [routing_rec])
        to_key = routing_rec.verkey
    return packed


async def send_packed(
    profile: Profile, connection_id: str, message: Union[bytes, Any]
) -> dict:
    """Fast-path pack+deliver for arbitrary DIDComm plaintext (or AgentMessage)."""
    t_total = time.perf_counter_ns()
    if STATE.first_send_ts is None:
        STATE.first_send_ts = time.monotonic()

    target = await resolve_target(profile, connection_id)

    t = time.perf_counter_ns()
    body = serialize_agent_message(message)
    STATE.record("build", time.perf_counter_ns() - t)

    t = time.perf_counter_ns()
    packed = await asyncio.get_event_loop().run_in_executor(
        _PACK_POOL, pack_for_target, body, target
    )
    STATE.record("pack", time.perf_counter_ns() - t)

    t = time.perf_counter_ns()
    mime = (
        DIDCOMM_V1_MIME
        if profile.settings.get("emit_new_didcomm_mime_type")
        else DIDCOMM_V0_MIME
    )
    endpoint = deliver_override_endpoint() or target.endpoint
    async with STATE.http().post(
        endpoint, data=packed, headers={"Content-Type": mime}
    ) as resp:
        if resp.status < 200 or resp.status > 299:
            raise RuntimeError(f"Delivery failed: HTTP {resp.status} {resp.reason}")
    STATE.record("deliver", time.perf_counter_ns() - t)

    STATE.record("total", time.perf_counter_ns() - t_total)
    STATE.last_send_ts = time.monotonic()
    return {}


async def send_agent_message(
    profile: Profile, connection_id: str, message: Any
) -> dict:
    """Alias for send_packed — pack+deliver an ACA-Py AgentMessage."""
    return await send_packed(profile, connection_id, message)


async def send_basicmessage(
    profile: Profile, connection_id: str, content: str
) -> dict:
    """Fast-path send: resolve (cached) -> build basicmessage -> pack -> deliver."""
    t_total = time.perf_counter_ns()
    if STATE.first_send_ts is None:
        STATE.first_send_ts = time.monotonic()

    target = await resolve_target(profile, connection_id)

    t = time.perf_counter_ns()
    body = build_basicmessage(
        content, use_new_prefix=bool(profile.settings.get("emit_new_didcomm_prefix"))
    )
    STATE.record("build", time.perf_counter_ns() - t)

    t = time.perf_counter_ns()
    packed = await asyncio.get_event_loop().run_in_executor(
        _PACK_POOL, pack_for_target, body, target
    )
    STATE.record("pack", time.perf_counter_ns() - t)

    t = time.perf_counter_ns()
    mime = (
        DIDCOMM_V1_MIME
        if profile.settings.get("emit_new_didcomm_mime_type")
        else DIDCOMM_V0_MIME
    )
    endpoint = deliver_override_endpoint() or target.endpoint
    async with STATE.http().post(
        endpoint, data=packed, headers={"Content-Type": mime}
    ) as resp:
        if resp.status < 200 or resp.status > 299:
            raise RuntimeError(f"Delivery failed: HTTP {resp.status} {resp.reason}")
    STATE.record("deliver", time.perf_counter_ns() - t)

    STATE.record("total", time.perf_counter_ns() - t_total)
    STATE.last_send_ts = time.monotonic()
    return {}
