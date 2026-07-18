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

Cache hardening:
- Keys are ``(local_tenant_wallet_id, connection_id)``. ``wallet_id`` is *our*
  ACA-Py tenant subwallet (``profile.settings["wallet.id"]``), not the remote
  peer. Isolation prevents one tenant from reusing another's cached sender_xk.
- FASTPATH_CACHE_TTL (default 30; 0 disables): lazy on access + ~1s active sweep.
- FASTPATH_CACHE_MAX (default 8192): LRU eviction. Size by peak *concurrent*
  active (tenant, connection) pairs on this process, not total connections.
- ConnRecord events, DELETE /cache, and tenant removal (invalidate_wallet /
  EventBus) dispose Askar Key handles by dropping refs.
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
from acapy_agent.connections.models.conn_record import ConnRecord
from acapy_agent.core.profile import Profile
from acapy_agent.utils.jwe import JweEnvelope, JweRecipient, b64url
from acapy_agent.wallet.base import BaseWallet
from acapy_agent.wallet.util import b58_to_bytes

LOGGER = logging.getLogger(__name__)

BASICMESSAGE_TYPE_NEW = "https://didcomm.org/basicmessage/1.0/message"
BASICMESSAGE_TYPE_OLD = "did:sov:BzCbsNYhMrjHiqZDTUASHg;spec/basicmessage/1.0/message"
DIDCOMM_V0_MIME = "application/ssi-agent-wire"
DIDCOMM_V1_MIME = "application/didcomm-envelope-enc"

# Custom EventBus topic (optional emitters e.g. Kanon multitenant manager).
WALLET_REMOVED_TOPIC = "acapy::didcomm_fastpath::wallet_removed"
SWEEP_INTERVAL_SECONDS = 1.0


@dataclass
class RecipientCrypto:
    """Precomputed crypto material for one recipient verkey."""

    verkey: str
    xk: Key  # recipient X25519 public key
    enc_sender: bytes  # crypto_box_seal(target_xk, sender_vk), reusable


@dataclass
class CachedConnectionCrypto:
    """Bidirectional crypto and delivery material for one connection.

    The local ``sender_xk`` keypair used to authcrypt outbound messages is the
    same private X25519 key needed to decrypt inbound messages addressed to
    ``sender_vk_b``. Likewise, ``recipients[].xk`` are the remote public keys
    used in both directions. One object therefore owns both directions' native
    key handles; the inbound recipient-key index only references this object.
    """

    endpoint: str
    recipients: List[RecipientCrypto]
    routing_recipients: List[RecipientCrypto]  # for mediator forward wrapping
    routing_verkeys: List[str]
    sender_vk_b: bytes  # base58 sender verkey, utf-8 encoded
    sender_xk: Key  # sender X25519 keypair (independent handle)
    wallet_id: str = "base"
    connection_id: str = ""
    cached_at: float = 0.0  # time.monotonic() at cache insert, for TTL expiry
    connection_record: Optional[ConnRecord] = None
    recipient_did_public: bool = False


# Backwards-compatible name used by the original outbound implementation.
CachedTarget = CachedConnectionCrypto


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


@dataclass
class CacheMetrics:
    """Counters for cache lifecycle events."""

    evictions_lru: int = 0
    expirations_ttl: int = 0
    invalidations_connection: int = 0
    invalidations_wallet: int = 0
    inbound_cache_hits: int = 0
    inbound_cache_misses: int = 0

    def as_dict(self) -> dict:
        return {
            "evictions_lru": self.evictions_lru,
            "expirations_ttl": self.expirations_ttl,
            "invalidations_connection": self.invalidations_connection,
            "invalidations_wallet": self.invalidations_wallet,
            "inbound_cache_hits": self.inbound_cache_hits,
            "inbound_cache_misses": self.inbound_cache_misses,
        }


def wallet_id_for_profile(profile: Profile) -> str:
    """Local ACA-Py tenant subwallet id for cache scoping.

    This is *our* tenant (``wallet.id`` on the sending profile), not the remote
    peer's wallet. Single-tenant / base agents fall back to ``\"base\"``.
    """
    settings = getattr(profile, "settings", None)
    if settings is not None:
        wid = settings.get("wallet.id")
        if wid:
            return str(wid)
    name = getattr(profile, "name", None) or getattr(profile, "profile_id", None)
    return str(name) if name else "base"


def cache_key(wallet_id: str, connection_id: str) -> str:
    """``{local_tenant_wallet_id}:{connection_id}`` — never connection_id alone."""
    return f"{wallet_id}:{connection_id}"


