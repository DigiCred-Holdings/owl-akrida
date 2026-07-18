"""GenericRecordAdapter — handles any StorageRecord without a typed adapter."""

from __future__ import annotations

import json
from typing import Any

from acapy_agent.storage.record import StorageRecord

from kanon_storage.v1_0.db.models.generic_record_pg import GenericRecordPg
from kanon_storage.v1_0.db.models.generic_record_sqlite import GenericRecordSqlite
from kanon_storage.v1_0.storage.base_adapter import BaseRecordAdapter


class GenericRecordAdapter(BaseRecordAdapter):
    """Catch-all adapter — handles any record_type."""

    record_types = ("*",)
    table_pg = GenericRecordPg
    table_sqlite = GenericRecordSqlite

    def get_values(self, record: StorageRecord) -> dict[str, Any]:
        return {
            "id": record.id,
            "profile_id": self.profile_id,
            "record_type": record.type,
            "value": _to_json_obj(record.value),
            "tags": dict(record.tags) if record.tags else None,
        }

    def to_record(self, row: Any) -> StorageRecord:
        value = row.value
        # dict/list need re-encoding so downstream json.loads works;
        # strings stay as-is — the startup wallet-type check reads .value
        # directly without decoding.
        if isinstance(value, (dict, list)):
            value_str = json.dumps(value, separators=(",", ":"))
        elif value is None:
            value_str = ""
        else:
            value_str = str(value)
        return StorageRecord(
            type=row.record_type,
            value=value_str,
            tags=dict(row.tags) if row.tags else {},
            id=row.id,
        )


def _to_json_obj(value):
    # Strings are NOT pre-parsed: some ACA-Py callers read .value back
    # without json.loads, so write/read must round-trip exactly.
    if value is None:
        return None
    if isinstance(value, (dict, list, int, float, bool, str)):
        return value
    return value
