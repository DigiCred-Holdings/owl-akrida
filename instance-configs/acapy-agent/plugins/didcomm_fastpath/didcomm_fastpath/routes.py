"""Admin routes for the didcomm_fastpath plugin.

POST   /didcomm-fastpath/connections/{conn_id}/send-message
GET    /didcomm-fastpath/stats
DELETE /didcomm-fastpath/stats
DELETE /didcomm-fastpath/cache
"""

import logging

from aiohttp import web

from acapy_agent.admin.request_context import AdminRequestContext

from .core import STATE, send_basicmessage, wallet_id_for_profile

LOGGER = logging.getLogger(__name__)


async def fastpath_send_message(request: web.BaseRequest):
    """Fast-path basic message send (minimal middleware, cached targets)."""
    context: AdminRequestContext = request["context"]
    connection_id = request.match_info["conn_id"]
    body = await request.json()
    content = body.get("content", "")

    try:
        await send_basicmessage(context.profile, connection_id, content)
    except LookupError as err:
        raise web.HTTPNotFound(reason=str(err)) from err
    except Exception as err:  # surface delivery/pack errors to the caller
        LOGGER.exception("fastpath send failed for %s", connection_id)
        raise web.HTTPBadGateway(reason=str(err)) from err

    return web.json_response({})


async def fastpath_stats(request: web.BaseRequest):
    """Return cumulative per-stage timing statistics."""
    return web.json_response(STATE.stats_dict())


async def fastpath_stats_reset(request: web.BaseRequest):
    """Reset timing statistics (keeps the target cache)."""
    STATE.reset_stats()
    return web.json_response({"reset": True})


async def fastpath_cache_clear(request: web.BaseRequest):
    """Drop cached targets for this tenant, or all for base wallet."""
    context: AdminRequestContext = request["context"]
    q_wallet = request.rel_url.query.get("wallet_id")
    if q_wallet:
        n = STATE.clear(wallet_id=q_wallet)
        return web.json_response({"cleared": n, "wallet_id": q_wallet})

    wallet_id = wallet_id_for_profile(context.profile)
    if wallet_id == "base" and not context.profile.settings.get("wallet.id"):
        n = STATE.clear()
        return web.json_response({"cleared": n, "wallet_id": None})
    n = STATE.clear(wallet_id=wallet_id)
    return web.json_response({"cleared": n, "wallet_id": wallet_id})


async def register(app: web.Application):
    """Register routes."""
    app.add_routes(
        [
            web.post(
                "/didcomm-fastpath/connections/{conn_id}/send-message",
                fastpath_send_message,
            ),
            web.get("/didcomm-fastpath/stats", fastpath_stats, allow_head=False),
            web.delete("/didcomm-fastpath/stats", fastpath_stats_reset),
            web.delete("/didcomm-fastpath/cache", fastpath_cache_clear),
        ]
    )


def post_process_routes(app: web.Application):
    """Amend swagger API."""
    if "tags" not in app._state["swagger_dict"]:
        app._state["swagger_dict"]["tags"] = []
    app._state["swagger_dict"]["tags"].append(
        {
            "name": "didcomm-fastpath",
            "description": "Experimental fast-path DIDComm send pipeline",
        }
    )
