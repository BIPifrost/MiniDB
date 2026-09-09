"""已有执行逻辑的接口对接测试，不替其他成员实现空白模块。

使用正式 Plan、Schema、CatalogManager 和已存在的内存存储替身。
源码位置与建表编码预检使用正式接口；
过滤测试的 Mock 只给出预设判断结果，不实现表达式求值。
这些测试不代表完整 SQL 执行链或磁盘持久化通过。
"""

import unittest
from unittest.mock import MagicMock, call, patch

from fakes.in_memory_storage_engine import InMemoryStorageEngine
from fixtures.contracts import STUDENT_CATALOG_ROWS, STUDENT_SCHEMA, STUDENT_TABLE
from minidb.catalog.catalog import SYSTEM_CATALOG_TABLE
from minidb.catalog.catalog_manager import CatalogManager
from minidb.compiler.bound import BoundBinary, BoundColumn, BoundLiteral
from minidb.compiler.plan import (
    CreateTablePlan,
    DeletePlan,
    FilterPlan,
    InsertPlan,
    ProjectPlan,
    SeqScanPlan,
)
from minidb.core.expressions import ExprOp
from minidb.core.errors import DbError, ErrorStage, INVALID_PLAN, TABLE_EXISTS
from minidb.core.records import RowId
from minidb.core.result import ResultColumn
from minidb.core.schema import DataType
from minidb.core.source import SourcePos, SourceSpan
from minidb.engine import expression_eval
from minidb.engine.context import ExecutionContext
from minidb.engine.executor import Executor


