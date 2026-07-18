"""Encrypted key rows (SQLite)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import Text, Index, JSON, LargeBinary
from sqlalchemy.orm import Mapped, mapped_column

from kanon_storage.v1_0.db.models.base_sqlite import BaseSqliteModel, SqliteRecordMixin


class KeySqlite(BaseSqliteModel, SqliteRecordMixin):
    __tablename__ = "kanon_key"

    key_alg: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    secret_ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    nonce: Mapped[bytes] = mapped_column(LargeBinary(12), nullable=False)
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    # kids is a JSON list[str]; SQLite has no JSON-array index so we still
    # filter rows in the keystore via in-Python list membership.
    kid: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    multikey: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)

    __table_args__ = (
        Index("uq_kanon_key_profile_verkey", "profile_id", "id", unique=True),
        Index("ix_kanon_key_profile_multikey", "profile_id", "multikey"),
    )
