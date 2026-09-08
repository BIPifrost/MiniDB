"""STABLE P0 CONTRACT shared with CatalogManager and Executor.

The method names and signatures in this file are ready for teammates to use.
Only the concrete page-backed implementation is pending.  That implementation
will compose three neighboring pieces rather than redefining them:

* Zhao Kaihang's ``RowCodec`` converts ``Row`` values to and from bytes;
* Zhou Shengrong's ``DataPage`` organizes encoded records and slots;
* Liao Jie's ``BufferPool`` and ``FileManager`` allocate, cache and persist
  complete pages.

Tests that need storage before those pieces land should instantiate
``tests.fakes.in_memory_storage_engine.InMemoryStorageEngine``.  Do not put a
second production StorageEngine interface in another module.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from minidb.core.records import Row, RowId, RowScan

# TableDef belongs to Zhang Zhen.  Keeping this import type-checking-only lets
# teammates import the storage contract before core/schema.py is merged.
if TYPE_CHECKING:
    from minidb.core.schema import TableDef


class StorageEngine(ABC):
    """Public P0 interface for table record storage.

    The concrete page-backed implementation will be added after RowCodec and
    BufferPool are available. Callers should depend only on these methods.
    """

    @abstractmethod
    def create_heap(self, table_id: int) -> int:
        """Allocate and initialize a user table root page."""

    @abstractmethod
    def initialize_reserved_heap(self, table: "TableDef") -> None:
        """Initialize the reserved system-catalog root in a new database."""

    @abstractmethod
    def validate_table_root(self, table: "TableDef") -> None:
        """Validate that a table definition points at its own valid root page."""

    @abstractmethod
    def insert_row(self, table: "TableDef", row: Row) -> RowId:
        """Insert one schema-ordered row and return its current location."""

    @abstractmethod
    def scan_rows(self, table: "TableDef") -> RowScan:
        """Return a closeable streaming scan over the table's stored rows."""

    @abstractmethod
    def delete_row(self, table: "TableDef", row_id: RowId) -> bool:
        """Mark one live record deleted, returning False if already deleted."""

    @abstractmethod
    def reclaim_empty_pages(self, table: "TableDef") -> int:
        """Release empty non-root pages after all active scans have closed."""

    @abstractmethod
    def sync(self) -> None:
        """Flush dirty pages and synchronize the backing file."""

    @abstractmethod
    def close(self) -> None:
        """Synchronize and close a usable storage engine."""

    @abstractmethod
    def abort(self) -> None:
        """Stop operations and close resources without flushing dirty pages."""
