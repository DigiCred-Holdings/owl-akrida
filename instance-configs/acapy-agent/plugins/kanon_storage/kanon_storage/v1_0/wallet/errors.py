"""Wallet-layer error types — pass-through to ACA-Py's wallet errors."""

from __future__ import annotations

from acapy_agent.wallet.error import (
    WalletDuplicateError,  # noqa: F401  (re-export)
    WalletError,
    WalletNotFoundError,  # noqa: F401  (re-export)
)


class WalletKeyTypeError(WalletError):
    """Unsupported key type."""

    pass
