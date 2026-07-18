"""KanonStorageMultitenantManager — profile_id-scoped tenants."""

from __future__ import annotations

import asyncio
import logging
from typing import Iterable, Optional

from acapy_agent.config.injection_context import InjectionContext
from acapy_agent.core.profile import Profile
from acapy_agent.multitenant.base import BaseMultitenantManager
from acapy_agent.wallet.models.wallet_record import WalletRecord
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from kanon_storage.v1_0.config import KanonStorageConfig
from kanon_storage.v1_0.db.engine import make_engine_and_session_factory
from kanon_storage.v1_0.profile.profile import KanonStorageProfile

LOGGER = logging.getLogger(__name__)

# Setting key name for `wallet.type`. Distinct from the `WALLET_TYPE`
# *value* exported by `kanon_storage.v1_0.__init__` (which identifies
# the manager registration). Rename clarifies intent.
WALLET_TYPE_SETTING_KEY = "wallet.type"
# Legacy alias kept for back-compat with any external imports.
WALLET_TYPE_KEY = WALLET_TYPE_SETTING_KEY


_FASTPATH_WALLET_REMOVED_TOPIC = "acapy::didcomm_fastpath::wallet_removed"


async def _notify_fastpath_wallet_removed(profile: Profile, wallet_id: str) -> None:
    """Best-effort clear of didcomm_fastpath cache for a removed local tenant.

    Soft-imports the plugin so kanon_storage has no hard dependency on it.
    Direct invalidate is primary; EventBus notify is for other subscribers.
    """
    if not wallet_id:
        return

    topic = _FASTPATH_WALLET_REMOVED_TOPIC
    try:
        from didcomm_fastpath.v1_0.core import STATE, WALLET_REMOVED_TOPIC

        STATE.invalidate_wallet(wallet_id)
        topic = WALLET_REMOVED_TOPIC
    except ImportError:
        pass
    except Exception:  # pragma: no cover
        LOGGER.debug(
            "kanon_storage: didcomm_fastpath invalidate_wallet failed",
            exc_info=True,
        )

    try:
        from acapy_agent.core.event_bus import Event, EventBus

        event_bus = None
        if hasattr(profile, "inject_or"):
            try:
                event_bus = profile.inject_or(EventBus)
            except Exception:
                event_bus = None
        if event_bus is not None:
            await event_bus.notify(
                profile,
                Event(topic, {"wallet_id": wallet_id}),
            )
    except Exception:  # pragma: no cover
        LOGGER.debug(
            "kanon_storage: wallet_removed event publish failed",
            exc_info=True,
        )


