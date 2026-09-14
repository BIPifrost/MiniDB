"""Shared immutable row and record-location types.

This module is the single source of truth for values exchanged between the
compiler, row codec, storage engine, catalog and executor.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import Enum
from typing import Protocol, TypeAlias, runtime_checkable
from uuid import UUID


from minidb.core.disk_types import CATALOG_ROOT_PAGE_ID, MAX_PAGE_ID


Value: TypeAlias = int | str | bool | date | Decimal | None
Row: TypeAlias = tuple[Value, ...]


def _validate_row(values: object, *, field_name: str) -> None:
    if not isinstance(values, tuple):
        raise TypeError(f"{field_name} must be a tuple")
    for value in values:
        if value is None or type(value) in (int, str, bool, date, Decimal):
            continue
        raise TypeError(
            f"{field_name} values must be int, str, bool, date, Decimal, or None"
        )


@dataclass(frozen=True, slots=True)
class RowSlot:
    """Slot identity returned by a data-page insertion."""

    slot_id: int
    generation: int

    def __post_init__(self) -> None:
        if isinstance(self.slot_id, bool) or not isinstance(self.slot_id, int):
            raise TypeError("slot_id must be an int")
        if self.slot_id < 0:
            raise ValueError("slot_id must be non-negative")
        if isinstance(self.generation, bool) or not isinstance(self.generation, int):
            raise TypeError("generation must be an int")
        if not 0 <= self.generation <= 0xFFFFFF:
            raise ValueError("generation must be between 0 and 0xFFFFFF")


@dataclass(frozen=True, slots=True)
class RowId:
    """Location of a row for the duration of the current storage operation."""

    page_id: int
    slot_id: int
    generation: int = 0

    def __post_init__(self) -> None:
        if isinstance(self.page_id, bool) or not isinstance(self.page_id, int):
            raise TypeError("page_id must be an int")
        if not CATALOG_ROOT_PAGE_ID <= self.page_id <= MAX_PAGE_ID:
            raise ValueError("page_id must identify a data or catalog page")
        if isinstance(self.slot_id, bool) or not isinstance(self.slot_id, int):
            raise TypeError("slot_id must be an int")
        if self.slot_id < 0:
            raise ValueError("slot_id must be non-negative")
        if isinstance(self.generation, bool) or not isinstance(self.generation, int):
            raise TypeError("generation must be an int")
        if not 0 <= self.generation <= 0xFFFFFF:
            raise ValueError("generation must be between 0 and 0xFFFFFF")


@dataclass(frozen=True, slots=True)
class StoredRow:
    """A decoded row paired with the storage location that produced it."""

    row_id: RowId
    values: Row

    def __post_init__(self) -> None:
        if not isinstance(self.row_id, RowId):
            raise TypeError("row_id must be a RowId")
        _validate_row(self.values, field_name="values")


class WriteKind(Enum):
    """物理写入动作，用于把表页变化交给索引和事务层。"""

    CREATE_TABLE = "CREATE_TABLE"
    CREATE_INDEX = "CREATE_INDEX"
    INSERT = "INSERT"
    UPDATE = "UPDATE"
    DELETE = "DELETE"


_TOKEN_CONSTRUCTION_KEY = object()


@dataclass(frozen=True, slots=True)
class ValidatedWriteToken:
    """Validator-issued identity for one prepared write in one session.

    Handoff note: this value object is ready for callers, but the current
    private issue hook is only a bridge for contract tests. The Session-owned
    Validator must become the only production caller before StorageEngine
    batch writes are enabled; do not use ``_issue_validated_write_token`` from
    Executor or application code.
    """

    session_id: UUID
    catalog_generation: int
    prepared_id: UUID

    def __init__(
        self,
        session_id: UUID,
        catalog_generation: int,
        prepared_id: UUID,
        *,
        _construction_key: object = None,
    ) -> None:
        if _construction_key is not _TOKEN_CONSTRUCTION_KEY:
            raise TypeError(
                "ValidatedWriteToken can only be issued by the validator factory"
            )
        if not isinstance(session_id, UUID):
            raise TypeError("session_id must be a UUID")
        if type(catalog_generation) is not int:
            raise TypeError("catalog_generation must be an int")
        if catalog_generation < 0:
            raise ValueError("catalog_generation must be non-negative")
        if not isinstance(prepared_id, UUID):
            raise TypeError("prepared_id must be a UUID")
        object.__setattr__(self, "session_id", session_id)
        object.__setattr__(self, "catalog_generation", catalog_generation)
        object.__setattr__(self, "prepared_id", prepared_id)


def _issue_validated_write_token(
    session_id: UUID, catalog_generation: int, prepared_id: UUID
) -> ValidatedWriteToken:
    """Temporary internal hook for contract tests and the future Validator.

    This function deliberately has no Session registry or one-shot consume
    tracking yet. The eventual Validator factory must own those checks and
    replace direct test use before this contract is promoted from WIP.
    """
    return ValidatedWriteToken(
        session_id,
        catalog_generation,
        prepared_id,
        _construction_key=_TOKEN_CONSTRUCTION_KEY,
    )


@dataclass(frozen=True, slots=True)
class RowMovement:
    """一条记录在物理层的变化；两端保存完整 StoredRow。"""

    old: StoredRow | None
    new: StoredRow | None

    def __post_init__(self) -> None:
        for value in (self.old, self.new):
            if value is not None and not isinstance(value, StoredRow):
                raise TypeError("movement values must be StoredRow or None")
        if self.old is None and self.new is None:
            raise ValueError("movement must contain an old or new row")


@dataclass(frozen=True, slots=True)
class RowUpdate:
    """批量 UPDATE 的单行输入，字段与项目总计划保持一致。"""

    row_id: RowId
    expected_old: Row
    new_row: Row

    def __post_init__(self) -> None:
        if not isinstance(self.row_id, RowId):
            raise TypeError("row_id must be a RowId")
        _validate_row(self.expected_old, field_name="expected_old")
        _validate_row(self.new_row, field_name="new_row")


@dataclass(frozen=True, slots=True)
class UpdateBatch:
    """不可变更新批次；空批次合法并且不会产生页面副作用。"""

    items: tuple[RowUpdate, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.items, tuple):
            raise TypeError("items must be a tuple")
        if any(not isinstance(update, RowUpdate) for update in self.items):
            raise TypeError("items must contain only RowUpdate values")
        row_ids = [update.row_id for update in self.items]
        if len(set(row_ids)) != len(row_ids):
            raise ValueError("items must not contain duplicate row_id values")


@runtime_checkable
class RowScan(Protocol):
    """Closeable iterator returned by ``StorageEngine.scan_rows``."""

    def __iter__(self) -> "RowScan": ...

    def __next__(self) -> StoredRow: ...

    def close(self) -> None: ...
