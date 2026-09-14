"""Immutable contract joining validated candidates to one write operation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

from minidb.core.records import (
    Row,
    RowId,
    RowUpdate,
    StoredRow,
    ValidatedWriteToken,
    Value,
    WriteKind,
    _validate_row,
)
from minidb.core.schema import Schema, TableDef
from minidb.core.source import SourceSpan

if TYPE_CHECKING:
    # These names are owned by the catalog/schema contributor. Keeping them as
    # forward references avoids creating competing production definitions.
    from minidb.core.schema import IndexDef, PendingIndexDef


@dataclass(frozen=True, slots=True)
class PreparedWrite:
    """Fully prepared, immutable input for one ACTIVE write application.

    Handoff note: this is the single WIP contract definition. IndexDef and
    PendingIndexDef are intentionally forward references until Catalog's
    owner lands the formal classes. Do not replace them with a second local
    production type; update the shared imports and validation together.
    """

    prepared_id: UUID
    session_id: UUID
    catalog_generation: int
    kind: WriteKind
    span: SourceSpan
    table: TableDef | None
    create_name: str | None
    create_schema: Schema | None
    create_indexes: tuple[PendingIndexDef, ...]
    create_index_entries: tuple[tuple[Value, RowId], ...]
    insert_row: Row | None
    updates: tuple[RowUpdate, ...]
    deletes: tuple[StoredRow, ...]
    affected_indexes: tuple[IndexDef, ...]
    encoded_candidate_bytes: int
    validation_token: ValidatedWriteToken

    def __post_init__(self) -> None:
        validate_prepared(self)


def validate_prepared(prepared: PreparedWrite) -> None:
    """Validate field types, token binding, and WriteKind field exclusivity.

    This validates the common shape and physical RowId targets only. It does
    not perform constraint, catalog, index-key, transaction-state, or page
    writes; those remain the responsibility of the future prepare/apply
    integration owners.
    """
    if not isinstance(prepared, PreparedWrite):
        raise TypeError("prepared must be a PreparedWrite")
    if not isinstance(prepared.prepared_id, UUID):
        raise TypeError("prepared_id must be a UUID")
    if not isinstance(prepared.session_id, UUID):
        raise TypeError("session_id must be a UUID")
    if type(prepared.catalog_generation) is not int:
        raise TypeError("catalog_generation must be an int")
    if prepared.catalog_generation < 0:
        raise ValueError("catalog_generation must be non-negative")
    if not isinstance(prepared.kind, WriteKind):
        raise TypeError("kind must be a WriteKind")
    if not isinstance(prepared.span, SourceSpan):
        raise TypeError("span must be a SourceSpan")
    if prepared.table is not None and not isinstance(prepared.table, TableDef):
        raise TypeError("table must be a TableDef or None")
    if prepared.create_name is not None and type(prepared.create_name) is not str:
        raise TypeError("create_name must be a str or None")
    if prepared.create_name == "":
        raise ValueError("create_name must not be empty")
    if prepared.create_schema is not None and not isinstance(
        prepared.create_schema, Schema
    ):
        raise TypeError("create_schema must be a Schema or None")
    for name in (
        "create_indexes",
        "create_index_entries",
        "updates",
        "deletes",
        "affected_indexes",
    ):
        if type(getattr(prepared, name)) is not tuple:
            raise TypeError(f"{name} must be a tuple")
    if any(not isinstance(item, RowUpdate) for item in prepared.updates):
        raise TypeError("updates must contain only RowUpdate values")
    if any(not isinstance(item, StoredRow) for item in prepared.deletes):
        raise TypeError("deletes must contain only StoredRow values")
    update_ids = tuple(item.row_id for item in prepared.updates)
    if len(set(update_ids)) != len(update_ids):
        raise ValueError("updates must not contain duplicate row_id values")
    delete_ids = tuple(item.row_id for item in prepared.deletes)
    if len(set(delete_ids)) != len(delete_ids):
        raise ValueError("deletes must not contain duplicate row_id values")
    _validate_index_entries(prepared.create_index_entries)
    if prepared.insert_row is not None:
        _validate_row(prepared.insert_row, field_name="insert_row")
    if type(prepared.encoded_candidate_bytes) is not int:
        raise TypeError("encoded_candidate_bytes must be an int")
    if prepared.encoded_candidate_bytes < 0:
        raise ValueError("encoded_candidate_bytes must be non-negative")
    if not isinstance(prepared.validation_token, ValidatedWriteToken):
        raise TypeError("validation_token must be a ValidatedWriteToken")
    token = prepared.validation_token
    if (
        token.prepared_id != prepared.prepared_id
        or token.session_id != prepared.session_id
        or token.catalog_generation != prepared.catalog_generation
    ):
        raise ValueError("validation_token must match the prepared write identity")

    _validate_kind_fields(prepared)


def _validate_index_entries(entries: tuple[tuple[Value, RowId], ...]) -> None:
    for index, entry in enumerate(entries):
        if type(entry) is not tuple or len(entry) != 2:
            raise TypeError(f"create_index_entries[{index}] must be a (Value, RowId) tuple")
        _validate_row((entry[0],), field_name=f"create_index_entries[{index}].key")
        if not isinstance(entry[1], RowId):
            raise TypeError(f"create_index_entries[{index}][1] must be a RowId")


def _validate_kind_fields(prepared: PreparedWrite) -> None:
    # Actual IndexDef/PendingIndexDef class checks stay deferred until the
    # shared catalog definitions land. The object-shape test here prevents
    # silently creating another competing catalog type in this module.
    has_table = prepared.table is not None
    has_create = prepared.create_name is not None or prepared.create_schema is not None
    has_insert = prepared.insert_row is not None
    has_updates = bool(prepared.updates)
    has_deletes = bool(prepared.deletes)

    if prepared.kind is WriteKind.CREATE_TABLE:
        valid = (
            prepared.create_name is not None
            and prepared.create_schema is not None
            and not has_table
            and not prepared.create_index_entries
            and not has_insert
            and not has_updates
            and not has_deletes
            and not prepared.affected_indexes
        )
    elif prepared.kind is WriteKind.CREATE_INDEX:
        valid = (
            has_table
            and len(prepared.create_indexes) == 1
            and not has_create
            and not has_insert
            and not has_updates
            and not has_deletes
            and not prepared.affected_indexes
        )
    elif prepared.kind is WriteKind.INSERT:
        valid = (
            has_table
            and has_insert
            and not has_create
            and not prepared.create_indexes
            and not prepared.create_index_entries
            and not has_updates
            and not has_deletes
        )
    elif prepared.kind is WriteKind.UPDATE:
        valid = (
            has_table
            and not has_create
            and not prepared.create_indexes
            and not prepared.create_index_entries
            and not has_insert
            and not has_deletes
        )
    else:
        valid = (
            has_table
            and not has_create
            and not prepared.create_indexes
            and not prepared.create_index_entries
            and not has_insert
            and not has_updates
        )
    if not valid:
        raise ValueError(f"fields are inconsistent with WriteKind.{prepared.kind.name}")


__all__ = ["PreparedWrite", "validate_prepared"]
