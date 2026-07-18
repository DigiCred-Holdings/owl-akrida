"""SQLite declarative base + shared mixin columns."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import Index, JSON, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class BaseSqliteModel(DeclarativeBase):
    """SQLite declarative base. Concrete tables extend and add columns."""

    pass


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class SqliteRecordMixin:
    """Shared columns for any record row, SQLite flavor."""

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    profile_id: Mapped[str] = mapped_column(Text, index=True, nullable=False)
    custom_tags: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[str] = mapped_column(Text, nullable=False, default=_now_iso)
    updated_at: Mapped[str] = mapped_column(
        Text, nullable=False, default=_now_iso, onupdate=_now_iso
    )

    @classmethod
    def __declare_last__(cls) -> None:  # noqa: D401
        """Hook for subclass-specific finalization."""
        pass


def composite_profile_index(table_name: str) -> Index:
    return Index(f"ix_{table_name}_profile_id_id", "profile_id", "id")
