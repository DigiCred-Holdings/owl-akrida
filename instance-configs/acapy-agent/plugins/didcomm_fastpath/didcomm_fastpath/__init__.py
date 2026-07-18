"""didcomm_fastpath — experimental fast-path DIDComm v1 send pipeline for ACA-Py.

Registers admin routes under /didcomm-fastpath (see routes.py) and evicts
cached connection targets whenever the underlying ConnRecord changes.
"""

import logging
import re

from acapy_agent.config.injection_context import InjectionContext
from acapy_agent.core.event_bus import Event, EventBus
from acapy_agent.core.profile import Profile

LOGGER = logging.getLogger(__name__)

CONN_RECORD_EVENT_PATTERN = re.compile("^acapy::record::connections::.*$")


async def on_connection_event(profile: Profile, event: Event):
    """Evict the cached target for a connection whose record changed."""
    connection_id = (event.payload or {}).get("connection_id")
    if not connection_id:
        return

    from .core import STATE, wallet_id_for_profile

    # profile is the local tenant subwallet that owns this ConnRecord.
    wallet_id = wallet_id_for_profile(profile)
    if STATE.invalidate(connection_id, wallet_id=wallet_id):
        LOGGER.debug(
            "didcomm_fastpath: evicted cached target for tenant=%s connection=%s (%s)",
            wallet_id,
            connection_id,
            event.topic,
        )


async def on_wallet_removed(profile: Profile, event: Event):
    """Clear all cache entries for a removed local tenant subwallet."""
    from .core import STATE, wallet_id_for_profile

    payload = event.payload or {}
    wallet_id = payload.get("wallet_id") or wallet_id_for_profile(profile)
    if not wallet_id:
        return

    n = STATE.invalidate_wallet(str(wallet_id))
    if n:
        LOGGER.info(
            "didcomm_fastpath: tenant wallet_removed cleared %d entries for %s",
            n,
            wallet_id,
        )


async def setup(context: InjectionContext):
    """Plugin entry point: subscribe cache eviction to connection events."""
    from .core import STATE, WALLET_REMOVED_TOPIC

    event_bus = context.inject_or(EventBus)
    if event_bus:
        event_bus.subscribe(CONN_RECORD_EVENT_PATTERN, on_connection_event)
        event_bus.subscribe(
            re.compile(f"^{re.escape(WALLET_REMOVED_TOPIC)}$"), on_wallet_removed
        )
    else:
        LOGGER.warning(
            "didcomm_fastpath: no EventBus available; "
            "cache eviction relies on FASTPATH_CACHE_TTL / LRU / invalidate_wallet"
        )
    STATE.start_sweeper()
    LOGGER.info("didcomm_fastpath plugin loaded")