class ExecutorIntegrationTests(unittest.TestCase):
    """重点检查字段传递、扫描关闭和 RowId，不模拟未实现的磁盘能力。"""

    def setUp(self):
        """用已有七字段目录行装配正式目录，位置使用正式 SourceSpan。"""
        self.span = SourceSpan(SourcePos(1, 1, 0), SourcePos(1, 2, 1), "test.sql")
        self.storage = InMemoryStorageEngine()
        self.addCleanup(self.storage.abort)
        CatalogManager.bootstrap_or_load(self.storage, True)
        self.storage.create_heap(STUDENT_TABLE.ref.table_id)
        for row in STUDENT_CATALOG_ROWS:
            self.storage.insert_row(SYSTEM_CATALOG_TABLE, row)
        self.catalog = CatalogManager.bootstrap_or_load(self.storage, False)
        self.context = ExecutionContext(self.catalog, self.storage)
        self.executor = Executor()
        self.table = self.catalog.find_table("student")

    def _insert_students(self):
        """通过正式 InsertPlan 放入两行，检查已有插入逻辑的返回值。"""
        for row in ((1, "Alice", 20), (2, "Bob", 17)):
            result = self.executor.execute(InsertPlan(self.table, row, self.span), self.context)
            self.assertEqual(result.affected_rows, 1)

    def _project(self, indexes=(0, 1, 2), child=None):
        """按正式列序号构造投影；不再使用旧计划中的字符串列名。"""
        columns = self.table.schema.columns
        return ProjectPlan(
            SeqScanPlan(self.table, self.span) if child is None else child,
            indexes,
            tuple(ResultColumn(columns[index].name, columns[index].data_type) for index in indexes),
            self.span,
        )

    def _filtered_scan(self):
        """构造正式的 age >= 18 条件，仅描述表达式，不实现求值。"""
        predicate = BoundBinary(
            ExprOp.GE, BoundColumn(2, DataType.INT, self.span),
            BoundLiteral(18, DataType.INT, self.span), DataType.BOOL,
            self.span, self.span,
        )
        return FilterPlan(SeqScanPlan(self.table, self.span), predicate, self.span)

    def test_insert_and_project_use_bound_column_order(self):
        """投影支持换序和重复列；结果类型取正式 DataType，扫描已关闭。"""
        self._insert_students()
        result = self.executor.execute(self._project((1, 0, 1)), self.context)
        self.assertEqual([column.name for column in result.columns], ["name", "id", "name"])
        self.assertEqual([column.data_type for column in result.columns],
                         [DataType.VARCHAR, DataType.INT, DataType.VARCHAR])
        self.assertEqual(result.rows, [("Alice", 1, "Alice"), ("Bob", 2, "Bob")])
        self.assertIsNone(result.affected_rows)
        self.assertEqual(self.storage.active_scan_count, 0)
        self.assertEqual(self.storage.sync_count, 0)

    def test_create_passes_complete_table_to_existing_catalog(self):
        """建表接通表号、根页、真实 RowCodec 预检和目录行写入。"""
        plan = CreateTablePlan("course", STUDENT_SCHEMA, self.span)
        result = self.executor.execute(plan, self.context)
        table = self.catalog.find_table("course")
        self.assertEqual((table.ref.table_id, table.ref.root_page_id), (2, 3))
        self.assertEqual(table.schema, STUDENT_SCHEMA)
        self.storage.validate_table_root(table)
        restored = CatalogManager.bootstrap_or_load(self.storage, False)
        self.assertEqual(restored.find_table("course"), table)
        self.assertEqual(result.affected_rows, 0)
        self.assertEqual(self.storage.sync_count, 0)

    def test_repeated_create_is_rejected_before_allocating_or_writing(self):
        """同一建表计划执行两次，第二次须查当前目录，不能再领表号或分配根页。"""
        plan = CreateTablePlan("course", STUDENT_SCHEMA, self.span)
        self.executor.execute(plan, self.context)
        original_table = self.catalog.find_table("course")
        with patch.object(
            self.catalog,
            "reserve_table_id",
            wraps=self.catalog.reserve_table_id,
        ) as reserve, patch.object(
            self.storage,
            "create_heap",
            wraps=self.storage.create_heap,
        ) as create, patch.object(
            self.storage,
            "insert_row",
            wraps=self.storage.insert_row,
        ) as insert:
            with self.assertRaises(DbError) as raised:
                self.executor.execute(plan, self.context)
            reserve.assert_not_called()
            create.assert_not_called()
            insert.assert_not_called()
        error = raised.exception
        self.assertEqual(error.code, TABLE_EXISTS)
        self.assertIs(error.stage, ErrorStage.SEMANTIC)
        self.assertIs(error.span, self.span)
        self.assertEqual(error.context, {"operation": "Executor.execute", "table_name": "course"})
        self.assertEqual(self.catalog.find_table("course"), original_table)
        self.assertEqual(self.storage.sync_count, 0)

    def test_delete_closes_scan_before_using_row_ids_and_reclaiming(self):
        """内存替身会拒绝扫描期间的写入，删除必须先关闭扫描再逐行标记。"""
        self._insert_students()
        plan = DeletePlan(self.table, SeqScanPlan(self.table, self.span), self.span)
        events = []
        delete_row = self.storage.delete_row
        reclaim = self.storage.reclaim_empty_pages

        def delete(table, row_id):
            """记录已有存储方法收到的参数，不重新实现删除。"""
            events.append(("delete", row_id))
            return delete_row(table, row_id)

        def reclaim_pages(table):
            """记录回收时机，再交给原有内存替身。"""
            events.append(("reclaim", table))
            return reclaim(table)

        with patch.object(self.storage, "delete_row", side_effect=delete), \
             patch.object(self.storage, "reclaim_empty_pages", side_effect=reclaim_pages):
            result = self.executor.execute(plan, self.context)
        self.assertEqual(events, [
            ("delete", RowId(2, 0)), ("delete", RowId(2, 1)), ("reclaim", self.table),
        ])
        self.assertEqual(result.affected_rows, 2)
        self.assertEqual(self.executor.execute(self._project(), self.context).rows, [])
        self.assertEqual(self.executor.execute(plan, self.context).affected_rows, 0)
        self.assertEqual(self.storage.active_scan_count, 0)
        self.assertEqual(self.storage.sync_count, 0)

    def test_formal_evaluator_filters_rows_without_a_mock(self):
        self._insert_students()
        result = self.executor.execute(
            self._project((1,), self._filtered_scan()),
            self.context,
        )
        self.assertEqual(result.rows, [("Alice",)])
        self.assertEqual(self.storage.active_scan_count, 0)

    def test_filter_passes_bound_expression_and_full_row_to_evaluator(self):
        """Mock 只验证约定的参数传递，不作为 WHERE 求值已实现的证据。"""
        self._insert_students()
        filtered = self._filtered_scan()
        with patch.object(
            expression_eval,
            "evaluate",
            side_effect=[True, False],
        ) as evaluate:
            result = self.executor.execute(self._project((1,), filtered), self.context)
        self.assertEqual(evaluate.call_args_list, [
            call(filtered.predicate, (1, "Alice", 20)),
            call(filtered.predicate, (2, "Bob", 17)),
        ])
        self.assertEqual(result.rows, [("Alice",)])

    def test_filtered_delete_preserves_original_row_id(self):
        """预设只选中第二行，确认删除使用其原始位置，而非过滤后的行序号。"""
        self._insert_students()
        filtered = self._filtered_scan()
        with patch.object(
            expression_eval,
            "evaluate",
            side_effect=[False, True],
        ), patch.object(
            self.storage,
            "delete_row",
            wraps=self.storage.delete_row,
        ) as delete:
            result = self.executor.execute(
                DeletePlan(self.table, filtered, self.span),
                self.context,
            )
        delete.assert_called_once_with(self.table, RowId(2, 1))
        self.assertEqual(result.affected_rows, 1)
        self.assertEqual(
            self.executor.execute(self._project(), self.context).rows,
            [(1, "Alice", 20)],
        )

    def test_invalid_roots_are_rejected_before_storage(self):
        """复用张振的结构校验，现在直接验证接通后的公共 DbError。"""
        plans = (SeqScanPlan(self.table, self.span), self._filtered_scan(),
                 DeletePlan(self.table, self._project(), self.span))
        for plan in plans:
            with self.subTest(plan=type(plan).__name__):
                with patch.object(self.storage, "scan_rows") as scan:
                    with self.assertRaises(DbError) as raised:
                        self.executor.execute(plan, self.context)
                    self.assertEqual(raised.exception.code, INVALID_PLAN)
                    self.assertIs(raised.exception.stage, ErrorStage.PLAN)
                    scan.assert_not_called()

    def test_scan_failure_still_closes_and_prevents_delete(self):
        """扫描读取失败时仍关闭资源，且不能删除已经读到的部分记录。"""
        self._insert_students()
        scan = self.storage.scan_rows(self.table)
        failure = OSError("读取失败")
        broken_scan = MagicMock(spec_set=["__iter__", "close"])

        def broken_rows():
            """先从真实替身读一行，再注入失败，观察执行器是否错误地提前删除。"""
            yield next(scan)
            raise failure

        broken_scan.__iter__.side_effect = broken_rows
        broken_scan.close.side_effect = scan.close
        with patch.object(self.storage, "scan_rows", return_value=broken_scan), \
             patch.object(self.storage, "delete_row") as delete:
            with self.assertRaises(OSError) as raised:
                self.executor.execute(
                    DeletePlan(
                        self.table,
                        SeqScanPlan(self.table, self.span),
                        self.span,
                    ),
                    self.context,
                )
            delete.assert_not_called()
        self.assertIs(raised.exception, failure)
        broken_scan.close.assert_called_once_with()
        self.assertEqual(self.storage.active_scan_count, 0)


if __name__ == "__main__":
    unittest.main()
