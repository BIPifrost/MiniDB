"""Shared immutable row and record-location types.

This module is the single source of truth for values exchanged between the
compiler, row codec, storage engine, catalog and executor.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, TypeAlias, runtime_checkable


Row: TypeAlias = tuple[int | str, ...]


def _validate_row(values: object, *, field_name: str) -> None:
    if not isinstance(values, tuple):
        raise TypeError(f"{field_name} must be a tuple")
    for value in values:
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            raise TypeError(f"{field_name} values must be int or str")


@dataclass(frozen=True, slots=True)
class RowId:
    """Location of a row for the duration of the current storage operation."""

    page_id: int
    slot_id: int

    def __post_init__(self) -> None:
        if isinstance(self.page_id, bool) or not isinstance(self.page_id, int):
            raise TypeError("page_id must be an int")
        if not 1 <= self.page_id <= 0xFFFFFFFE:
            raise ValueError("page_id must identify a data or catalog page")
        if isinstance(self.slot_id, bool) or not isinstance(self.slot_id, int):
            raise TypeError("slot_id must be an int")
        if self.slot_id < 0:
            raise ValueError("slot_id must be non-negative")


@dataclass(frozen=True, slots=True)
class StoredRow:
    """A decoded row paired with the storage location that produced it."""

    row_id: RowId
    values: Row

    def __post_init__(self) -> None:
        if not isinstance(self.row_id, RowId):
            raise TypeError("row_id must be a RowId")
        _validate_row(self.values, field_name="values")


@runtime_checkable
class RowScan(Protocol):
    """Closeable iterator returned by ``StorageEngine.scan_rows``."""

    def __iter__(self) -> "RowScan": ...

    def __next__(self) -> StoredRow: ...

    def close(self) -> None: ...
