"""WQL (Wallet Query Language) -> SQLAlchemy ColumnElement."""

from __future__ import annotations

import logging
import re
from typing import Any

from sqlalchemy import (
    ColumnElement,
    and_,
    not_,
    or_,
)

LOGGER = logging.getLogger(__name__)

# Tag/key names must be plain SQL-ish identifiers. This rules out
# attribute escapes (e.g. dunder names that hit SQLAlchemy internals via
# `getattr(table, ...)`) AND characters that would break the JSONPath
# string we interpolate into `json_extract(...)` ($, [, ", ', ., space).
#
# `:` is allowed because askar stores anoncreds attribute tags under the
# `attr::<name>::value` namespace (see `anoncreds/holder.py:238`), which
# is then filtered with `$exist` on every proof flow. `:` doesn't break
# JSONPath grammar — it's a plain literal in both SQLite `json_extract`
# and Postgres `->>` — so admitting it keeps the upstream-parity tag
# scheme working without weakening the injection guard.
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_:]*$")


def _validate_identifier(name: str, *, kind: str) -> None:
    """Reject any identifier that doesn't match `[A-Za-z_][A-Za-z0-9_:]*`.

    Used to guard tag names, JSON keys, and dotted JSONPath segments
    before they are interpolated into raw `json_extract` strings.
    """
    if not isinstance(name, str) or not _IDENT_RE.match(name):
        raise ValueError(
            f"Invalid {kind}: {name!r}. Must match [A-Za-z_][A-Za-z0-9_:]*."
        )


# Operators that are sensible to coerce numerically when the operand parses
# as a number. ACA-Py's WQL spec treats tag values as strings, but the
# encoders cast to numeric for ordered comparisons against numeric tags.
_NUMERIC_OPS = {"$gt", "$gte", "$lt", "$lte"}


def _maybe_numeric(val: Any) -> Any:
    """Coerce a string operand into int/float when parseable.

    Mirrors the cast performed in ACA-Py's WQL SQL encoders so a string
    tag with a numeric value compares numerically rather than
    lexicographically.
    """
    if isinstance(val, bool):
        # bool is a subclass of int — keep as-is to avoid surprising the caller.
        return val
    if isinstance(val, (int, float)):
        return val
    if isinstance(val, str):
        try:
            return int(val)
        except (TypeError, ValueError):
            pass
        try:
            return float(val)
        except (TypeError, ValueError):
            pass
    return val


COMPARISON_OPS = {
    "$eq": lambda col, v: col == v,
    "$neq": lambda col, v: col != v,
    "$gt": lambda col, v: col > v,
    "$gte": lambda col, v: col >= v,
    "$lt": lambda col, v: col < v,
    "$lte": lambda col, v: col <= v,
    "$like": lambda col, v: col.like(v, escape="\\"),
    "$in": lambda col, v: col.in_(v),
}


