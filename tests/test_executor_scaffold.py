"""TEMPORARY executor tests using private plan/catalog/storage stand-ins.

Delete this file together with ``minidb.engine._scaffold`` after Zhang Zhen's
real Plan and CatalogManager types are connected.  The long-term executor
tests should use real Plan objects plus ``InMemoryStorageEngine``.
"""

import unittest

from minidb.engine._scaffold import (
    CatalogManager,
    CreateTablePlan,
    DeletePlan,
    FilterPlan,
    InsertPlan,
    ProjectPlan,
    SeqScanPlan,
    StorageEngine,
    TableDef,
)
from minidb.engine.context import ExecutionContext
from minidb.engine.executor import Executor


class ExecutorScaffoldTests(unittest.TestCase):
    def setUp(self):
        self.catalog = CatalogManager()
        self.storage = StorageEngine()
        self.context = ExecutionContext(self.catalog, self.storage)
        self.executor = Executor()
        self.table = TableDef.from_names("student", ("id", "name", "age"))
        self.executor.execute(CreateTablePlan(self.table), self.context)

    def test_fixed_plan_can_insert_scan_filter_and_project(self):
        self.executor.execute(InsertPlan("student", (1, "Alice", 20)), self.context)
        self.executor.execute(InsertPlan("student", (2, "Bob", 17)), self.context)

        plan = ProjectPlan(
            FilterPlan(
                SeqScanPlan("student"),
                lambda row: row[2] >= 18,
            ),
            ("id", "name"),
        )

        result = self.executor.execute(plan, self.context)

        self.assertEqual(tuple(column.name for column in result.columns), ("id", "name"))
        self.assertEqual(result.rows, [(1, "Alice")])
        self.assertIsNone(result.affected_rows)

    def test_delete_returns_affected_row_count(self):
        self.executor.execute(InsertPlan("student", (1, "Alice", 20)), self.context)
        self.executor.execute(InsertPlan("student", (2, "Bob", 17)), self.context)

        result = self.executor.execute(
            DeletePlan("student", lambda row: row[2] < 18), self.context
        )

        self.assertEqual(result.affected_rows, 1)
        remaining = self.executor.execute(SeqScanPlan("student"), self.context)
        self.assertEqual(remaining.rows, [(1, "Alice", 20)])


if __name__ == "__main__":
    unittest.main()
