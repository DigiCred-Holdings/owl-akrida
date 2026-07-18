"""Storage-version row (SQLite)."""

from __future__ import annotations

from sqlalchemy import Text
from sqlalchemy.orm import Mapped, mapped_column

from kanon_storage.v1_0.db.models.base_sqlite import BaseSqliteModel, SqliteRecordMixin


class StorageVersionSqlite(BaseSqliteModel, SqliteRecordMixin):
    __tablename__ = "kanon_storage_version"

    schema_version: Mapped[str] = mapped_column(Text, nullable=False)
    plugin_version: Mapped[str] = mapped_column(Text, nullable=False)
