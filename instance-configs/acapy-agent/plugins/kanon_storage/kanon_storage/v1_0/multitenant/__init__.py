"""kanon_storage multitenant manager."""

from kanon_storage.v1_0.multitenant.manager import KanonStorageMultitenantManager

__all__ = ["KanonStorageMultitenantManager"]

try:
    from kanon_storage.v1_0.multitenant.multitoken_manager import (
        KanonStorageMultitokenMultitenantManager,  # noqa: F401
    )

    __all__.append("KanonStorageMultitokenMultitenantManager")
except ImportError:  # pragma: no cover
    # multitenant_provider not installed — multitoken manager unavailable.
    # Single-tenant or non-JWT-token deployments don't need it.
    pass
