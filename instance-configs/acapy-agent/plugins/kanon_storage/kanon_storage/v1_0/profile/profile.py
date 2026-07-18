"""KanonStorageProfile — concrete acapy_agent.core.profile.Profile."""

from __future__ import annotations

import logging
from typing import Optional

from acapy_agent.config.injection_context import InjectionContext
from acapy_agent.core.profile import Profile
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from kanon_storage.v1_0.config import KanonStorageConfig

LOGGER = logging.getLogger(__name__)


class KanonStorageProfile(Profile):
    """Profile backed by SQLAlchemy + Alembic.

    `BACKEND_NAME` is set to `"kanon-anoncreds"` rather than something
    like `"kanon-storage-anoncreds"`, even though our `wallet.type` is
    `kanon-storage-anoncreds`. The reason: ACA-Py's conductor does
    string-equality dispatch on `profile.BACKEND_NAME` for multiledger /
    verifier-binding decisions (`conductor.py:200`), and only knows
    `askar`, `askar-anoncreds`, and `kanon-anoncreds`. By presenting that
    name we get the correct downstream wiring without forking core. Our
    *manager* registration (`MANAGER_TYPES["kanon-storage-anoncreds"]`)
    stays distinct so `wallet.type` selects us correctly.

    `Profile.is_anoncreds` returns True because the name contains
    "anoncreds".
    """

    BACKEND_NAME = "kanon-anoncreds"

    def __init__(
        self,
        *,
        engine: AsyncEngine,
        session_factory: async_sessionmaker[AsyncSession],
        config: KanonStorageConfig,
        context: Optional[InjectionContext] = None,
        name: Optional[str] = None,
        profile_id: Optional[str] = None,
        created: bool = False,
    ):
        super().__init__(context=context, name=name or profile_id, created=created)
        self._engine = engine
        self._session_factory = session_factory
        self._config = config
        self._profile_id = profile_id or self.name
        self._ledger_pool = None
        self.init_ledger_pool()
        self.bind_providers()

    def init_ledger_pool(self) -> None:
        """Initialize the IndyVDR ledger pool from settings (parity with kanon)."""
        if self.settings.get("ledger.disabled"):
            LOGGER.info("Ledger support is disabled")
            return
        if self.settings.get("ledger.genesis_transactions"):
            from acapy_agent.cache.base import BaseCache
            from acapy_agent.ledger.indy_vdr import IndyVdrLedgerPool

            cache = self._context.injector.inject_or(BaseCache)
            self._ledger_pool = IndyVdrLedgerPool(
                self.settings.get("ledger.pool_name", "default"),
                keepalive=int(self.settings.get("ledger.keepalive", 5)),
                cache=cache,
                genesis_transactions=self.settings.get("ledger.genesis_transactions"),
                read_only=bool(self.settings.get("ledger.read_only", False)),
                socks_proxy=self.settings.get("ledger.socks_proxy"),
            )

    @property
    def profile_id(self) -> str:
        """Tenant identifier for multi-profile-in-one-database deployments."""
        return self._profile_id

    @property
    def engine(self) -> AsyncEngine:
        return self._engine

    @property
    def session_factory(self) -> async_sessionmaker[AsyncSession]:
        return self._session_factory

    @property
    def config(self) -> KanonStorageConfig:
        return self._config

    def bind_providers(self) -> None:
        """Bind storage / wallet / anoncreds / ledger providers.

        Bindings use ClassProvider with string import paths where possible
        so heavy modules (indy_credx, didcomm_messaging) aren't imported
        at profile construction time — they're resolved lazily on first
        `inject()` call. This mirrors the pattern in
        `acapy_agent.askar.profile.AskarProfile.bind_providers`.
        """
        from acapy_agent.cache.base import BaseCache
        from acapy_agent.config.provider import ClassProvider
        from acapy_agent.indy.holder import IndyHolder
        from acapy_agent.indy.issuer import IndyIssuer
        from acapy_agent.indy.verifier import IndyVerifier
        from acapy_agent.ledger.base import BaseLedger
        from acapy_agent.ledger.indy_vdr import IndyVdrLedger, IndyVdrLedgerPool
        from acapy_agent.storage.base import BaseStorage, BaseStorageSearch
        from acapy_agent.storage.vc_holder.base import VCHolder
        from acapy_agent.utils.multi_ledger import (
            get_write_ledger_config_for_profile,
        )
        from weakref import ref

        injector = self._context.injector
        injector.bind_provider(
            BaseStorage,
            ClassProvider(
                "kanon_storage.v1_0.storage.storage_service.KanonStorage",
                ref(self),
            ),
        )
        # KanonStorage implements both BaseStorage and BaseStorageSearch
        # (search_records returns a paged search session). Bind to the same
        # factory so both interfaces resolve to one impl per profile.
        injector.bind_provider(
            BaseStorageSearch,
            ClassProvider(
                "kanon_storage.v1_0.storage.storage_service.KanonStorage",
                ref(self),
            ),
        )
        injector.bind_provider(
            IndyHolder,
            ClassProvider(
                "kanon_storage.v1_0.anoncreds.holder.KanonIndyHolder", ref(self)
            ),
        )
        injector.bind_provider(
            IndyIssuer,
            ClassProvider(
                "kanon_storage.v1_0.anoncreds.issuer.KanonIndyIssuer", ref(self)
            ),
        )

        # VCHolder — the upstream KanonVCHolder ships in acapy_agent and
        # operates over BaseStorage on this profile, so we can reuse it
        # without a plugin-local fork. soft_bind so a downstream plugin
        # (e.g. JSON-LD VC plugin) can override us if it wants.
        injector.soft_bind_provider(
            VCHolder,
            ClassProvider(
                "acapy_agent.storage.vc_holder.kanon.KanonVCHolder",
                ref(self),
            ),
        )

        # IndyVerifier — bound only when a ledger is available, since
        # verification needs schema/cred_def lookups. Mirrors the
        # AskarProfile guard. Deferred class import keeps indy_credx
        # off the import path until the first verify call.
        if self._ledger_pool or self.settings.get("ledger.ledger_config_list"):
            injector.bind_provider(
                IndyVerifier,
                ClassProvider(
                    "acapy_agent.indy.credx.verifier.IndyCredxVerifier",
                    ref(self),
                ),
            )

        # DIDComm v2 — bind the DMPResolver adapter at profile scope so
        # it's available to anything injecting it before a session opens.
        # The SecretsManager + DIDCommMessaging chain is wired per-session
        # in KanonStorageProfileSession._wire_didcomm_v2 because
        # KanonSecretsAdapter needs an active SQLAlchemy session.
        if self.context.settings.get("experiment.didcomm_v2"):
            from acapy_agent.resolver.did_resolver import DIDResolver

            try:
                from didcomm_messaging.resolver import (
                    DIDResolver as DMPResolver,
                )
            except ImportError:
                LOGGER.debug(
                    "didcomm_messaging not installed; skipping DMP resolver bind"
                )
            else:
                injector.bind_provider(
                    DMPResolver,
                    ClassProvider(
                        "acapy_agent.didcomm_v2.adapters.ResolverAdapter",
                        ref(self),
                        ClassProvider.Inject(DIDResolver),
                    ),
                )

        # Ledger binding (parity with kanon/askar profile). Two paths:
        #   * multi-ledger config — pick the write ledger
        #   * single ledger pool initialized from settings
        ledger_list = self.settings.get("ledger.ledger_config_list")
        if ledger_list and len(ledger_list) >= 1:
            write_cfg = get_write_ledger_config_for_profile(settings=self.settings)
            cache = self._context.injector.inject_or(BaseCache)
            injector.bind_provider(
                BaseLedger,
                ClassProvider(
                    IndyVdrLedger,
                    IndyVdrLedgerPool(
                        write_cfg.get("pool_name") or write_cfg.get("id"),
                        keepalive=write_cfg.get("keepalive"),
                        cache=cache,
                        genesis_transactions=write_cfg.get("genesis_transactions"),
                        read_only=write_cfg.get("read_only"),
                        socks_proxy=write_cfg.get("socks_proxy"),
                    ),
                    ref(self),
                ),
            )
            self.settings["ledger.write_ledger"] = write_cfg.get("id")
            if "endorser_alias" in write_cfg and "endorser_did" in write_cfg:
                self.settings["endorser.endorser_alias"] = write_cfg.get(
                    "endorser_alias"
                )
                self.settings["endorser.endorser_public_did"] = write_cfg.get(
                    "endorser_did"
                )
        elif self._ledger_pool:
            injector.bind_provider(
                BaseLedger,
                ClassProvider(IndyVdrLedger, self._ledger_pool, ref(self)),
            )

        # BaseWallet is bound on the *session* (not the profile) because
        # KanonWallet needs an active SQLAlchemy session — see
        # KanonStorageProfileSession._setup.

    def session(self, context: Optional[InjectionContext] = None):
        from kanon_storage.v1_0.profile.session import KanonStorageProfileSession

        return KanonStorageProfileSession(
            self, context=context, is_transaction=False
        )

    def transaction(self, context: Optional[InjectionContext] = None):
        from kanon_storage.v1_0.profile.session import KanonStorageProfileSession

        return KanonStorageProfileSession(
            self, context=context, is_transaction=True
        )

    async def close(self) -> None:
        """Close profile-scoped resources.

        We deliberately do NOT dispose `self._engine`: it comes from the
        module-level `_ENGINE_CACHE` in `profile/manager.py` and is shared
        across every `provision`/`open` call for the same URL. Disposing
        it here would leave the cache entry pointing at a closed engine,
        and the next profile-open would raise "engine is closed" on first
        use (notably hitting pytest fixtures that reuse the same in-memory
        SQLite URL across tests). The multitenant manager owns its own
        engine and disposes it correctly in `multitenant/manager.py:195`;
        single-tenant engines live until process exit alongside the
        cache.
        """
        await super().close()

    async def remove(self) -> None:
        """Drop all rows for this profile_id across all tables.

        Runs every per-table DELETE in a single tx — on any failure the
        operator gets a clean rollback and the tenant is left in the
        prior state (not half-cleaned). Per-table row counts are logged
        so operators can audit what was removed.
        """
        from sqlalchemy import delete

        # Walk metadata so tables added later are covered automatically.
        metadata = self._tenant_metadata()
        per_table: list[tuple[str, int]] = []
        async with self._session_factory.begin() as sess:
            for table in metadata.sorted_tables:
                if "profile_id" not in table.c:
                    continue
                result = await sess.execute(
                    delete(table).where(table.c.profile_id == self._profile_id)
                )
                count = int(result.rowcount or 0)
                per_table.append((table.name, count))
        LOGGER.info(
            "kanon_storage profile remove: profile_id=%s tables=%s",
            self._profile_id,
            ", ".join(f"{n}={c}" for n, c in per_table),
        )

    def _tenant_metadata(self):
        from kanon_storage.v1_0.db.models.base_pg import BasePgModel
        from kanon_storage.v1_0.db.models.base_sqlite import BaseSqliteModel

        if self._config.dialect == "postgresql":
            return BasePgModel.metadata
        return BaseSqliteModel.metadata
