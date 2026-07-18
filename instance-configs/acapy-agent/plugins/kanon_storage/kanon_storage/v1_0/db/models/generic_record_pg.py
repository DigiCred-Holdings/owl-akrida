"""Generic record table (Postgres).

Natural key is ``(profile_id, record_type, id)`` because the same ``id``
can exist across different record types.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import Text, Index, TIMESTAMP, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from kanon_storage.v1_0.db.models.base_pg import BasePgModel


class GenericRecordPg(BasePgModel):
    __tablename__ = "kanon_generic_record"

    row_pk: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    profile_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    record_type: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    tags: Mapped[dict[str, str] | None] = mapped_column(JSONB, nullable=True)
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

    __table_args__ = (
        UniqueConstraint(
            "profile_id", "record_type", "id", name="uq_kanon_generic_profile_type_id"
        ),
        Index("ix_kanon_generic_tags_gin", "tags", postgresql_using="gin"),
    )
