#!/usr/bin/env python3
"""Replay a valid authcrypt BasicMessage into ACA-Py and time full handling.

Run this from a separate ACA-Py image container on the benchmark Docker
network. It creates one static connection with known remote key material,
warms the unified fast-path object via one outbound send, then concurrently
replays a wire-valid DIDComm v1 envelope. ACA-Py's BasicMessage handler event
counter—not HTTP acceptance—determines completion and throughput.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import secrets
import string
import time

import aiohttp
from aries_askar import Key, KeyAlg

from acapy_agent.askar.didcomm.v1 import pack_message


def seed() -> str:
    """Return a fresh 32-byte ASCII Ed25519 secret."""
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(32))


async def checked_json(
    session: aiohttp.ClientSession, method: str, url: str, **kwargs
) -> dict:
    async with session.request(method, url, **kwargs) as response:
        body = await response.text()
        if response.status < 200 or response.status >= 300:
            raise RuntimeError(f"{method} {url}: HTTP {response.status}: {body}")
        return json.loads(body) if body else {}


async def wait_handled(
    session: aiohttp.ClientSession,
    admin_url: str,
    target: int,
    timeout: float,
) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        stats = await checked_json(
            session, "GET", f"{admin_url}/didcomm-fastpath/stats"
        )
        if stats.get("inbound_handled", 0) >= target:
            return stats
        await asyncio.sleep(0.1)
    raise TimeoutError(f"only {stats.get('inbound_handled', 0)}/{target} handled")


async def main(args) -> None:
    timeout = aiohttp.ClientTimeout(total=args.timeout)
    connector = aiohttp.TCPConnector(limit=args.concurrency * 2)
    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        their_seed = seed()
        static = await checked_json(
            session,
            "POST",
            f"{args.admin_url}/connections/create-static",
            json={
                "their_seed": their_seed,
                "their_endpoint": args.remote_endpoint,
                "their_label": "inbound-replay-sender",
                "alias": f"inbound-{int(time.time())}",
            },
        )
        connection_id = static["record"]["connection_id"]
        local_verkey = static["my_verkey"]

        # Populate the exact shared connection object used in both directions.
        await checked_json(
            session,
            "POST",
            (
                f"{args.admin_url}/didcomm-fastpath/connections/"
                f"{connection_id}/send-message"
            ),
            json={"content": "warm unified bidirectional cache"},
        )

        remote_ed = Key.from_secret_bytes(
            KeyAlg.ED25519, their_seed.encode("utf-8")
        )
        plaintext = json.dumps(
            {
                "@type": "https://didcomm.org/basicmessage/1.0/message",
                "@id": secrets.token_hex(16),
                "sent_time": "2026-07-18T00:00:00.000000Z",
                "content": "inbound replay benchmark",
            }
        ).encode("utf-8")
        packed = pack_message([local_verkey], remote_ed, plaintext)

        await checked_json(
            session, "DELETE", f"{args.admin_url}/didcomm-fastpath/stats"
        )

        claimed = 0
        claim_lock = asyncio.Lock()
        failures = []

        async def worker():
            nonlocal claimed
            while True:
                async with claim_lock:
                    if claimed >= args.messages:
                        return
                    claimed += 1
                try:
                    async with session.post(
                        args.inbound_url,
                        data=packed,
                        headers={"Content-Type": "application/didcomm-envelope-enc"},
                    ) as response:
                        if response.status < 200 or response.status >= 300:
                            failures.append(
                                f"HTTP {response.status}: {await response.text()}"
                            )
                except Exception as err:
                    failures.append(repr(err))

        accepted_start = time.monotonic()
        await asyncio.gather(*(worker() for _ in range(args.concurrency)))
        accepted_seconds = time.monotonic() - accepted_start
        stats = await wait_handled(
            session, args.admin_url, args.messages - len(failures), args.timeout
        )

        result = {
            "messages": args.messages,
            "concurrency": args.concurrency,
            "failures": len(failures),
            "http_accept_seconds": round(accepted_seconds, 3),
            "http_accept_rps": round(args.messages / accepted_seconds, 2),
            "handled": stats.get("inbound_handled"),
            "handled_rps": stats.get("inbound_observed_rps"),
            "handled_seconds": stats.get("inbound_window_seconds"),
            "cache_hits": stats.get("inbound_cache_hits"),
            "cache_misses": stats.get("inbound_cache_misses"),
            "hot_unpack": stats.get("inbound_unpack"),
            "cold_unpack": stats.get("inbound_unpack_cold"),
            "cache_entries": stats.get("cache_entries"),
        }
        if failures:
            result["first_failure"] = failures[0]
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--admin-url", default="http://issuer:8150")
    parser.add_argument("--inbound-url", default="http://issuer:8151")
    # ACA-Py's endpoint validator rejects Docker service hostnames. Delivery is
    # redirected by FASTPATH_DELIVER_OVERRIDE during this benchmark.
    parser.add_argument("--remote-endpoint", default="https://example.com")
    parser.add_argument("--messages", type=int, default=10_000)
    parser.add_argument("--concurrency", type=int, default=20)
    parser.add_argument("--timeout", type=float, default=300)
    asyncio.run(main(parser.parse_args()))
