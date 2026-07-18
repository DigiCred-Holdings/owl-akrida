"""Encrypted key rows (Postgres)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import Index, LargeBinary, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from kanon_storage.v1_0.db.models.base_pg import BasePgModel, PgRecordMixin


class KeyPg(BasePgModel, PgRecordMixin):
    __tablename__ = "kanon_key"

    # `id` (from mixin) is the verkey — base58 public key — used as the
    # natural lookup key everywhere ACA-Py refers to a signing key.
    key_alg: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    secret_ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    nonce: Mapped[bytes] = mapped_column(LargeBinary(12), nullable=False)
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    # kids is a JSONB list[str]; matches askar/kanon's `tags["kid"]` shape.
    kid: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    multikey: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)

    __table_args__ = (
        Index("uq_kanon_key_profile_verkey", "profile_id", "id", unique=True),
        # GIN over the JSONB list of kids — drop profile_id from the index
        # since GIN can't span text + jsonb. Tenant filtering still happens
        # in the WHERE clause; the GIN narrows on kid containment first.
        Index("ix_kanon_key_kid_gin", "kid", postgresql_using="gin"),
        Index("ix_kanon_key_profile_multikey", "profile_id", "multikey"),
    )
