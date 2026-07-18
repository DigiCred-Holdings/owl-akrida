"""Verify the X-Internal-Sig header on internal admin requests.

Wire format:  ``t=<unix_ts>,n=<hex_nonce>,v=<hex_hmac_sha256>``
Signed payload:  ``f"{METHOD}\\n{PATH_QS}\\n{ts}\\n{nonce}\\n{sha256(body)}"``
Key:  ``INTERNAL_HMAC_KEY`` preferred, ``ACAPY_ADMIN_API_KEY`` fallback
(must be at least 32 chars for HMAC-SHA256). Mirrors
``resolveInternalHmacKey()`` on the Nest side — set the dedicated key on
both containers or on neither.

Wire format is in sync with the Node signer at
``services/crms-ui/src/common/internal-hmac.ts``. Tests on both sides
pin the wire shape so a drift would fail both suites loudly.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import time
from functools import wraps
from typing import Awaitable, Callable, Optional, Protocol

from aiohttp import web

_HEADER_NAME = "X-Internal-Sig"
_DEFAULT_MAX_AGE_S = 60
# Reject signed timestamps more than this many seconds in the future. Stops
# an attacker who can sign once from extending the replay window by post-
# dating ts; the previous ``abs(now - ts) > max_age`` accepted up to
# max_age in either direction, doubling the effective window.
_FUTURE_SKEW_S = 5
# 256 bits of entropy = 32 ASCII chars. Anything shorter is rejected at
# verify-time so a misconfigured env fails loud instead of silently
# degrading to a weak MAC.
_MIN_KEY_LENGTH = 32
# Upper bound kept in lockstep with the TS verifier (internal-hmac.ts):
# stops multi-KB nonces from bloating the replay cache.
_NONCE_RE = re.compile(r"^[0-9a-fA-F]{8,64}$")
_VERIFIED_BODY_KEY = "_kanon_verified_body"
_VERIFIED_JSON_KEY = "_kanon_verified_json"


class ReplayCache(Protocol):
    """Caller-supplied replay store. Optional — when present, the verifier
    rejects a (ts, nonce) pair that has already been accepted."""

    async def has(self, key: str) -> bool: ...
    async def add(self, key: str, ttl_seconds: int) -> None: ...


def _get_key() -> str:
    key = os.environ.get("INTERNAL_HMAC_KEY") or os.environ.get("ACAPY_ADMIN_API_KEY") or ""
    if not key or len(key) < _MIN_KEY_LENGTH:
        raise web.HTTPInternalServerError(
            reason="INTERNAL_HMAC_KEY/ACAPY_ADMIN_API_KEY missing or too weak "
            "for HMAC verification"
        )
    return key


def _parse_header(header_value: str) -> tuple[int, str, str]:
    """Returns ``(ts, nonce, hex_sig)``. Raises 403 on malformed input."""
    parts: dict[str, str] = {}
    for kv in header_value.split(","):
        if "=" not in kv:
            continue
        k, _, v = kv.strip().partition("=")
        parts[k] = v
    try:
        ts = int(parts.get("t", ""))
    except ValueError as err:
        raise web.HTTPForbidden(reason="internal-sig: malformed timestamp") from err
    nonce = parts.get("n", "")
    if not nonce or not _NONCE_RE.fullmatch(nonce):
        raise web.HTTPForbidden(reason="internal-sig: malformed nonce")
    v = parts.get("v", "")
    if not v:
        raise web.HTTPForbidden(reason="internal-sig: missing v")
    return ts, nonce, v


async def verify_internal_caller(
    request: web.BaseRequest,
    *,
    max_age_s: int = _DEFAULT_MAX_AGE_S,
    now_fn: Callable[[], float] = time.time,
    replay_cache: Optional[ReplayCache] = None,
) -> None:
    header_value = request.headers.get(_HEADER_NAME, "")
    if not header_value:
        raise web.HTTPForbidden(reason="internal-sig: header missing")

    ts, nonce, signed_hex = _parse_header(header_value)
    now = now_fn()
    if now - ts > max_age_s:
        raise web.HTTPForbidden(reason="internal-sig: stale")
    if ts - now > _FUTURE_SKEW_S:
        raise web.HTTPForbidden(reason="internal-sig: future timestamp")

    body_bytes = await request.read() if request.can_read_body else b""
    body_hash = hashlib.sha256(body_bytes).hexdigest()
    path_qs = request.rel_url.path_qs
    payload = f"{request.method.upper()}\n{path_qs}\n{ts}\n{nonce}\n{body_hash}"

    expected = hmac.new(_get_key().encode(), payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signed_hex, expected):
        raise web.HTTPForbidden(reason="internal-sig: signature mismatch")

    # Replay protection: once we've vouched for a (ts, nonce) pair, the
    # cache holds it until the request would have aged out anyway.
    if replay_cache is not None:
        replay_key = f"{ts}:{nonce}"
        if await replay_cache.has(replay_key):
            raise web.HTTPForbidden(reason="internal-sig: replay")
        await replay_cache.add(replay_key, max_age_s + _FUTURE_SKEW_S)

    request[_VERIFIED_BODY_KEY] = body_bytes


def get_verified_body(request: web.BaseRequest) -> bytes:
    if _VERIFIED_BODY_KEY not in request:
        raise web.HTTPInternalServerError(
            reason="internal-sig: verifier did not run before body access"
        )
    return request[_VERIFIED_BODY_KEY]


def get_verified_json(request: web.BaseRequest) -> dict:
    if _VERIFIED_JSON_KEY in request:
        return request[_VERIFIED_JSON_KEY]
    body = get_verified_body(request)
    if not body:
        request[_VERIFIED_JSON_KEY] = {}
        return {}
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as err:
        raise web.HTTPBadRequest(reason="internal-sig: body is not valid JSON") from err
    request[_VERIFIED_JSON_KEY] = parsed
    return parsed


def require_internal_caller(handler: Callable[..., Awaitable[web.StreamResponse]]):
    @wraps(handler)
    async def wrapped(request: web.BaseRequest, *args, **kwargs):
        await verify_internal_caller(request)
        return await handler(request, *args, **kwargs)

    return wrapped