def dispose_target(target: Optional[CachedTarget]) -> None:
    """Drop Askar Key refs so CPython can free/zeroize native handles.

    Aries Askar frees key material when the last Python Key reference is
    dropped (``askar_key_free``); Rust zeroizes on drop (best-effort).
    """
    if target is None:
        return
    try:
        for rec in list(target.recipients or []):
            rec.xk = None  # type: ignore[assignment]
        target.recipients.clear()
        for rec in list(target.routing_recipients or []):
            rec.xk = None  # type: ignore[assignment]
        target.routing_recipients.clear()
        target.sender_xk = None  # type: ignore[assignment]
        target.sender_vk_b = b""
        target.connection_record = None
        target.recipient_did_public = False
    except Exception:  # pragma: no cover - defensive
        LOGGER.debug("dispose_target failed", exc_info=True)


class FastpathState:
    """Singleton plugin state: wallet-scoped LRU target cache, HTTP, stats."""

    STAGES = ("resolve_cold", "build", "pack", "deliver", "total")

    def __init__(self):
        self.targets: "OrderedDict[str, CachedConnectionCrypto]" = OrderedDict()
        # Secondary references only: (wallet, local recipient verkey) -> one or
        # more primary connection-cache keys. No key handles are duplicated.
        self.recipient_index: Dict[str, "OrderedDict[str, None]"] = {}
        self._http: Optional[ClientSession] = None
        self.stats: Dict[str, StageStats] = {s: StageStats() for s in self.STAGES}
        self.cache_metrics = CacheMetrics()
        self.first_send_ts: Optional[float] = None
        self.last_send_ts: Optional[float] = None
        self.inbound_handled_count = 0
        self.first_inbound_handled_ts: Optional[float] = None
        self.last_inbound_handled_ts: Optional[float] = None
        # Optional external timing dict (e.g. workflow_protocol stages)
        self.external_stats: Dict[str, Any] = {}
        self._sweep_task: Optional[asyncio.Task] = None

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
        out["inbound_handled"] = self.inbound_handled_count
        if (
            self.inbound_handled_count
            and self.first_inbound_handled_ts
            and self.last_inbound_handled_ts != self.first_inbound_handled_ts
        ):
            inbound_window = (
                self.last_inbound_handled_ts - self.first_inbound_handled_ts
            )
            out["inbound_observed_rps"] = round(
                self.inbound_handled_count / inbound_window, 2
            )
            out["inbound_window_seconds"] = round(inbound_window, 3)
        n = len(self.targets)
        out["cache_entries"] = n
        out["cached_connections"] = n  # alias kept for older dashboards
        out["inbound_index_keys"] = len(self.recipient_index)
        out["cache_max"] = cache_max_entries()
        out["ttl_seconds"] = cache_ttl_seconds()
        out["pack_workers"] = _PACK_POOL._max_workers  # type: ignore[attr-defined]
        out.update(self.cache_metrics.as_dict())
        if self.external_stats:
            out["workflow"] = self.external_stats
        return out

    def reset_stats(self):
        """Reset timing counters only (keeps cache + eviction metrics)."""
        self.stats = {s: StageStats() for s in self.STAGES}
        self.first_send_ts = None
        self.last_send_ts = None
        self.inbound_handled_count = 0
        self.first_inbound_handled_ts = None
        self.last_inbound_handled_ts = None

    def record_inbound_handled(self) -> None:
        """Count a BasicMessage after its stock ACA-Py handler completed."""
        now = time.monotonic()
        if self.first_inbound_handled_ts is None:
            self.first_inbound_handled_ts = now
        self.last_inbound_handled_ts = now
        self.inbound_handled_count += 1

    def get(
        self, wallet_id: str, connection_id: str
    ) -> Optional[CachedConnectionCrypto]:
        """Return a fresh cached target, touching LRU order on hit."""
        key = cache_key(wallet_id, connection_id)
        return self._get_by_key(key)

    def _get_by_key(self, key: str) -> Optional[CachedConnectionCrypto]:
        """Return one primary cache entry by key, enforcing TTL and LRU."""
        cached = self.targets.get(key)
        if not cached:
            return None
        ttl = cache_ttl_seconds()
        if ttl > 0 and (time.monotonic() - cached.cached_at) >= ttl:
            self._pop_and_dispose(key, reason="ttl")
            return None
        self.targets.move_to_end(key)
        return cached

    def get_by_recipient(
        self, wallet_id: str, recipient_verkey: str
    ) -> List[CachedConnectionCrypto]:
        """Return live connection objects addressed to one local recipient key.

        Pairwise connections normally produce one candidate. A list correctly
        handles deployments that reuse a local DID/key across connections; the
        authenticated sender key selects the matching candidate during unpack.
        """
        index_key = cache_key(wallet_id, recipient_verkey)
        primary_keys = list(self.recipient_index.get(index_key, ()))
        candidates = []
        for primary_key in primary_keys:
            cached = self._get_by_key(primary_key)
            if cached is not None:
                candidates.append(cached)
        if candidates:
            self.cache_metrics.inbound_cache_hits += 1
        else:
            self.cache_metrics.inbound_cache_misses += 1
        return candidates

    def put(
        self,
        wallet_id: str,
        connection_id: str,
        target: CachedConnectionCrypto,
    ) -> None:
        """Insert/replace a target; evict LRU entries if over max size."""
        key = cache_key(wallet_id, connection_id)
        target.wallet_id = wallet_id
        target.connection_id = connection_id
        if key in self.targets:
            old = self.targets.pop(key)
            self._unindex_target(key, old)
            dispose_target(old)
        self.targets[key] = target
        self._index_target(key, target)
        self.targets.move_to_end(key)
        max_entries = cache_max_entries()
        while max_entries > 0 and len(self.targets) > max_entries:
            old_key, old = self.targets.popitem(last=False)
            self._unindex_target(old_key, old)
            dispose_target(old)
            self.cache_metrics.evictions_lru += 1
            LOGGER.debug("didcomm_fastpath: LRU evicted %s", old_key)

    def invalidate(self, connection_id: str, *, wallet_id: str) -> bool:
        """Drop one connection's cached target for a specific local tenant."""
        if not wallet_id:
            raise ValueError("wallet_id is required (local tenant subwallet id)")
        removed = self._pop_and_dispose(
            cache_key(wallet_id, connection_id), reason="connection"
        )
        return removed is not None

    def invalidate_wallet(self, wallet_id: str) -> int:
        """Drop all cached targets for one local tenant subwallet."""
        if not wallet_id:
            return 0
        prefix = f"{wallet_id}:"
        keys = [k for k in self.targets if k.startswith(prefix)]
        for key in keys:
            self._pop_and_dispose(key, reason="wallet")
        if keys:
            LOGGER.info(
                "didcomm_fastpath: cleared %d cache entries for tenant wallet %s",
                len(keys),
                wallet_id,
            )
        return len(keys)

    def clear(self, wallet_id: Optional[str] = None) -> int:
        """Clear all entries, or only those for one wallet."""
        if wallet_id is not None:
            return self.invalidate_wallet(wallet_id)
        n = len(self.targets)
        while self.targets:
            key, old = self.targets.popitem(last=True)
            self._unindex_target(key, old)
            dispose_target(old)
        self.recipient_index.clear()
        return n

    def expire_stale(self) -> int:
        """Actively drop TTL-expired entries; return count removed."""
        ttl = cache_ttl_seconds()
        if ttl <= 0:
            return 0
        now = time.monotonic()
        stale = [
            key
            for key, target in self.targets.items()
            if (now - target.cached_at) >= ttl
        ]
        for key in stale:
            self._pop_and_dispose(key, reason="ttl")
        return len(stale)

    def _pop_and_dispose(
        self, key: str, reason: str
    ) -> Optional[CachedConnectionCrypto]:
        target = self.targets.pop(key, None)
        if target is None:
            return None
        self._unindex_target(key, target)
        dispose_target(target)
        if reason == "ttl":
            self.cache_metrics.expirations_ttl += 1
        elif reason == "connection":
            self.cache_metrics.invalidations_connection += 1
        elif reason == "wallet":
            self.cache_metrics.invalidations_wallet += 1
        return target

    def _index_target(
        self, primary_key: str, target: CachedConnectionCrypto
    ) -> None:
        """Index a connection object by its local inbound recipient verkey."""
        if not target.sender_vk_b:
            return
        try:
            recipient_verkey = target.sender_vk_b.decode("utf-8")
        except UnicodeDecodeError:
            return
        index_key = cache_key(target.wallet_id, recipient_verkey)
        bucket = self.recipient_index.setdefault(index_key, OrderedDict())
        bucket[primary_key] = None

    def _unindex_target(
        self, primary_key: str, target: CachedConnectionCrypto
    ) -> None:
        """Remove secondary references before disposing the owning object."""
        if not target.sender_vk_b:
            return
        try:
            recipient_verkey = target.sender_vk_b.decode("utf-8")
        except UnicodeDecodeError:
            return
        index_key = cache_key(target.wallet_id, recipient_verkey)
        bucket = self.recipient_index.get(index_key)
        if not bucket:
            return
        bucket.pop(primary_key, None)
        if not bucket:
            self.recipient_index.pop(index_key, None)

    def start_sweeper(self) -> None:
        """Start background TTL sweep if not already running."""
        if self._sweep_task and not self._sweep_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            LOGGER.debug("didcomm_fastpath: no running loop; sweeper not started")
            return

        async def _sweep_loop():
            while True:
                try:
                    await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
                    n = self.expire_stale()
                    if n:
                        LOGGER.debug(
                            "didcomm_fastpath: active TTL expired %d entries", n
                        )
                except asyncio.CancelledError:
                    raise
                except Exception:  # pragma: no cover
                    LOGGER.exception("didcomm_fastpath: cache sweeper error")

        self._sweep_task = loop.create_task(
            _sweep_loop(), name="didcomm-fastpath-cache-sweep"
        )

    def stop_sweeper(self) -> None:
        if self._sweep_task and not self._sweep_task.done():
            self._sweep_task.cancel()
        self._sweep_task = None

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
    """TTL for cached targets (seconds). Default 30; 0 disables expiry."""
    try:
        return float(os.environ.get("FASTPATH_CACHE_TTL", "30"))
    except ValueError:
        return 30.0


