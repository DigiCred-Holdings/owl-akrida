"""kanon_storage v1_0 plugin entry."""

import logging

from acapy_agent.config.injection_context import InjectionContext

LOGGER = logging.getLogger(__name__)

WALLET_TYPE = "kanon-storage-anoncreds"
MULTITENANT_WALLET_TYPE = "kanon-storage-multi"

PROFILE_MANAGER_PATH = (
    "kanon_storage.v1_0.profile.manager.KanonStorageProfileManager"
)
MULTITENANT_MANAGER_PATH = (
    "kanon_storage.v1_0.multitenant.manager.KanonStorageMultitenantManager"
)

ANONCREDS_PLUGIN_PATHS = (
    "acapy_agent.anoncreds",
    "acapy_agent.anoncreds.default.did_web",
    "acapy_agent.anoncreds.default.legacy_indy",
    "acapy_agent.anoncreds.revocation",
)


def _install_init_context_patch() -> None:
    """Register anoncreds plugins in PluginRegistry before iteration.

    Mutating `_plugins` during iteration crashes with "OrderedDict mutated",
    so we wrap `init_context` to inject paths before iteration begins.
    """
    from acapy_agent.core.plugin_registry import PluginRegistry

    if getattr(PluginRegistry.init_context, "_kanon_storage_patched", False):
        return

    original_init_context = PluginRegistry.init_context

    async def patched_init_context(self, context):
        wallet_type = (context.settings.get("wallet.type") or "").lower()
        # Inject anoncreds plugins for both the single-tenant kanon storage
        # type and the multitenant variant. In multitenant deployments the
        # agent-level `wallet.type` is `kanon-storage-multi` while subwallets
        # use the single-tenant type — both need ANONCREDS_PLUGIN_PATHS in
        # the registry so anoncreds routes/handlers load on agent boot.
        if wallet_type in (WALLET_TYPE, MULTITENANT_WALLET_TYPE):
            for path in ANONCREDS_PLUGIN_PATHS:
                if path not in self._plugins:
                    self.register_plugin(path)
            LOGGER.info(
                "kanon_storage injected anoncreds plugin set into PluginRegistry"
                " (wallet.type=%s)",
                wallet_type,
            )
        await original_init_context(self, context)

    patched_init_context._kanon_storage_patched = True  # type: ignore[attr-defined]
    PluginRegistry.init_context = patched_init_context


async def setup(context: InjectionContext) -> None:
    """Register the kanon_storage backend with ACA-Py.

    Idempotent: the underlying patch / registry mutations all check a
    `_kanon_storage_patched` flag, so repeated invocations are safe.
    The PluginRegistry patch used to fire at import time, which made any
    `import kanon_storage.v1_0` (tests, REPL, IDE probe) permanently
    affect global plugin registration. Moving the patch into `setup`
    means the patch only fires when ACA-Py actually invokes the plugin.
    """
    LOGGER.debug("> kanon_storage plugin setup...")

    from acapy_agent.core.profile import ProfileManagerProvider
    from acapy_agent.multitenant.manager_provider import MultitenantManagerProvider

    # Defer PluginRegistry mutation until setup runs.
    _install_init_context_patch()

    ProfileManagerProvider.MANAGER_TYPES[WALLET_TYPE] = PROFILE_MANAGER_PATH
    MultitenantManagerProvider.MANAGER_TYPES[MULTITENANT_WALLET_TYPE] = (
        MULTITENANT_MANAGER_PATH
    )

    # Apply runtime monkey-patches for the few hardcoded isinstance checks
    # in core that wouldn't otherwise admit our session class.
    from kanon_storage.v1_0.isinstance_compat import install_compat_patches

    install_compat_patches()

    LOGGER.info(
        "kanon_storage registered: wallet.type=%s -> %s; "
        "multitenant.wallet_type=%s -> %s",
        WALLET_TYPE,
        PROFILE_MANAGER_PATH,
        MULTITENANT_WALLET_TYPE,
        MULTITENANT_MANAGER_PATH,
    )
    LOGGER.debug("< kanon_storage plugin setup.")
