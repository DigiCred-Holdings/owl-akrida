"""KanonStorageProfileSession — holds an AsyncSession (transactional or not)."""

from __future__ import annotations

import asyncio
import logging
import time
import weakref
from typing import Optional

from acapy_agent.config.injection_context import InjectionContext
from acapy_agent.core.profile import ProfileSession
from sqlalchemy.ext.asyncio import AsyncSession

LOGGER = logging.getLogger(__name__)

# Tracks every AsyncSession we open so a sweeper can close any that leaked
# past `__aexit__` (typically when a parent task is cancelled mid-session
# and Python skips the context-manager exit). Without this, the GC closes
# the underlying asyncpg connection — and SQLAlchemy 2.0's autobegun
# transaction is silently *rolled back*, so any record.save() inside that
# session vanishes. Kept as weakrefs so this set never extends a session's
# lifetime.
_OPEN_SESSIONS: "weakref.WeakSet[AsyncSession]" = weakref.WeakSet()


def _schedule_close(sa_session: AsyncSession, profile_id: str) -> None:
    """Schedule `sa_session.close()` on the running event loop.

    Used as a `weakref.finalize` callback. By the time this runs the
    `KanonStorageProfileSession` has already been GC'd, so we can't reach
    its profile / context any more — we get the AsyncSession + profile_id
    captured in closure args.

    The close is wrapped so we both (a) commit any pending implicit
    transaction (the whole point — without commit-then-close, asyncpg
    silently rolls back on connection release) and (b) swallow any
    exceptions because we're past the lifecycle of the original task.
    """

    async def _close():
        try:
            if sa_session.in_transaction():
                try:
                    await sa_session.commit()
                except Exception:
                    LOGGER.debug(
                        "finalizer: commit failed for profile_id=%s; "
                        "rolling back",
                        profile_id,
                        exc_info=True,
                    )
                    try:
                        await sa_session.rollback()
                    except Exception:
                        pass
        finally:
            try:
                await sa_session.close()
            except Exception:
                LOGGER.debug(
                    "finalizer: close failed for profile_id=%s",
                    profile_id,
                    exc_info=True,
                )

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No running loop — interpreter shutdown, finalizer firing from
        # GC on a thread without an event loop, etc. `get_event_loop()`
        # used to be tolerant here but is deprecated on 3.12+ and slated
        # to raise outright; `get_running_loop` makes the "no loop"
        # branch explicit. The asyncpg connection will be reaped by GC
        # (with a warning), but at least we tried.
        return
    if not loop.is_running():
        return
    try:
        loop.create_task(_close(), name=f"kanon_session_finalize:{profile_id}")
    except Exception:
        LOGGER.debug(
            "finalizer: failed to schedule close on loop for profile_id=%s",
            profile_id,
            exc_info=True,
        )

# Sessions that take longer than this to open get a WARNING log instead
# of DEBUG. Operators can grep for "kanon_storage session SLOW" to find
# pool-saturation or contention regressions in production.
SLOW_SESSION_OPEN_MS = 250.0
SLOW_SESSION_LIFETIME_MS = 5000.0
SLOW_COMMIT_MS = 250.0


