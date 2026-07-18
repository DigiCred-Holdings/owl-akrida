"""didcomm_fastpath — experimental fast-path DIDComm v1 send pipeline for ACA-Py.

Registers admin routes under /didcomm-fastpath (see routes.py).
"""

import logging

from acapy_agent.config.injection_context import InjectionContext

LOGGER = logging.getLogger(__name__)


async def setup(context: InjectionContext):
    """Plugin entry point; route registration is handled via routes.register."""
    LOGGER.info("didcomm_fastpath plugin loaded")
