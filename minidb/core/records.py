"""Shared immutable row and record-location types.

This module is the single source of truth for values exchanged between the
compiler, row codec, storage engine, catalog and executor.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol, TypeAlias, runtime_checkable


from minidb.core.disk_types import CATALOG_ROOT_PAGE_ID, MAX_PAGE_ID


Row: TypeAlias = tuple[int | str, ...]


def _validate_row(values: object, *, field_name: str) -> None:
    if not isinstance(values, tuple):
        raise TypeError(f"{field_name} must be a tuple")
    for value in values:
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            raise TypeError(f"{field_name} values must be int or str")


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

    INSERT = "INSERT"
    UPDATE = "UPDATE"
    DELETE = "DELETE"


@dataclass(frozen=True, slots=True)
class RowMovement:
    """一条记录在物理层的身份变化；None 表示插入或删除的一端。"""

    kind: WriteKind
    old: RowId | None
    new: RowId | None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, WriteKind):
            raise TypeError("kind must be a WriteKind")
        if self.kind is WriteKind.INSERT and (self.old is not None or self.new is None):
            raise ValueError("INSERT movement requires only new RowId")
        if self.kind is WriteKind.UPDATE and (self.old is None or self.new is None):
            raise ValueError("UPDATE movement requires old and new RowId")
        if self.kind is WriteKind.DELETE and (self.old is None or self.new is not None):
            raise ValueError("DELETE movement requires only old RowId")
        for value in (self.old, self.new):
            if value is not None and not isinstance(value, RowId):
                raise TypeError("movement identities must be RowId or None")


@dataclass(frozen=True, slots=True)
class RowUpdate:
    """批量 UPDATE 的单行输入；old 是准备阶段读取的完整旧值。"""

    old: StoredRow
    new_values: Row

    def __post_init__(self) -> None:
        if not isinstance(self.old, StoredRow):
            raise TypeError("old must be a StoredRow")
        _validate_row(self.new_values, field_name="new_values")


@dataclass(frozen=True, slots=True)
class UpdateBatch:
    """不可变更新批次；空批次合法并且不会产生页面副作用。"""

    updates: tuple[RowUpdate, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.updates, tuple):
            raise TypeError("updates must be a tuple")
        if any(not isinstance(update, RowUpdate) for update in self.updates):
            raise TypeError("updates must contain only RowUpdate values")


@runtime_checkable
class RowScan(Protocol):
    """Closeable iterator returned by ``StorageEngine.scan_rows``."""

    def __iter__(self) -> "RowScan": ...

    def __next__(self) -> StoredRow: ...

    def close(self) -> None: ...
