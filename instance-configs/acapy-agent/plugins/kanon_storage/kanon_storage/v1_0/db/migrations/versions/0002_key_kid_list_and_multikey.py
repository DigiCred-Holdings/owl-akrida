"""kanon_key: kid -> JSON list, add multikey column.

Revision ID: 0002
Revises: 0001
Create Date: 2026-05-02
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    if dialect == "postgresql":
        # Idempotency: if the column is already JSONB (i.e. a fresh schema
        # created by `metadata.create_all` and only now being stamped),
        # skip the ALTER COLUMN. Likewise check the multikey column and
        # indexes via information_schema so re-running this migration is
        # a no-op.
        kid_type = bind.execute(
            sa.text(
                "SELECT data_type FROM information_schema.columns "
                "WHERE table_name = 'kanon_key' AND column_name = 'kid'"
            )
        ).scalar()
        if kid_type and kid_type.lower() != "jsonb":
            op.execute(
                "ALTER TABLE kanon_key "
                "ALTER COLUMN kid TYPE jsonb "
                "USING CASE WHEN kid IS NULL THEN NULL "
                "ELSE jsonb_build_array(kid) END"
            )
        multikey_exists = bind.execute(
            sa.text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name = 'kanon_key' AND column_name = 'multikey'"
            )
        ).scalar()
        if not multikey_exists:
            op.add_column(
                "kanon_key",
                sa.Column("multikey", sa.Text(), nullable=True),
            )
        # Drop legacy btree on the old scalar kid column if it exists.
        op.execute("DROP INDEX IF EXISTS ix_kanon_key_kid")
        op.execute(
            "CREATE INDEX IF NOT EXISTS ix_kanon_key_kid_gin "
            "ON kanon_key USING gin (kid)"
        )
        op.execute(
            "CREATE INDEX IF NOT EXISTS ix_kanon_key_profile_multikey "
            "ON kanon_key (profile_id, multikey)"
        )
    else:
        # SQLite: JSON column is TEXT under the hood. Wrap existing scalars
        # as single-element arrays so the new list-shape parser works.
        op.execute(
            "UPDATE kanon_key "
            "SET kid = json_array(kid) "
            "WHERE kid IS NOT NULL "
            "AND substr(kid, 1, 1) != '['"
        )
        # Check whether multikey already exists (e.g. created by
        # `metadata.create_all`). SQLite's PRAGMA table_info works fine.
        cols = [
            r[1]
            for r in bind.execute(sa.text("PRAGMA table_info(kanon_key)")).fetchall()
        ]
        if "multikey" not in cols:
            with op.batch_alter_table("kanon_key") as batch:
                batch.add_column(sa.Column("multikey", sa.Text(), nullable=True))
        op.execute(
            "CREATE INDEX IF NOT EXISTS ix_kanon_key_profile_multikey "
            "ON kanon_key (profile_id, multikey)"
        )


def downgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    if dialect == "postgresql":
        op.execute("DROP INDEX IF EXISTS ix_kanon_key_profile_multikey")
        op.execute("DROP INDEX IF EXISTS ix_kanon_key_kid_gin")
        op.drop_column("kanon_key", "multikey")
        # Best-effort: take element 0 of the JSON array back to a scalar.
        op.execute(
            "ALTER TABLE kanon_key "
            "ALTER COLUMN kid TYPE text "
            "USING CASE WHEN kid IS NULL THEN NULL "
            "ELSE (kid->>0) END"
        )
        op.create_index("ix_kanon_key_kid", "kanon_key", ["kid"])
    else:
        op.execute("DROP INDEX IF EXISTS ix_kanon_key_profile_multikey")
        with op.batch_alter_table("kanon_key") as batch:
            batch.drop_column("multikey")
        op.execute(
            "UPDATE kanon_key "
            "SET kid = json_extract(kid, '$[0]') "
            "WHERE kid IS NOT NULL"
        )