class WqlToSqlAlchemy:
    """Translate a WQL JSON dict into a SQLAlchemy ColumnElement.

    Args:
        table: SQLAlchemy declarative table class (Pg or SQLite variant).
        dialect: 'postgresql' or 'sqlite'.
        tag_column: Name of the JSON column holding string-keyed tags.
            Default 'tags' (for GenericRecord). Set to None if no tag JSON
            column exists on this table.
        custom_tags_column: Optional JSON column for overflow tags on
            typed tables (Drizzle's customTags). Default 'custom_tags'.
        tag_key_mapping: Optional map of tag name -> JSON path inside the
            value column.
    """

    def __init__(
        self,
        *,
        table: type,
        dialect: str,
        tag_column: str | None = "tags",
        custom_tags_column: str | None = "custom_tags",
        value_column: str = "value",
        tag_key_mapping: dict[str, str] | None = None,
    ):
        self.table = table
        self.dialect = dialect
        self.tag_column = tag_column
        self.custom_tags_column = custom_tags_column
        self.value_column = value_column
        self.tag_key_mapping = tag_key_mapping or {}

    def __call__(self, q: Any) -> ColumnElement | None:
        if q is None:
            return None
        if isinstance(q, list):
            sub = [self._compile(item) for item in q if isinstance(item, dict) and item]
            sub = [s for s in sub if s is not None]
            if not sub:
                return None
            if len(sub) == 1:
                return sub[0]
            return and_(*sub)
        if not q:
            return None
        return self._compile(q)

    def _compile(self, q: dict[str, Any]) -> ColumnElement | None:
        clauses: list[ColumnElement] = []

        for key, val in q.items():
            if key == "$or":
                if not isinstance(val, list):
                    raise ValueError(f"$or expects a list, got {type(val).__name__}")
                sub = [self._compile(item) for item in val if item]
                sub = [s for s in sub if s is not None]
                if sub:
                    clauses.append(or_(*sub))
            elif key == "$and":
                if not isinstance(val, list):
                    raise ValueError(f"$and expects a list, got {type(val).__name__}")
                sub = [self._compile(item) for item in val if item]
                sub = [s for s in sub if s is not None]
                if sub:
                    clauses.append(and_(*sub))
            elif key == "$not":
                if not isinstance(val, dict):
                    raise ValueError(f"$not expects a dict, got {type(val).__name__}")
                inner = self._compile(val)
                if inner is not None:
                    clauses.append(not_(inner))
            elif key == "$exist" or key == "$exists":
                names = val if isinstance(val, list) else [val]
                for name in names:
                    if not isinstance(name, str):
                        raise ValueError(
                            f"{key} expects a string or list of strings, "
                            f"got {type(name).__name__}"
                        )
                    clauses.append(self._exists_clause(name))
            elif key.startswith("$"):
                raise ValueError(f"Unsupported top-level operator: {key!r}")
            else:
                clauses.append(self._tag_clause(key, val))

        if not clauses:
            return None
        if len(clauses) == 1:
            return clauses[0]
        return and_(*clauses)

    def _tag_clause(self, tag: str, val: Any) -> ColumnElement:
        col = self._resolve_tag_column(tag)

        # List-valued tag filter is an implicit IN, matching askar semantics.
        if isinstance(val, (list, tuple, set)):
            values = [str(v) if not isinstance(v, (int, float, bool)) else v for v in val]
            return col.in_(values)

        if not isinstance(val, dict):
            return col == _coerce_for_compare(val)

        sub_clauses: list[ColumnElement] = []
        for op, op_val in val.items():
            if op == "$exist" or op == "$exists":
                want = bool(op_val)
                clause = self._exists_clause(tag)
                sub_clauses.append(clause if want else not_(clause))
            elif op in COMPARISON_OPS:
                operand = op_val
                if op in _NUMERIC_OPS:
                    operand = _maybe_numeric(operand)
                else:
                    operand = _coerce_for_compare(operand)
                sub_clauses.append(COMPARISON_OPS[op](col, operand))
            else:
                raise ValueError(f"Unsupported tag operator: {op!r} for tag {tag!r}")
        if len(sub_clauses) == 1:
            return sub_clauses[0]
        return and_(*sub_clauses)

    def _resolve_tag_column(self, tag: str):
        # tag_key_mapping is operator-controlled (set in adapter classes,
        # not by inbound requests), but the *path string* it produces is
        # still interpolated raw into JSONPath. Sanitize there.
        if tag in self.tag_key_mapping:
            return self._value_path(self.tag_key_mapping[tag])

        # `tag` is caller-supplied (WQL dict from request body), so reject
        # anything that isn't a plain identifier before reflecting on the
        # ORM table or interpolating into JSONPath. This blocks
        # `getattr(self.table, "__mapper__")`-style attribute escapes and
        # JSONPath-grammar breakers like `a"; drop` / `a.b` in one shot.
        _validate_identifier(tag, kind="tag name")

        # Only resolve to a real mapped column on the table — never to an
        # arbitrary class attribute (which `hasattr` accepted before and
        # opened up `__class__`/`metadata`/`__mapper__` attribute reads).
        mapped_columns = self.table.__table__.c
        if tag in mapped_columns:
            return mapped_columns[tag]

        if self.tag_column and self.tag_column in self.table.__table__.c:
            return self._json_extract(self.tag_column, tag)

        if (
            self.custom_tags_column
            and self.custom_tags_column in self.table.__table__.c
        ):
            return self._json_extract(self.custom_tags_column, tag)

        raise ValueError(
            f"Cannot resolve tag {tag!r}: no typed column, no tag JSON, "
            f"no custom_tags JSON on {self.table.__tablename__}"
        )

    def _exists_clause(self, tag: str) -> ColumnElement:
        """Build a `tag IS NOT NULL`-style clause for the given tag.

        Equivalent to ACA-Py's `$exist` (and the Mongo `$exists: true`).
        Resolves the tag the same way as comparison clauses, then asserts
        the resolved column is not null.
        """
        col = self._resolve_tag_column(tag)
        return col.isnot(None)

    def _json_extract(self, column_name: str, key: str) -> ColumnElement:
        """`column->>'key'` (Postgres) or `json_extract(column, '$.key')` (SQLite)."""
        # `key` must already be validated by `_resolve_tag_column`. Belt-
        # and-suspenders: callers internal to this module must not bypass.
        _validate_identifier(key, kind="JSON key")
        col = self.table.__table__.c[column_name]
        if self.dialect == "postgresql":
            return col[key].astext
        # SQLite: use func.json_extract for portability across SQLAlchemy versions.
        from sqlalchemy import func

        return func.json_extract(col, f"$.{key}")

    def _value_path(self, path: str) -> ColumnElement:
        """Reach into the value column by dotted path (e.g. 'a.b.c')."""
        parts = path.split(".")
        # Each dotted segment is interpolated into the JSONPath string,
        # so every segment must be a safe identifier — no quotes, no
        # spaces, no `$`/`[`.
        for part in parts:
            _validate_identifier(part, kind="JSON path segment")
        col = self.table.__table__.c[self.value_column]
        if self.dialect == "postgresql":
            ret = col
            for p in parts[:-1]:
                ret = ret[p]
            return ret[parts[-1]].astext
        from sqlalchemy import func

        return func.json_extract(col, f"$.{'.'.join(parts)}")


def _coerce_for_compare(val: Any) -> Any:
    """Tag values are typically strings in WQL. Pass through types we accept."""
    if isinstance(val, (list, tuple, set)):
        return [str(v) if not isinstance(v, (int, float, bool)) else v for v in val]
    return val
