"""TEST-ONLY implementation of the stable public StorageEngine contract.

This is not temporary because its method signatures are incomplete; it fully
implements the P0 interface. The formal page-backed StorageEngine, RowCodec,
BufferPool and FileManager now exist; this fake remains only for fast Executor
unit tests that do not need to exercise page layout or disk persistence.

Keep this test double after integration if it remains useful for fast unit
tests, but never select it from the CLI or claim persistence based on it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from minidb.core.disk_types import (
    FIRST_ALLOCATABLE_PAGE_ID, MAX_PAGE_ID, CATALOG_ROOT_PAGE_ID,
)
from minidb.core.records import Row, RowId, RowScan, StoredRow
from minidb.storage.storage_engine import StorageEngine


@dataclass(slots=True)
class _Record:
    values: Row
    deleted: bool = False


@dataclass(slots=True)
class _Heap:
    table_id: int
    root_page_id: int
    records: dict[int, _Record] = field(default_factory=dict)
    next_slot_id: int = 0


class _MemoryRowScan(RowScan):
    def __init__(
        self,
        rows: tuple[StoredRow, ...],
        on_close: Callable[["_MemoryRowScan"], None],
    ) -> None:
        self._rows = iter(rows)
        self._on_close = on_close
        self._closed = False

    def __iter__(self) -> "_MemoryRowScan":
        return self

    def __next__(self) -> StoredRow:
        if self._closed:
            raise StopIteration
        try:
            return next(self._rows)
        except StopIteration:
            self.close()
            raise

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._on_close(self)


class InMemoryStorageEngine(StorageEngine):
    """Behavioral test double with no byte encoding or persistence.

    This class models table heaps, row locations, scan lifetime and resource
    state. It intentionally does not model page capacity, RowCodec bytes,
    BufferPool behavior, disk failure or restart persistence; those belong to
    the page-backed implementation and its integration tests.
    """

    def __init__(self, *, first_user_page_id: int = FIRST_ALLOCATABLE_PAGE_ID) -> None:
        if (
            isinstance(first_user_page_id, bool)
            or not isinstance(first_user_page_id, int)
            or not FIRST_ALLOCATABLE_PAGE_ID <= first_user_page_id <= MAX_PAGE_ID
        ):
            raise ValueError("first_user_page_id must be a valid user page id")
        self._next_page_id = first_user_page_id
        self._heaps: dict[int, _Heap] = {}
        self._active_scans: set[_MemoryRowScan] = set()
        self._closed = False
        self._failed = False
        self._sync_count = 0

    @property
    def active_scan_count(self) -> int:
        return len(self._active_scans)

    @property
    def sync_count(self) -> int:
        return self._sync_count

    @property
    def is_closed(self) -> bool:
        return self._closed

    def create_heap(self, table_id: int) -> int:
        self._require_mutable("create_heap")
        self._validate_table_id(table_id, allow_catalog=False)
        if table_id in self._heaps:
            raise ValueError(f"heap already exists for table_id {table_id}")
        if self._next_page_id > MAX_PAGE_ID:
            raise OverflowError("page id limit reached")

        root_page_id = self._next_page_id
        self._next_page_id += 1
        self._heaps[table_id] = _Heap(table_id, root_page_id)
        return root_page_id

    def initialize_reserved_heap(self, table: Any) -> None:
        self._require_mutable("initialize_reserved_heap")
        table_id, root_page_id = self._table_identity(table)
        if table_id != 0 or root_page_id != CATALOG_ROOT_PAGE_ID:
            raise ValueError("the reserved catalog heap must be table 0 on page 1")
        if table_id in self._heaps:
            raise ValueError("the reserved catalog heap is already initialized")
        self._heaps[0] = _Heap(table_id=0, root_page_id=CATALOG_ROOT_PAGE_ID)

    def validate_table_root(self, table: Any) -> None:
        self._require_open("validate_table_root")
        table_id, root_page_id = self._table_identity(table)
        heap = self._heaps.get(table_id)
        if heap is None:
            raise KeyError(f"heap not found for table_id {table_id}")
        if heap.root_page_id != root_page_id:
            raise ValueError(
                f"root page mismatch: expected {heap.root_page_id}, got {root_page_id}"
            )

    def insert_row(self, table: Any, row: Row) -> RowId:
        self._require_mutable("insert_row")
        heap = self._require_heap(table)
        self._validate_row_for_table(table, row)

        slot_id = heap.next_slot_id
        heap.next_slot_id += 1
        heap.records[slot_id] = _Record(values=tuple(row))
        return RowId(heap.root_page_id, slot_id)

    def scan_rows(self, table: Any) -> RowScan:
        self._require_open("scan_rows")
        heap = self._require_heap(table)
        rows = tuple(
            StoredRow(RowId(heap.root_page_id, slot_id), record.values)
            for slot_id, record in heap.records.items()
            if not record.deleted
        )
        scan = _MemoryRowScan(rows, self._scan_closed)
        self._active_scans.add(scan)
        return scan

    def delete_row(self, table: Any, row_id: RowId) -> bool:
        self._require_mutable("delete_row")
        if not isinstance(row_id, RowId):
            raise TypeError("row_id must be a RowId")
        heap = self._require_heap(table)
        if row_id.page_id != heap.root_page_id:
            raise ValueError("row_id does not belong to the table heap")
        record = heap.records.get(row_id.slot_id)
        if record is None:
            raise IndexError(f"slot does not exist: {row_id.slot_id}")
        if record.deleted:
            return False
        record.deleted = True
        return True

    def reclaim_empty_pages(self, table: Any) -> int:
        self._require_mutable("reclaim_empty_pages")
        self._require_heap(table)
        # This fake models one permanent root page per table and no overflow pages.
        return 0

    def sync(self) -> None:
        self._require_open("sync")
        self._sync_count += 1

    def close(self) -> None:
        if self._closed:
            return
        if self._active_scans:
            raise RuntimeError("ACTIVE_SCAN: close requires all scans to be closed")
        self.sync()
        self._closed = True

    def abort(self) -> None:
        if self._closed:
            return
        self._failed = True
        for scan in tuple(self._active_scans):
            scan.close()
        self._closed = True

    def _scan_closed(self, scan: _MemoryRowScan) -> None:
        self._active_scans.discard(scan)

    def _require_heap(self, table: Any) -> _Heap:
        self.validate_table_root(table)
        table_id, _ = self._table_identity(table)
        return self._heaps[table_id]

    def _require_open(self, operation: str) -> None:
        if self._closed:
            state = "failed" if self._failed else "closed"
            raise RuntimeError(f"CLOSED: cannot {operation}; engine is {state}")

    def _require_mutable(self, operation: str) -> None:
        self._require_open(operation)
        if self._active_scans:
            raise RuntimeError(
                f"ACTIVE_SCAN: cannot {operation} while a scan is active"
            )

    @staticmethod
    def _validate_table_id(table_id: object, *, allow_catalog: bool) -> None:
        minimum = 0 if allow_catalog else 1
        if isinstance(table_id, bool) or not isinstance(table_id, int):
            raise TypeError("table_id must be an int")
        if not minimum <= table_id <= 0xFFFFFFFE:
            raise ValueError("table_id is outside the supported range")

    @classmethod
    def _table_identity(cls, table: Any) -> tuple[int, int]:
        try:
            table_id = table.ref.table_id
            root_page_id = table.ref.root_page_id
        except AttributeError as exc:
            raise TypeError("table must provide ref.table_id and ref.root_page_id") from exc
        cls._validate_table_id(table_id, allow_catalog=True)
        if (
            isinstance(root_page_id, bool)
            or not isinstance(root_page_id, int)
            or not CATALOG_ROOT_PAGE_ID <= root_page_id <= MAX_PAGE_ID
        ):
            raise ValueError("root_page_id is outside the supported range")
        return table_id, root_page_id

    @staticmethod
    def _validate_row_for_table(table: Any, row: object) -> None:
        if not isinstance(row, tuple):
            raise TypeError("row must be a tuple")
        try:
            columns = table.schema.columns
        except AttributeError as exc:
            raise TypeError("table must provide schema.columns") from exc
        if len(row) != len(columns):
            raise ValueError("row length does not match the table schema")

        for value, column in zip(row, columns, strict=True):
            type_name = getattr(column.data_type, "name", column.data_type)
            if type_name == "INT":
                valid = isinstance(value, int) and not isinstance(value, bool)
            elif type_name == "VARCHAR":
                valid = isinstance(value, str)
            else:
                raise ValueError(f"unsupported table column type: {type_name}")
            if not valid:
                raise TypeError(
                    f"value for column {column.name} does not match {type_name}"
                )