class KanonStorageMultitenantManager(BaseMultitenantManager):
    """Multitenant manager backed by the kanon_storage SQLAlchemy DB."""

    def __init__(self, profile: Profile):
        super().__init__(profile)
        self._engine: Optional[AsyncEngine] = None
        self._session_factory: Optional[async_sessionmaker[AsyncSession]] = None
        self._config: Optional[KanonStorageConfig] = None
        self._open_profiles: dict[str, KanonStorageProfile] = {}
        # Per-wallet open locks so two coroutines calling
        # `get_wallet_profile(wallet_id)` concurrently don't both build
        # a fresh KanonStorageProfile and stomp each other in the cache
        # (which would leak the loser — it still holds context refs).
        self._open_locks: dict[str, asyncio.Lock] = {}
        self._open_locks_lock = asyncio.Lock()

    @property
    def open_profiles(self) -> Iterable[Profile]:
        """Return open tenant profiles."""
        return list(self._open_profiles.values())

    def reset_settings(self, wallet_record: WalletRecord) -> dict:
        """Return settings to wipe before merging the wallet_record settings.

        Mirrors the `reset_settings` dict used by the upstream
        `MultitenantManager.get_wallet_profile`. Subclasses can override
        to extend or replace these. Returned dict is meant to be
        `.extend()`-ed onto the base context settings before the
        wallet-record-specific settings are applied.

        Args:
            wallet_record: The wallet record being opened (unused by the
                default impl; available for subclass overrides that want
                per-tenant behavior).

        Returns:
            dict mapping setting key to None/False that overrides any
            stale values inherited from the base profile.
        """
        return {
            "wallet.recreate": False,
            "wallet.seed": None,
            "wallet.rekey": None,
            "wallet.name": None,
            WALLET_TYPE_KEY: None,
            "mediation.open": None,
            "mediation.invite": None,
            "mediation.default_id": None,
            "mediation.clear": None,
        }

    async def _ensure_shared_engine(self, base_context: InjectionContext) -> None:
        """Lazily create the shared engine + session factory + schema."""
        if self._engine is not None:
            return
        self._config = KanonStorageConfig.from_settings(base_context.settings)
        self._engine, self._session_factory = make_engine_and_session_factory(
            self._config
        )
        if self._config.auto_migrate:
            from kanon_storage.v1_0.profile.manager import KanonStorageProfileManager

            await KanonStorageProfileManager._ensure_schema(self._engine, self._config)
        LOGGER.info(
            "kanon_storage multitenant: shared engine ready (dialect=%s)",
            self._config.dialect,
        )

    async def _get_open_lock(self, wallet_id: str) -> asyncio.Lock:
        """Return (creating if needed) the per-wallet open lock."""
        async with self._open_locks_lock:
            lock = self._open_locks.get(wallet_id)
            if lock is None:
                lock = asyncio.Lock()
                self._open_locks[wallet_id] = lock
            return lock

    async def get_wallet_profile(
        self,
        base_context: InjectionContext,
        wallet_record: WalletRecord,
        extra_settings: Optional[dict] = None,
        *,
        provision: bool = False,
    ) -> Profile:
        """Open or provision a tenant Profile bound to wallet_record.wallet_id."""
        await self._ensure_shared_engine(base_context)
        wallet_id = wallet_record.wallet_id

        cached = self._open_profiles.get(wallet_id)
        if cached and not provision:
            return cached

        # Serialize open-or-provision per wallet_id so two concurrent
        # callers don't both construct + cache a fresh KanonStorageProfile.
        lock = await self._get_open_lock(wallet_id)
        async with lock:
            # Re-check inside the lock — another coroutine may have
            # populated the cache while we were waiting.
            cached = self._open_profiles.get(wallet_id)
            if cached and not provision:
                return cached

            # Build a tenant-scoped context: base + reset + wallet_record + extras.
            # Order matches MultitenantManager.get_wallet_profile so behavior
            # is consistent across backends.
            context = base_context.copy()
            merged_extra = dict(extra_settings or {})
            merged_extra["admin.webhook_urls"] = self.get_webhook_urls(
                base_context, wallet_record
            )
            merged_extra["wallet.id"] = wallet_id
            # Mirror SingleWalletKanonMultitenantManager: stamp the per-tenant
            # askar_profile so any code path that resolves askar profile name
            # from settings (legacy KanonAnonCreds wiring, telemetry) sees the
            # tenant-scoped wallet_id rather than the base manager's.
            merged_extra["wallet.askar_profile"] = wallet_id
            context.settings = (
                context.settings.extend(self.reset_settings(wallet_record))
                .extend(wallet_record.settings)
                .extend(merged_extra)
            )

            profile = KanonStorageProfile(
                engine=self._engine,
                session_factory=self._session_factory,
                config=self._config,
                context=context,
                profile_id=wallet_id,
                name=wallet_record.wallet_name or wallet_id,
                created=provision,
            )
            self._open_profiles[wallet_id] = profile
            LOGGER.info(
                "kanon_storage tenant profile %s: %s",
                "provisioned" if provision else "opened",
                wallet_id,
            )
            return profile

    async def remove_wallet_profile(self, profile: Profile) -> None:
        """Remove all rows scoped to this tenant + drop from open cache."""
        # Read wallet_id from settings first (parity with
        # MultitenantManager.remove_wallet_profile), fall back to the
        # profile attribute / name so tests that pass a bare profile
        # without `wallet.id` in settings still work.
        wallet_id = (
            profile.settings.get_str("wallet.id")
            or getattr(profile, "profile_id", None)
            or profile.name
        )
        await profile.remove()  # tenant-scoped cascade delete
        self._open_profiles.pop(wallet_id, None)
        # Optional: clear didcomm_fastpath in-process key cache for this
        # local tenant subwallet. Soft-import — no hard dependency.
        await _notify_fastpath_wallet_removed(profile, wallet_id)
        LOGGER.info("kanon_storage tenant profile removed: %s", wallet_id)

    async def close(self) -> None:
        """Dispose the shared engine and clear the open-profile cache.

        Called on agent shutdown via the multitenant lifecycle. Idempotent —
        a second call is a no-op once the engine is None.
        """
        self._open_profiles.clear()
        if self._engine is not None:
            engine = self._engine
            self._engine = None
            self._session_factory = None
            await engine.dispose()
            LOGGER.info("kanon_storage multitenant: shared engine disposed")

    async def __aenter__(self) -> "KanonStorageMultitenantManager":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        """Async context-manager exit — disposes the shared engine."""
        await self.close()
