"""Lightweight DIDComm message sink — returns 200 without unpacking.

Used to measure issuer pack+deliver capacity without Credo holder CPU
competing for cores on the same host. Connection setup still uses real
Credo agents; only the delivery HTTP hop is redirected here.
"""

from __future__ import annotations

import time
from aiohttp import web

state = {
    "received": 0,
    "bytes": 0,
    "first_ts": None,
    "last_ts": None,
}


async def handle_message(request: web.Request) -> web.Response:
    body = await request.read()
    now = time.monotonic()
    state["received"] += 1
    state["bytes"] += len(body)
    if state["first_ts"] is None:
        state["first_ts"] = now
    state["last_ts"] = now
    return web.Response(status=200)


async def handle_stats(_request: web.Request) -> web.Response:
    received = state["received"]
    window = 0.0
    rps = 0.0
    if state["first_ts"] and state["last_ts"] and state["last_ts"] != state["first_ts"]:
        window = state["last_ts"] - state["first_ts"]
        rps = received / window
    return web.json_response(
        {
            "received": received,
            "bytes": state["bytes"],
            "window_seconds": round(window, 3),
            "observed_rps": round(rps, 2),
        }
    )


async def handle_reset(_request: web.Request) -> web.Response:
    state["received"] = 0
    state["bytes"] = 0
    state["first_ts"] = None
    state["last_ts"] = None
    return web.json_response({"reset": True})


async def handle_health(_request: web.Request) -> web.Response:
    return web.json_response({"alive": True})


def main() -> None:
    app = web.Application(client_max_size=10 * 1024 * 1024)
    app.router.add_post("/", handle_message)
    app.router.add_post("/{path:.*}", handle_message)
    app.router.add_get("/stats", handle_stats)
    app.router.add_delete("/stats", handle_reset)
    app.router.add_get("/health", handle_health)
    web.run_app(app, host="0.0.0.0", port=8090, print=lambda *_: None)


if __name__ == "__main__":
    main()
