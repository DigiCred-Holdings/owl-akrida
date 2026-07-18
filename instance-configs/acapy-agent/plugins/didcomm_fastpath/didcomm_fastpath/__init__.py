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

# Any ConnRecord state change (update, DID rotation, deletion) can stale the
# cached endpoint/keys, so evict on every connections record event.
CONN_RECORD_EVENT_PATTERN = re.compile("^acapy::record::connections::.*$")


async def on_connection_event(profile: Profile, event: Event):
    """Evict the cached target for a connection whose record changed."""
    connection_id = (event.payload or {}).get("connection_id")
    if not connection_id:
        return

    from .core import STATE

    if STATE.invalidate(connection_id):
        LOGGER.debug(
            "didcomm_fastpath: evicted cached target for %s (%s)",
            connection_id,
            event.topic,
        )


async def setup(context: InjectionContext):
    """Plugin entry point: subscribe cache eviction to connection events."""
    event_bus = context.inject_or(EventBus)
    if event_bus:
        event_bus.subscribe(CONN_RECORD_EVENT_PATTERN, on_connection_event)
    else:
        LOGGER.warning(
            "didcomm_fastpath: no EventBus available; "
            "cache eviction relies on FASTPATH_CACHE_TTL only"
        )
    LOGGER.info("didcomm_fastpath plugin loaded")
