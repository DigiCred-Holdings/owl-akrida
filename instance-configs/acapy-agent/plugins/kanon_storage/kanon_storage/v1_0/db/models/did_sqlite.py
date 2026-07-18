"""DID records (SQLite)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import Text, Index, JSON
from sqlalchemy.orm import Mapped, mapped_column

from kanon_storage.v1_0.db.models.base_sqlite import BaseSqliteModel, SqliteRecordMixin


class DidSqlite(BaseSqliteModel, SqliteRecordMixin):
    __tablename__ = "kanon_did"

    method: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    verkey: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    key_type: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    __table_args__ = (
        Index("uq_kanon_did_profile_did", "profile_id", "id", unique=True),
        Index("ix_kanon_did_profile_verkey", "profile_id", "verkey"),
    )
