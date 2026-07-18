"""Storage error mapping."""

from __future__ import annotations

import logging
import sqlite3
from typing import Optional

from acapy_agent.storage.error import (
    StorageDuplicateError,
    StorageError,
    StorageNotFoundError,
)
from sqlalchemy.exc import IntegrityError, NoResultFound

LOGGER = logging.getLogger(__name__)

# Re-export under the kanon naming for callers that prefer
# `RecordDuplicateError` over `StorageDuplicateError`. `StorageError`
# itself is already imported above; the previous `StorageError =
# StorageError` self-assignment was a leftover no-op.
RecordDuplicateError = StorageDuplicateError

__all__ = [
    "RecordDuplicateError",
    "RecordNotFoundError",
    "StorageError",
    "StorageDuplicateError",
    "StorageNotFoundError",
    "translate_db_error",
]


class RecordNotFoundError(StorageNotFoundError):
    """Record not found in storage."""

    def __init__(self, *, record_type: str, record_id: Optional[str] = None):
        msg = (
            f"{record_type} record not found"
            if record_id is None
            else f"{record_type} record id={record_id!r} not found"
        )
        super().__init__(msg)
        self.record_type = record_type
        self.record_id = record_id


PG_UNIQUE_VIOLATION = "23505"
PG_FOREIGN_KEY_VIOLATION = "23503"
PG_NOT_NULL_VIOLATION = "23502"
PG_CHECK_VIOLATION = "23514"
SQLITE_CONSTRAINT_PRIMARY = "PRIMARY KEY"
SQLITE_CONSTRAINT_UNIQUE = "UNIQUE"
SQLITE_CONSTRAINT_FOREIGN = "FOREIGN KEY"
SQLITE_CONSTRAINT_CHECK = "CHECK"
SQLITE_CONSTRAINT_NOT_NULL = "NOT NULL"


def translate_db_error(
    err: Exception, *, record_type: str, record_id: Optional[str] = None
) -> Exception:
    """Map a low-level DB exception into an ACA-Py storage domain error.

    Returns the new exception; caller `raise`s it (use `raise translate_db_error(...)`).
    The original is attached via `__cause__`.

    Recognized SQLSTATEs:
      * 23505 unique_violation       -> StorageDuplicateError
      * 23503 foreign_key_violation  -> StorageError ("references missing parent")
      * 23502 not_null_violation     -> StorageError ("missing required field")
      * 23514 check_violation        -> StorageError ("violates check constraint")
    SQLite IntegrityError messages map to the same domain errors by
    matching constraint keywords (UNIQUE / PRIMARY KEY / FOREIGN KEY /
    CHECK / NOT NULL) — sqlite3 doesn't expose SQLSTATE.
    """
    if isinstance(err, NoResultFound):
        return RecordNotFoundError(record_type=record_type, record_id=record_id)

    if isinstance(err, IntegrityError):
        orig = getattr(err, "orig", None)

        # asyncpg / psycopg
        sqlstate = getattr(orig, "sqlstate", None)
        if sqlstate is None and orig is not None:
            sqlstate = getattr(orig, "pgcode", None)
        if sqlstate == PG_UNIQUE_VIOLATION:
            return _wrap(
                err,
                RecordDuplicateError(
                    f"{record_type} record id={record_id!r} already exists"
                ),
            )
        if sqlstate == PG_FOREIGN_KEY_VIOLATION:
            return _wrap(
                err,
                StorageError(
                    f"{record_type} record id={record_id!r} foreign key violation: "
                    f"references a missing parent row ({_short(orig)})"
                ),
            )
        if sqlstate == PG_NOT_NULL_VIOLATION:
            return _wrap(
                err,
                StorageError(
                    f"{record_type} record id={record_id!r} missing required field "
                    f"({_short(orig)})"
                ),
            )
        if sqlstate == PG_CHECK_VIOLATION:
            return _wrap(
                err,
                StorageError(
                    f"{record_type} record id={record_id!r} violates check "
                    f"constraint ({_short(orig)})"
                ),
            )

        # aiosqlite -> sqlite3
        cause = err.__cause__
        if isinstance(cause, sqlite3.IntegrityError):
            msg = str(cause)
            msg_upper = msg.upper()
            if SQLITE_CONSTRAINT_PRIMARY in msg_upper or SQLITE_CONSTRAINT_UNIQUE in msg_upper:
                return _wrap(
                    err,
                    RecordDuplicateError(
                        f"{record_type} record id={record_id!r} already exists"
                    ),
                )
            if SQLITE_CONSTRAINT_FOREIGN in msg_upper:
                return _wrap(
                    err,
                    StorageError(
                        f"{record_type} record id={record_id!r} foreign key "
                        f"violation: references a missing parent row ({msg})"
                    ),
                )
            if SQLITE_CONSTRAINT_NOT_NULL in msg_upper:
                return _wrap(
                    err,
                    StorageError(
                        f"{record_type} record id={record_id!r} missing required "
                        f"field ({msg})"
                    ),
                )
            if SQLITE_CONSTRAINT_CHECK in msg_upper:
                return _wrap(
                    err,
                    StorageError(
                        f"{record_type} record id={record_id!r} violates check "
                        f"constraint ({msg})"
                    ),
                )

    # Generic fall-through — caller still wants a StorageError, not a SQLAlchemy one.
    return _wrap(err, StorageError(f"{record_type} storage error: {err}"))


def _wrap(orig: Exception, new: Exception) -> Exception:
    """Attach `orig` as the __cause__ of `new` and return `new`."""
    new.__cause__ = orig
    return new


def _short(orig) -> str:
    """Return a user-safe one-line tag from a DB driver error.

    Surface only the constraint name (or top-level class of error) to
    callers — the asyncpg/psycopg error message includes schema, table,
    and column names which is internal information that doesn't belong
    in HTTP responses. The full original is logged at DEBUG and is also
    available via `__cause__` for in-process diagnostics.
    """
    if orig is None:
        return ""
    full = str(orig)
    LOGGER.debug("kanon_storage db error: %s", full)
    if not full:
        return ""
    # asyncpg errors expose a `constraint_name`/`schema_name`/etc set of
    # attributes — prefer the constraint name when present.
    constraint = getattr(orig, "constraint_name", None)
    if constraint:
        return f"constraint={constraint}"
    # Otherwise return only the first line up to the first colon, so
    # `relation "kanon_x" violates check constraint "...": (CONTEXT...)`
    # collapses to its top-level class.
    first_line = full.splitlines()[0]
    return first_line.split(":", 1)[0]
