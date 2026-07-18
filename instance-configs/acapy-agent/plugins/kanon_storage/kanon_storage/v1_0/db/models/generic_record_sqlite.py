"""Generic record table (SQLite). See generic_record_pg.py for the design rationale."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import JSON, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from kanon_storage.v1_0.db.models.base_sqlite import BaseSqliteModel


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class GenericRecordSqlite(BaseSqliteModel):
    __tablename__ = "kanon_generic_record"

    row_pk: Mapped[str] = mapped_column(
        Text, primary_key=True, default=lambda: str(uuid.uuid4())
    )
    id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    profile_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    record_type: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    tags: Mapped[dict[str, str] | None] = mapped_column(JSON, nullable=True)
    custom_tags: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[str] = mapped_column(Text, nullable=False, default=_now_iso)
    updated_at: Mapped[str] = mapped_column(
        Text, nullable=False, default=_now_iso, onupdate=_now_iso
    )

    __table_args__ = (
        UniqueConstraint(
            "profile_id", "record_type", "id", name="uq_kanon_generic_profile_type_id"
        ),
    )
