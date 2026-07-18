"""Runtime monkey-patches so ACA-Py core admits our session class."""

from __future__ import annotations

import logging
from typing import Any

LOGGER = logging.getLogger(__name__)

_INSTALLED = False


def install_compat_patches() -> None:
    """Apply runtime patches. Safe to call multiple times."""
    global _INSTALLED
    if _INSTALLED:
        return

    from kanon_storage.v1_0.profile.session import KanonStorageProfileSession

    # 1) Anoncreds revocation isinstance check.
    try:
        import acapy_agent.anoncreds.revocation.revocation as core_rev

        _patch_isinstance_name(
            core_rev,
            "AskarAnonCredsProfileSession",
            extra_classes=(KanonStorageProfileSession,),
        )
        _patch_isinstance_name(
            core_rev,
            "KanonAnonCredsProfileSession",
            extra_classes=(KanonStorageProfileSession,),
        )
    except ImportError:
        LOGGER.debug("anoncreds revocation module not present; skipping patch")

    # 2) DIDComm v2 secrets adapter (only matters once we serve DIDComm v2).
    try:
        import acapy_agent.didcomm_v2.adapters as core_dc2

        _patch_isinstance_name(
            core_dc2,
            "AskarProfileSession",
            extra_classes=(KanonStorageProfileSession,),
        )
    except ImportError:
        LOGGER.debug("didcomm_v2 adapters module not present; skipping patch")

    # 3) Multitenant admin route validators — `wallet_type` and `wallet_key_derivation`
    # are validated against snapshots taken at schema-import time. Our wallet type
    # `kanon-storage-anoncreds` won't be in those snapshots even though we
    # mutate `MANAGER_TYPES` at plugin load. Patch each schema's validator
    # to include our key.
    _patch_multitenant_wallet_type_validator()

    # 4) Anoncreds-profile guards in admin routes. CRMS uses the LEGACY
    # /schemas, /credential-definitions, and `filter:{indy:...}` cred ex
    # paths. Upstream's `is_anoncreds_profile_raise_web_exception` 403s
    # any profile where `is_anoncreds=True` — that includes ours
    # (BACKEND_NAME="kanon-anoncreds"). Bypass the guard for our profile
    # so legacy + anoncreds routes coexist on the same wallet.
    _patch_anoncreds_profile_guards()

    _INSTALLED = True
    LOGGER.info("kanon_storage isinstance compat patches installed")


def _patch_anoncreds_profile_guards() -> None:
    """Replace the two guard functions at every callsite that imported them.

    `from acapy_agent.utils.profiles import is_anoncreds_profile_raise_web_exception`
    binds the function reference into the consumer module at *import time*.
    Replacing only `acapy_agent.utils.profiles.<name>` afterwards leaves
    every consumer holding a stale reference. We walk `sys.modules`,
    detect modules that bound either guard, and rebind to no-ops that
    accept our profile.

    Our profile is anoncreds-capable (it binds IndyCredxVerifier and
    serves `/anoncreds/*` routes), AND legacy-compatible (it binds
    KanonIndyHolder/KanonIndyIssuer and supports `cred_def_sent`-style
    storage). Both guards become no-ops for instances of our profile.
    """
    import sys

    try:
        from acapy_agent.utils import profiles as core_profiles
        from kanon_storage.v1_0.profile.profile import KanonStorageProfile
    except ImportError as exc:
        LOGGER.debug("Cannot patch anoncreds guards (%s)", exc)
        return

    original_block_anoncreds = core_profiles.is_anoncreds_profile_raise_web_exception
    original_require_anoncreds = core_profiles.is_not_anoncreds_profile_raise_web_exception

    def kanon_block_anoncreds(profile) -> None:
        if isinstance(profile, KanonStorageProfile):
            return
        original_block_anoncreds(profile)

    def kanon_require_anoncreds(profile) -> None:
        if isinstance(profile, KanonStorageProfile):
            return
        original_require_anoncreds(profile)

    # Replace at the source module so later imports see the patched version.
    core_profiles.is_anoncreds_profile_raise_web_exception = kanon_block_anoncreds
    core_profiles.is_not_anoncreds_profile_raise_web_exception = kanon_require_anoncreds

    # Rebind in every already-loaded consumer module that imported by name.
    rebound = 0
    for mod in list(sys.modules.values()):
        if mod is None or mod is core_profiles:
            continue
        for name, replacement in (
            ("is_anoncreds_profile_raise_web_exception", kanon_block_anoncreds),
            ("is_not_anoncreds_profile_raise_web_exception", kanon_require_anoncreds),
        ):
            current = getattr(mod, name, None)
            if current is None:
                continue
            if current is replacement:
                continue
            if current is original_block_anoncreds or current is original_require_anoncreds:
                setattr(mod, name, replacement)
                rebound += 1
    LOGGER.info(
        "kanon_storage rebinded anoncreds-profile guards in %d modules", rebound
    )


def _patch_multitenant_wallet_type_validator() -> None:
    """Add 'kanon-storage-anoncreds' to the OneOf snapshot in mt admin routes."""
    try:
        import acapy_agent.multitenant.admin.routes as mt_routes
    except ImportError:
        return

    from kanon_storage.v1_0 import WALLET_TYPE
    from marshmallow.validate import OneOf

    schema_classes = [
        getattr(mt_routes, name, None)
        for name in ("CreateWalletRequestSchema", "UpdateWalletRequestSchema")
    ]
    for schema_cls in schema_classes:
        if schema_cls is None:
            continue
        for field_name in ("wallet_type",):
            field = schema_cls._declared_fields.get(field_name)
            if field is None:
                continue
            for validator in field.validators or ():
                if isinstance(validator, OneOf):
                    if WALLET_TYPE not in validator.choices:
                        validator.choices = list(validator.choices) + [WALLET_TYPE]
                        LOGGER.debug(
                            "patched %s.%s.validate to include %s",
                            schema_cls.__name__,
                            field_name,
                            WALLET_TYPE,
                        )


def _patch_isinstance_name(module: Any, attr: str, *, extra_classes: tuple) -> None:
    """Rebind `module.attr` so `isinstance(x, module.attr)` admits extra classes.

    If `module.attr` is currently a single class, replace it with a tuple
    `(original, *extra_classes)`. If it's already a tuple (we patched
    already, or core ships one), append.
    """
    current = getattr(module, attr, None)
    if current is None:
        LOGGER.debug("%s.%s missing; nothing to patch", module.__name__, attr)
        return
    if isinstance(current, tuple):
        merged = current + tuple(c for c in extra_classes if c not in current)
    else:
        merged = (current, *extra_classes)
    setattr(module, attr, merged)
    LOGGER.debug(
        "patched %s.%s -> tuple of %d classes", module.__name__, attr, len(merged)
    )
