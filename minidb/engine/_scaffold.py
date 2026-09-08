"""TEMPORARY: private dependencies for early executor development.

Do not import this module from new production code.  It exists only so that
``Executor.execute`` can be exercised before these teammate-owned interfaces
are available:

* Zhang Zhen: ``core/schema.py``, ``compiler/plan.py`` and
  ``catalog/catalog_manager.py``;
* Liao Jie and Zhou Shengrong: the page-backed ``StorageEngine`` integration.

Replacement checklist:

1. Import the real schema and plan classes in ``engine/executor.py``.
2. Use the real ``CatalogManager`` through ``ExecutionContext.catalog``.
3. Use ``tests.fakes.InMemoryStorageEngine`` for isolated executor tests and
   the page-backed engine for integration tests.
4. Delete this module and ``test_executor_scaffold.py``.

Nothing defined here is a public contract.  The underscore in the filename is
intentional and warns other modules not to depend on these definitions.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable, Sequence

from minidb.core.records import Row


Predicate = Callable[[Row], bool]


# Temporary schema stand-ins.  The official definitions will come from
# Zhang Zhen's minidb.core.schema module.
@dataclass(frozen=True)
class ColumnDef:
    name: str
    data_type: str = "VARCHAR"


@dataclass(frozen=True)
class TableDef:
    name: str
    columns: tuple[ColumnDef, ...]

    @classmethod
    def from_names(cls, name: str, columns: Iterable[str]) -> "TableDef":
        return cls(name=name, columns=tuple(ColumnDef(column) for column in columns))


class CatalogManager:
    """Temporary catalog; does not model persistent system-catalog rows."""

    def __init__(self) -> None:
        self._tables: dict[str, TableDef] = {}

    def find_table(self, name: str) -> TableDef | None:
        return self._tables.get(name.lower())

    def register_table(self, table: TableDef) -> None:
        key = table.name.lower()
        if key in self._tables:
            raise ValueError(f"table already exists: {table.name}")
        self._tables[key] = table


class StorageEngine:
    """Legacy executor stub; it does NOT implement the public storage contract.

    For new tests use tests.fakes.in_memory_storage_engine instead.  This class
    remains only because the early executor scaffold calls create_table and
    delete_rows, methods which do not exist on the final StorageEngine API.
    """

    def __init__(self) -> None:
        self._rows: dict[str, list[Row]] = {}

    def create_table(self, table: TableDef) -> None:
        self._rows.setdefault(table.name.lower(), [])

    def insert_row(self, table: TableDef, row: Sequence[int | str]) -> None:
        if len(row) != len(table.columns):
            raise ValueError("row does not match table schema")
        self._rows.setdefault(table.name.lower(), []).append(tuple(row))

    def scan_rows(self, table: TableDef) -> list[Row]:
        return list(self._rows.get(table.name.lower(), []))

    def delete_rows(self, table: TableDef, predicate: Predicate | None = None) -> int:
        rows = self._rows.setdefault(table.name.lower(), [])
        if predicate is None:
            deleted = len(rows)
            rows.clear()
            return deleted
        remaining = [row for row in rows if not predicate(row)]
        deleted = len(rows) - len(remaining)
        rows[:] = remaining
        return deleted

    def sync(self) -> None:
        pass


# Temporary plans.  Replace this entire section with imports from Zhang
# Zhen's minidb.compiler.plan once that module is merged.
class Plan:
    pass


@dataclass(frozen=True)
class ValuesPlan(Plan):
    columns: tuple[str, ...]
    rows: tuple[Row, ...]


@dataclass(frozen=True)
class CreateTablePlan(Plan):
    table: TableDef


@dataclass(frozen=True)
class InsertPlan(Plan):
    table_name: str
    row: Row


@dataclass(frozen=True)
class SeqScanPlan(Plan):
    table_name: str


@dataclass(frozen=True)
class FilterPlan(Plan):
    child: Plan
    predicate: Predicate


@dataclass(frozen=True)
class ProjectPlan(Plan):
    child: Plan
    columns: tuple[str, ...]


@dataclass(frozen=True)
class DeletePlan(Plan):
    table_name: str
    predicate: Predicate | None = None
