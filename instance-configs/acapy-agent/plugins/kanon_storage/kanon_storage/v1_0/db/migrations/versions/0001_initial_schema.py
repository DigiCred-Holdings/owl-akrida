"""initial schema.

Revision ID: 0001
Revises:
Create Date: 2026-05-02
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name
    if dialect == "postgresql":
        from kanon_storage.v1_0.db.models import (  # noqa: F401
            base_pg,
            did_pg,
            generic_record_pg,
            key_pg,
            outbox_pg,
            storage_version_pg,
        )

        base_pg.BasePgModel.metadata.create_all(bind)
    else:
        from kanon_storage.v1_0.db.models import (  # noqa: F401
            base_sqlite,
            did_sqlite,
            generic_record_sqlite,
            key_sqlite,
            outbox_sqlite,
            storage_version_sqlite,
        )

        base_sqlite.BaseSqliteModel.metadata.create_all(bind)


def downgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name
    if dialect == "postgresql":
        from kanon_storage.v1_0.db.models import base_pg, did_pg, generic_record_pg, key_pg, outbox_pg, storage_version_pg  # noqa: F401

        base_pg.BasePgModel.metadata.drop_all(bind)
    else:
        from kanon_storage.v1_0.db.models import base_sqlite, did_sqlite, generic_record_sqlite, key_sqlite, outbox_sqlite, storage_version_sqlite  # noqa: F401

        base_sqlite.BaseSqliteModel.metadata.drop_all(bind)
