"""KanonStorageProfileManager — entry point for ACA-Py's wallet_config()."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Mapping, Optional, Tuple

from acapy_agent.config.injection_context import InjectionContext
from acapy_agent.core.profile import Profile, ProfileManager
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from kanon_storage.v1_0.config import KanonStorageConfig
# Single source of truth for URL redaction lives in db.engine — re-export
# under the legacy `_redact_url` name for back-compat with anything that
# imported it from this module.
from kanon_storage.v1_0.db.engine import (
    _safe_url as _redact_url,
    make_engine_and_session_factory,
)

LOGGER = logging.getLogger(__name__)

# Module-level engine cache. ACA-Py invokes ProfileManager.open() and
# provision() on every profile-open path; creating a fresh engine per
# call leaks a 30+30 (default) connection pool each time → PG runs out
# of slots. Keying by URL means same-URL callers (typical: one DB URL
# per deployment) share one engine + one pool — matching how the
# multitenant manager already does it.
_ENGINE_CACHE: dict[str, Tuple[AsyncEngine, async_sessionmaker[AsyncSession]]] = {}
_ENGINE_CACHE_LOCK = asyncio.Lock()


async def _get_or_create_engine(
    cfg: KanonStorageConfig,
) -> Tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    cached = _ENGINE_CACHE.get(cfg.database_url)
    if cached is not None:
        return cached
    async with _ENGINE_CACHE_LOCK:
        cached = _ENGINE_CACHE.get(cfg.database_url)
        if cached is not None:
            return cached
        engine, factory = make_engine_and_session_factory(cfg)
        _ENGINE_CACHE[cfg.database_url] = (engine, factory)
        LOGGER.info(
            "kanon_storage: engine cached for %s (pool=%d/+%d)",
            _redact_url(cfg.database_url),
            cfg.pool_size,
            cfg.pool_overflow,
        )
        return engine, factory


class KanonStorageProfileManager(ProfileManager):
    """Provision/open a KanonStorageProfile."""

    async def provision(
        self,
        context: InjectionContext,
        config: Optional[Mapping[str, Any]] = None,
    ) -> Profile:
        cfg = self._resolve_config(context, config)
        engine, session_factory = await _get_or_create_engine(cfg)

        if cfg.auto_migrate:
            await self._ensure_schema(engine, cfg)

        from kanon_storage.v1_0.profile.profile import KanonStorageProfile

        profile = KanonStorageProfile(
            engine=engine,
            session_factory=session_factory,
            config=cfg,
            context=context,
            profile_id=self._profile_id(context, config),
            created=True,
        )
        LOGGER.debug(
            "kanon_storage profile provisioned: dialect=%s profile_id=%s",
            cfg.dialect,
            profile.profile_id,
        )
        return profile

    async def open(
        self,
        context: InjectionContext,
        config: Optional[Mapping[str, Any]] = None,
    ) -> Profile:
        cfg = self._resolve_config(context, config)
        engine, session_factory = await _get_or_create_engine(cfg)

        if cfg.auto_migrate:
            await self._ensure_schema(engine, cfg)

        from kanon_storage.v1_0.profile.profile import KanonStorageProfile

        profile = KanonStorageProfile(
            engine=engine,
            session_factory=session_factory,
            config=cfg,
            context=context,
            profile_id=self._profile_id(context, config),
            created=False,
        )
        LOGGER.debug(
            "kanon_storage profile opened: dialect=%s profile_id=%s",
            cfg.dialect,
            profile.profile_id,
        )
        # Drain any outbox rows left over from a previous run (crash recovery).
        # Failures here are logged but don't block startup; pending rows stay
        # in the table for the next replay attempt.
        # Always log the count (including zero) so operators can confirm the
        # replay path ran on every profile open — silence on a zero-row drain
        # is indistinguishable from a skipped/failed replay otherwise.
        try:
            from kanon_storage.v1_0.outbox import Outbox

            outbox = Outbox(profile_id=profile.profile_id, dialect=cfg.dialect)
            applied = await outbox.replay_pending(profile)
            if applied:
                LOGGER.info(
                    "kanon_storage outbox replay on open: profile_id=%s applied=%d",
                    profile.profile_id,
                    applied,
                )
            else:
                LOGGER.debug(
                    "kanon_storage outbox replay on open: profile_id=%s applied=0",
                    profile.profile_id,
                )
        except Exception as err:
            LOGGER.warning(
                "kanon_storage outbox replay failed on open (non-fatal): "
                "profile_id=%s err=%s",
                profile.profile_id,
                err,
            )
        return profile

    @staticmethod
    def _profile_id(
        context: InjectionContext, config: Optional[Mapping[str, Any]]
    ) -> str:
        """Resolve the per-tenant profile_id used to scope every record.

        Order:
          * `wallet.id` / `wallet.name` from config kwargs (multitenant — innkeeper sets these)
          * `wallet.id` / `wallet.name` from settings (single-tenant)
        Raises ConfigError if neither is set. There is no "default" fallback:
        a silent default in a multitenant deployment merges every tenant's
        records into one profile, which is unrecoverable (cred-def-scoped
        BJJ keys, etc.).
        """
        from kanon_storage.v1_0.config import ConfigError

        if config:
            for key in ("wallet.id", "wallet.name", "name", "profile_id"):
                if config.get(key):
                    return str(config[key])
        for key in ("wallet.id", "wallet.name"):
            if context.settings.get_value(key):
                return str(context.settings.get_value(key))
        raise ConfigError(
            "kanon_storage: cannot resolve profile_id — no wallet.id, "
            "wallet.name, name, or profile_id in config kwargs or settings. "
            "Multitenant deployments must invoke the multitenant manager "
            "(which sets wallet.id from the wallet_record); single-tenant "
            "deployments must set wallet.name in the base settings."
        )

    @staticmethod
    def _resolve_config(
        context: InjectionContext, config: Optional[Mapping[str, Any]]
    ) -> KanonStorageConfig:
        """Build a KanonStorageConfig from context + override config."""
        # The profile manager is invoked with the global settings; per-profile
        # overrides come via `config`. We merge by precedence: config kwargs
        # > settings > env > defaults (env/defaults are inside from_settings).
        from dataclasses import fields, replace

        from kanon_storage.v1_0.config import KS_PREFIX, ConfigError

        cfg = KanonStorageConfig.from_settings(context.settings)
        if not config:
            return cfg

        valid_field_names = {f.name for f in fields(KanonStorageConfig)}
        overrides: dict[str, Any] = {}
        for k, v in config.items():
            if not k.startswith(KS_PREFIX):
                continue
            field_name = k[len(KS_PREFIX):]
            if field_name not in valid_field_names:
                # Misspellings such as `kanon_storage.database_ul` would
                # otherwise silently no-op and operators would never know
                # their per-profile override didn't apply.
                raise ConfigError(
                    f"Unknown per-profile override {k!r}: no matching field "
                    f"on KanonStorageConfig. Known fields: "
                    f"{sorted(valid_field_names)}"
                )
            overrides[field_name] = v

        if not overrides:
            return cfg

        # `master_key` may arrive as a str — normalize the same way
        # `from_settings` does so override-via-str works.
        if "master_key" in overrides:
            mk = overrides["master_key"]
            if isinstance(mk, str):
                mk = mk.encode("utf-8")
            if not isinstance(mk, (bytes, bytearray)) or len(mk) != 32:
                raise ConfigError(
                    "Per-profile kanon_storage.master_key override must be "
                    "exactly 32 bytes."
                )
            overrides["master_key"] = bytes(mk)

        return replace(cfg, **overrides)

    @staticmethod
    async def _ensure_schema(engine, cfg: KanonStorageConfig) -> None:
        """Create tables if missing.

        Only runs `metadata.create_all` when the schema is empty (no
        managed tables present). Re-running `create_all` on every profile
        open in a multitenant deployment acquires metadata locks
        unnecessarily and races at boot when two ACA-Py instances start
        together. Operators with a populated schema should drive
        migrations via Alembic; `auto_migrate` is now strictly a
        bootstrap-on-empty helper.
        """
        if cfg.dialect == "postgresql":
            from kanon_storage.v1_0.db.models import base_pg

            metadata = base_pg.BasePgModel.metadata
            from kanon_storage.v1_0.db.models import (  # noqa: F401
                did_pg,
                generic_record_pg,
                key_pg,
                outbox_pg,
                storage_version_pg,
            )
        else:
            from kanon_storage.v1_0.db.models import base_sqlite

            metadata = base_sqlite.BaseSqliteModel.metadata
            from kanon_storage.v1_0.db.models import (  # noqa: F401
                did_sqlite,
                generic_record_sqlite,
                key_sqlite,
                outbox_sqlite,
                storage_version_sqlite,
            )

        managed_table_names = set(metadata.tables.keys())

        def _check_existing(sync_conn) -> bool:
            from sqlalchemy import inspect as sa_inspect

            existing = set(sa_inspect(sync_conn).get_table_names())
            return bool(managed_table_names & existing)

        async with engine.begin() as conn:
            already_has_tables = await conn.run_sync(_check_existing)
            if already_has_tables:
                LOGGER.debug(
                    "kanon_storage auto_migrate: schema already present "
                    "(dialect=%s); skipping create_all. Run Alembic for "
                    "in-place migrations.",
                    cfg.dialect,
                )
                return
            LOGGER.info(
                "kanon_storage auto_migrate: bootstrapping empty schema "
                "(dialect=%s, url=%s)",
                cfg.dialect,
                _redact_url(cfg.database_url),
            )
            await conn.run_sync(metadata.create_all)
        LOGGER.info(
            "kanon_storage auto_migrate: schema bootstrapped (dialect=%s, "
            "%d tables)",
            cfg.dialect,
            len(metadata.tables),
        )