def cache_max_entries() -> int:
    """Maximum cached targets (LRU). Default 8192; 0 disables the bound.

    Size by peak concurrently-active (tenant, connection) pairs on this
    process (idle entries fall out via TTL), not by total connections.
    """
    try:
        return int(os.environ.get("FASTPATH_CACHE_MAX", "8192"))
    except ValueError:
        return 8192


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
    wallet_id = wallet_id_for_profile(profile)
    cached = STATE.get(wallet_id, connection_id)
    if cached:
        return cached

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
        connection_record = await ConnRecord.retrieve_by_id(session, connection_id)
        recipient_did_public = False
        if connection_record.my_did:
            try:
                wallet = session.inject(BaseWallet)
                my_info = await wallet.get_local_did(connection_record.my_did)
                recipient_did_public = bool(
                    my_info.metadata.get("posted", False)
                )
            except Exception:
                # Public-DID metadata only affects receipt decoration. Unknown
                # metadata safely retains stock's default false value.
                pass
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
        wallet_id=wallet_id,
        connection_id=connection_id,
        cached_at=time.monotonic(),
        connection_record=connection_record,
        recipient_did_public=recipient_did_public,
    )
    STATE.put(wallet_id, connection_id, cached)
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


async def _pack_and_deliver(
    profile: Profile,
    connection_id: str,
    body: bytes,
    *,
    t_total: int,
) -> dict:
    """Shared hot path: resolve → pack (thread pool) → HTTP deliver."""
    target = await resolve_target(profile, connection_id)

    t = time.perf_counter_ns()
    packed = await asyncio.get_running_loop().run_in_executor(
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


async def send_packed(
    profile: Profile, connection_id: str, message: Union[bytes, Any]
) -> dict:
    """Fast-path pack+deliver for arbitrary DIDComm plaintext (or AgentMessage)."""
    t_total = time.perf_counter_ns()
    if STATE.first_send_ts is None:
        STATE.first_send_ts = time.monotonic()

    t = time.perf_counter_ns()
    body = serialize_agent_message(message)
    STATE.record("build", time.perf_counter_ns() - t)
    return await _pack_and_deliver(profile, connection_id, body, t_total=t_total)


async def send_agent_message(
    profile: Profile, connection_id: str, message: Any
) -> dict:
    """Alias for send_packed — pack+deliver an ACA-Py AgentMessage."""
    return await send_packed(profile, connection_id, message)


async def send_basicmessage(
    profile: Profile, connection_id: str, content: str
) -> dict:
    """Fast-path send: resolve (cached) → build basicmessage → pack → deliver."""
    t_total = time.perf_counter_ns()
    if STATE.first_send_ts is None:
        STATE.first_send_ts = time.monotonic()

    t = time.perf_counter_ns()
    body = build_basicmessage(
        content, use_new_prefix=bool(profile.settings.get("emit_new_didcomm_prefix"))
    )
    STATE.record("build", time.perf_counter_ns() - t)
    return await _pack_and_deliver(profile, connection_id, body, t_total=t_total)