class KanonStorageProfileSession(ProfileSession):
    """Session over a `KanonStorageProfile`."""

    def __init__(
        self,
        profile,
        *,
        context: Optional[InjectionContext] = None,
        settings=None,
        is_transaction: bool = False,
    ):
        super().__init__(profile, context=context, settings=settings)
        self._is_transaction = is_transaction
        self._sa_session: Optional[AsyncSession] = None
        # Timing instrumentation. _open_perf is set on _setup() entry,
        # _open_done_perf right after the SQLAlchemy session factory
        # returns. Both are used in _teardown to log session lifetime.
        self._open_perf: Optional[float] = None
        self._open_done_perf: Optional[float] = None
        # Finalizer that closes the AsyncSession when *this* ProfileSession
        # is GC'd. Required because ACA-Py's `await profile.session()`
        # pattern (transport/inbound, etc.) sets `_awaited=True` on the
        # parent class, which makes `__aexit__` skip `_teardown`. Without
        # the finalizer the asyncpg connection lives until the *underlying*
        # AsyncSession is GC'd separately — and gets rolled back when it
        # is. The finalizer is detached in `_teardown` (normal path) so it
        # only fires for the await-without-context pattern.
        self._finalizer: Optional[weakref.finalize] = None

    @property
    def is_transaction(self) -> bool:
        return self._is_transaction

    @property
    def sa_session(self) -> AsyncSession:
        if self._sa_session is None:
            raise RuntimeError(
                "KanonStorageProfileSession is not active — use as `async with`"
            )
        return self._sa_session

    async def _setup(self) -> None:
        """Open a SQLAlchemy session (and optionally start a transaction).

        We track every session in `_OPEN_SESSIONS` so a stray cancellation
        that skips `__aexit__` can't leave the connection checked out.
        """
        self._open_perf = time.perf_counter()
        factory = self._profile.session_factory
        sa_session = factory()
        try:
            if self._is_transaction:
                await sa_session.begin()
        except BaseException:
            # `begin()` can fail (pool exhausted, connect timeout). Make
            # sure the half-initialised session releases its connection
            # rather than waiting for GC.
            await asyncio.shield(sa_session.close())
            raise
        self._sa_session = sa_session
        _OPEN_SESSIONS.add(sa_session)
        # Finalizer closes the AsyncSession on the running loop when this
        # instance is GC'd — required for the `await profile.session()`
        # pattern that never invokes `_teardown`.
        self._finalizer = weakref.finalize(
            self,
            _schedule_close,
            sa_session,
            self._profile.profile_id,
        )
        self._open_done_perf = time.perf_counter()
        open_ms = (self._open_done_perf - self._open_perf) * 1000.0
        if open_ms >= SLOW_SESSION_OPEN_MS:
            LOGGER.warning(
                "kanon_storage session SLOW open profile_id=%s txn=%s elapsed_ms=%.1f",
                self._profile.profile_id,
                self._is_transaction,
                open_ms,
            )
        else:
            LOGGER.debug(
                "kanon_storage session open profile_id=%s txn=%s elapsed_ms=%.1f",
                self._profile.profile_id,
                self._is_transaction,
                open_ms,
            )

        # Rebind BaseStorage + BaseWallet on the session-scoped injector
        # so any storage/wallet call inside the session participates in
        # the same SQLAlchemy session/transaction.
        from acapy_agent.storage.base import BaseStorage
        from acapy_agent.wallet.base import BaseWallet

        from kanon_storage.v1_0.storage.storage_service import KanonStorage
        from kanon_storage.v1_0.wallet.wallet import KanonWallet

        storage = KanonStorage(
            profile_id=self._profile.profile_id,
            dialect=self._profile.config.dialect,
            session_factory=self._profile.session_factory,
            active_session=self._sa_session,
        )
        self._context.injector.bind_instance(BaseStorage, storage)
        self._context.injector.bind_instance(BaseWallet, KanonWallet(self))

        # DIDComm v2 wiring — only when the experimental flag is on. Mirrors
        # acapy_agent.askar.profile.AskarProfileSession._setup so the upstream
        # `didcomm_messaging` library uses our keystore for SecretsManager.
        if self._profile.context.settings.get("experiment.didcomm_v2"):
            self._wire_didcomm_v2()

    def _wire_didcomm_v2(self) -> None:
        """Bind the DIDComm v2 chain on this session injector.

        Reuses ACA-Py's CryptoService / PackagingService / RoutingService /
        DMPResolver bindings (the agent's startup binds those globally) and
        only swaps in our SecretsManager.
        """
        from acapy_agent.config.provider import ClassProvider
        from didcomm_messaging import (
            CryptoService,
            DIDCommMessaging,
            PackagingService,
            RoutingService,
            SecretsManager,
        )
        from didcomm_messaging.resolver import DIDResolver as DMPResolver

        from kanon_storage.v1_0.wallet.secrets_adapter import KanonSecretsAdapter

        injector = self._context.injector
        injector.bind_instance(SecretsManager, KanonSecretsAdapter(self))
        injector.bind_provider(
            DIDCommMessaging,
            ClassProvider(
                DIDCommMessaging,
                ClassProvider.Inject(CryptoService),
                ClassProvider.Inject(SecretsManager),
                ClassProvider.Inject(DMPResolver),
                ClassProvider.Inject(PackagingService),
                ClassProvider.Inject(RoutingService),
            ),
        )

    async def _teardown(self, commit: Optional[bool] = None) -> None:
        """Finalise + close the session.

        `asyncio.shield` is used so a cancellation arriving mid-teardown
        doesn't abort `commit()` or `close()`. If we let cancellation
        kill `close()`, the asyncpg connection stays checked out, the GC
        eventually reaps it, the implicit transaction is rolled back,
        and any record.save() in this session disappears.
        """
        if self._sa_session is None:
            return
        sa_session = self._sa_session
        # Null out *first* so a re-entrant teardown (Profile.__aexit__ +
        # explicit commit() can both fire) becomes a no-op.
        self._sa_session = None
        # We're cleaning up cleanly — the finalizer would only double-close.
        if self._finalizer is not None:
            self._finalizer.detach()
            self._finalizer = None
        teardown_start = time.perf_counter()
        try:
            await asyncio.shield(self._safe_finalize(sa_session, commit))
        finally:
            try:
                await asyncio.shield(sa_session.close())
            except BaseException:
                LOGGER.exception(
                    "kanon_storage session close failed profile_id=%s",
                    self._profile.profile_id,
                )
            finally:
                _OPEN_SESSIONS.discard(sa_session)
                close_ms = (time.perf_counter() - teardown_start) * 1000.0
                lifetime_ms = (
                    (time.perf_counter() - self._open_perf) * 1000.0
                    if self._open_perf is not None
                    else -1.0
                )
                log = (
                    LOGGER.warning
                    if lifetime_ms >= SLOW_SESSION_LIFETIME_MS
                    else LOGGER.debug
                )
                log(
                    "kanon_storage session close profile_id=%s txn=%s commit=%s "
                    "close_ms=%.1f lifetime_ms=%.1f",
                    self._profile.profile_id,
                    self._is_transaction,
                    commit,
                    close_ms,
                    lifetime_ms,
                )

    async def _safe_finalize(
        self, sess: AsyncSession, commit: Optional[bool]
    ) -> None:
        """Commit or rollback, defensive against poisoned sessions.

        Semantics:
          * `commit=True`  → explicit commit (transactional or auto)
          * `commit=False` → explicit rollback
          * `commit=None`  → ACA-Py's `__aexit__` default. Treat as
            "implicit success" — commit pending writes. Rolling back
            here was the source of the cred_ex_v20 vanish bug: any
            `async with profile.session():` block that didn't
            explicitly call `txn.commit()` had its records discarded.

        A failed flush in an autobegun transaction leaves the session in a
        state where commit() raises `PendingRollbackError`; we fall back
        to rollback in that case to release the connection cleanly.
        """
        if self._is_transaction:
            if commit is False:
                await sess.rollback()
                return
            if not sess.in_transaction():
                # Caller already committed (e.g. via `await txn.commit()`
                # which routed through ProfileSession.commit -> _teardown).
                return
            commit_start = time.perf_counter()
            try:
                await sess.commit()
            except Exception:
                LOGGER.debug(
                    "kanon_storage transaction commit failed; rolling back",
                    exc_info=True,
                )
                try:
                    await sess.rollback()
                except Exception:
                    LOGGER.debug(
                        "kanon_storage transaction rollback also failed",
                        exc_info=True,
                    )
                raise
            commit_ms = (time.perf_counter() - commit_start) * 1000.0
            log = (
                LOGGER.warning
                if commit_ms >= SLOW_COMMIT_MS
                else LOGGER.debug
            )
            log(
                "kanon_storage commit profile_id=%s elapsed_ms=%.1f",
                self._profile.profile_id,
                commit_ms,
            )
            return

        # Auto-commit (non-transactional) session: SQLAlchemy 2.0 autobegins.
        if commit is False:
            await sess.rollback()
            return
        if not sess.in_transaction():
            return
        try:
            await sess.commit()
        except Exception:
            LOGGER.debug(
                "kanon_storage auto-commit failed; rolling back", exc_info=True
            )
            try:
                await sess.rollback()
            except Exception:
                LOGGER.debug(
                    "kanon_storage auto-commit rollback also failed",
                    exc_info=True,
                )
            # Don't re-raise on auto-commit teardown: the original exception
            # (which poisoned the session) is already propagating.

    def __del__(self):
        # Diagnostic: ProfileSession GC'd without `_teardown`. This is the
        # `await profile.session()` pattern (no `async with`, no explicit
        # `commit()` / `rollback()`). The `weakref.finalize` registered in
        # `_setup` will close the AsyncSession asynchronously on the
        # running loop, so this is informational, not a data-loss signal.
        sa_session = getattr(self, "_sa_session", None)
        if sa_session is not None:
            try:
                LOGGER.debug(
                    "kanon_storage ProfileSession GC'd without _teardown "
                    "profile_id=%s txn=%s — finalizer will close session",
                    getattr(self._profile, "profile_id", "?"),
                    self._is_transaction,
                )
            except Exception:
                pass

    @property
    def handle(self):
        """Return a Kanon shim that exposes the askar/kanon Store handle API.

        Acapy's anoncreds + credx layers call `session.handle.fetch_all(...)`,
        `.fetch(...)`, `.insert(...)` etc. directly — bypassing BaseStorage.
        We expose a `KanonSessionHandle` that translates those calls onto
        our generic_record table via the active SQLAlchemy session, so
        anoncreds operations Just Work without porting indy_credx code.
        """
        if self._sa_session is None:
            return None
        from acapy_agent.wallet.base import BaseWallet

        from kanon_storage.v1_0.profile.handle import KanonSessionHandle

        handle = KanonSessionHandle(
            sa_session=self._sa_session,
            profile_id=self._profile.profile_id,
            dialect=self._profile.config.dialect,
        )
        # Wire the master-key AEAD so handle.fetch_key/insert_key can
        # decrypt/encrypt key secrets. Wallet is always bound by _setup
        # before any caller can reach `.handle` inside `async with`.
        wallet = self._context.inject_or(BaseWallet)
        aead = getattr(getattr(wallet, "_keystore", None), "_aead", None)
        if aead is not None:
            handle.attach_keystore_aead(aead)
        return handle
