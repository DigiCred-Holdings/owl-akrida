"""Outbox table (Postgres)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import Index, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from kanon_storage.v1_0.db.models.base_pg import BasePgModel, PgRecordMixin


class OutboxPg(BasePgModel, PgRecordMixin):
    __tablename__ = "kanon_outbox"

    op_type: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, default="pending", index=True
    )
    attempts: Mapped[int] = mapped_column(default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("ix_kanon_outbox_pending", "profile_id", "status", "created_at"),
    )
