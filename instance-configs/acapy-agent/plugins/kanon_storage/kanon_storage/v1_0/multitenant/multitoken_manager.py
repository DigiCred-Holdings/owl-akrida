"""KanonStorageMultitokenMultitenantManager — drop-in for ``multitenant_provider``."""

from __future__ import annotations

import logging
from typing import Optional

from acapy_agent.config.injection_context import InjectionContext
from acapy_agent.core.profile import Profile
from acapy_agent.wallet.models.wallet_record import WalletRecord

from kanon_storage.v1_0.multitenant.manager import KanonStorageMultitenantManager

LOGGER = logging.getLogger(__name__)


def _load_multitoken_handler():
    """Import MulittokenHandler lazily so module import never fails.

    Returns the class on success; raises ImportError with a clear message
    on failure (the caller is the runtime path that actually needs it,
    so a deferred error is reasonable).
    """
    try:
        from multitenant_provider.v1_0.manager import MulittokenHandler

        return MulittokenHandler
    except ImportError as err:  # pragma: no cover - import-time guard
        raise ImportError(
            "multitenant_provider plugin is required for "
            "KanonStorageMultitokenMultitenantManager. Install the "
            "acapy-plugins multitenant_provider package or switch the "
            "configured manager class."
        ) from err


class KanonStorageMultitokenMultitenantManager(KanonStorageMultitenantManager):
    """Kanon-storage multitenant manager + multitenant_provider JWT tokens."""

    def __init__(self, profile: Profile):
        super().__init__(profile)
        self.logger = logging.getLogger(__class__.__name__)
        # multitenant_provider's MulittokenHandler.create_wallet calls
        # `self._manager._super_create_wallet(settings, mode)` for the
        # underlying provisioning, then layers token logic on top.
        # We expose `_super_create_wallet` pointing at the base
        # `BaseMultitenantManager.create_wallet` (which calls our
        # `get_wallet_profile(provision=True)`).
        self._super_create_wallet = super().create_wallet

    async def create_auth_token(
        self, wallet_record: WalletRecord, wallet_key: Optional[str] = None
    ) -> str:
        self.logger.debug("> create_auth_token")
        handler_cls = _load_multitoken_handler()
        handler = handler_cls(self)
        token = await handler.create_auth_token(wallet_record, wallet_key)
        self.logger.debug("< create_auth_token")
        return token

    async def get_profile_for_token(
        self, context: InjectionContext, token: str
    ) -> Profile:
        self.logger.debug("> get_profile_for_token")
        handler_cls = _load_multitoken_handler()
        handler = handler_cls(self)
        profile = await handler.get_profile_for_token(context, token)
        self.logger.debug("< get_profile_for_token")
        return profile

    async def create_wallet(
        self,
        settings: dict,
        key_management_mode: str,
    ) -> WalletRecord:
        self.logger.debug("> create_wallet")
        handler_cls = _load_multitoken_handler()
        handler = handler_cls(self)
        wallet_record = await handler.create_wallet(settings, key_management_mode)
        self.logger.debug("< create_wallet")
        return wallet_record
