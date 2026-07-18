"""Postgres declarative base + shared mixin columns."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import Index, TIMESTAMP, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class BasePgModel(DeclarativeBase):
    """Postgres declarative base. Concrete tables extend and add columns."""

    pass


class PgRecordMixin:
    """Shared columns for any record row.

    Strings are unbounded ``Text`` because workflow templates, did:peer:4
    identifiers, and DIDComm thread IDs can exceed any reasonable varchar limit.
    """

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    profile_id: Mapped[str] = mapped_column(Text, index=True, nullable=False)
    custom_tags: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    @classmethod
    def __declare_last__(cls) -> None:  # noqa: D401 - SQLAlchemy hook
        """Add a composite index on (profile_id, id) once the class is built."""
        pass


def composite_profile_index(table_name: str) -> Index:
    """Helper: index on (profile_id, id) for tenant-scoped lookups."""
    return Index(f"ix_{table_name}_profile_id_id", "profile_id", "id")
