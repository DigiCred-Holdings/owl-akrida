"""ClassProvider target — builds a KanonStorage from the profile's engine."""

from __future__ import annotations

from kanon_storage.v1_0.storage.storage_service import KanonStorage


def create_storage_for_profile(profile_ref) -> KanonStorage:
    """ClassProvider entry point.

    `profile_ref` is a weakref to the KanonStorageProfile; we resolve it
    and build a fresh KanonStorage bound to the profile's session factory.
    Any session-scoped overrides happen in
    `KanonStorageProfileSession._setup`, which rebinds BaseStorage on the
    session injector to a KanonStorage that uses the active session.
    """
    profile = profile_ref()
    if profile is None:
        raise RuntimeError("KanonStorageProfile has been garbage-collected")
    return KanonStorage(
        profile_id=profile.profile_id,
        dialect=profile.config.dialect,
        session_factory=profile.session_factory,
    )
