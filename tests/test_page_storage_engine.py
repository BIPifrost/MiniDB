"""正式页式 StorageEngine 与临时内存 BufferPool 的对接测试。"""

import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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
from minidb.core.disk_types import INVALID_PAGE_ID
from minidb.core.errors import (
    ACTIVE_SCAN,
    INVALID_ARGUMENT,
    IO_SYNC_FAILED,
    IO_WRITE_FAILED,
    PAGE_CORRUPTED,
    PAGE_ID_INVALID,
    ROW_ENCODING_ERROR,
    DbError,
    ErrorStage,
)
from minidb.core.expressions import ExprOp
from minidb.core.records import RowId, RowScan, StoredRow
from minidb.core.result import ResultColumn
from minidb.core.schema import ColumnDef, DataType, Schema
from minidb.engine.context import ExecutionContext
from minidb.engine.executor import Executor
from minidb.storage.data_page import DataPage
from minidb.storage.buffer_pool import BufferPool
from minidb.storage.file_manager import FileManager
from minidb.storage.row_codec import RowCodec
from minidb.storage.storage_engine import StorageEngine
from tests.fakes.in_memory_buffer_pool import InMemoryBufferPool, InMemoryFileManager
from tests.fixtures.contracts import STUDENT_SCHEMA, STUDENT_TABLE, span


class PageStorageEngineTests(unittest.TestCase):
    def setUp(self):
        self.file_manager = InMemoryFileManager()
        self.buffer = InMemoryBufferPool(self.file_manager, capacity=2)
        self.storage = StorageEngine(self.buffer, RowCodec(), self.file_manager)
        self.root_page_id = self.storage.create_heap(STUDENT_TABLE.ref.table_id)
        self.assertEqual(self.root_page_id, STUDENT_TABLE.ref.root_page_id)
        self.table = STUDENT_TABLE

    def tearDown(self):
        if not self.storage.is_closed:
            self.storage.abort()

    def assert_error(self, code, action):
        with self.assertRaises(DbError) as raised:
            action()
        self.assertEqual(raised.exception.code, code)
        return raised.exception

    def test_constructor_rejects_a_different_file_manager_before_io(self):
        other = InMemoryFileManager()
        buffer = InMemoryBufferPool(other)
        self.assert_error(
            INVALID_ARGUMENT,
            lambda: StorageEngine(buffer, RowCodec(), InMemoryFileManager()),
        )
        self.assertEqual(other.read_log, [])
        self.assertEqual(other.write_log, [])

    def test_create_heap_writes_a_valid_empty_root(self):
        page = DataPage(
            self.buffer.get_page(self.root_page_id),
            page_id=self.root_page_id,
            expected_table_id=self.table.ref.table_id,
        )
        self.assertEqual(page.header.slot_count, 0)
        self.assertEqual(page.header.next_page_id, INVALID_PAGE_ID)

    def test_create_heap_rechecks_the_page_number_returned_by_buffer(self):
        before = tuple(self.file_manager.write_log)
        with patch.object(self.buffer, "new_page", return_value=1):
            self.assert_error(PAGE_ID_INVALID, lambda: self.storage.create_heap(2))
        self.assertEqual(tuple(self.file_manager.write_log), before)

    def test_reserved_catalog_initialization_requires_new_zero_page(self):
        file_manager = InMemoryFileManager()
        buffer = InMemoryBufferPool(file_manager)
        storage = StorageEngine(buffer, RowCodec(), file_manager)
        self.addCleanup(storage.abort)

        storage.initialize_reserved_heap(SYSTEM_CATALOG_TABLE)
        storage.validate_table_root(SYSTEM_CATALOG_TABLE)
        row = (1, "student", 2, 3, 0, "id", "INT")
        self.assertEqual(storage.insert_row(SYSTEM_CATALOG_TABLE, row), RowId(1, 0))
        self.assert_error(
            PAGE_CORRUPTED,
            lambda: storage.initialize_reserved_heap(SYSTEM_CATALOG_TABLE),
        )

    def test_single_page_insert_scan_delete_and_repeat_delete(self):
        row = (1, "Alice", 20)
        row_id = self.storage.insert_row(self.table, row)

        scan = self.storage.scan_rows(self.table)
        self.assertIsInstance(scan, RowScan)
        self.assertEqual(list(scan), [StoredRow(row_id, row)])
        self.assertEqual(self.storage.active_scan_count, 0)

        self.assertTrue(self.storage.delete_row(self.table, row_id))
        self.assertFalse(self.storage.delete_row(self.table, row_id))
        self.assertEqual(list(self.storage.scan_rows(self.table)), [])

    def test_empty_scan_requests_the_root_page_only_once(self):
        """创建扫描时已经校验根页，迭代不能再虚增一次缓存命中。"""
        before = self.buffer.stats().requests
        scan = self.storage.scan_rows(self.table)
        self.assertEqual(self.buffer.stats().requests - before, 1)
        self.assertEqual(list(scan), [])
        self.assertEqual(self.buffer.stats().requests - before, 1)

    def test_large_rows_span_pages_and_scan_in_chain_order(self):
        rows = tuple((index, chr(64 + index) * 3000, 20 + index) for index in range(1, 4))
        row_ids = tuple(self.storage.insert_row(self.table, row) for row in rows)

        self.assertEqual([row_id.page_id for row_id in row_ids], [2, 3, 4])
        self.assertEqual(list(self.storage.scan_rows(self.table)), [
            StoredRow(row_id, row) for row_id, row in zip(row_ids, rows)
        ])
        root = DataPage(self.buffer.get_page(2), page_id=2, expected_table_id=1)
        middle = DataPage(self.buffer.get_page(3), page_id=3, expected_table_id=1)
        self.assertEqual(root.header.next_page_id, 3)
        self.assertEqual(middle.header.next_page_id, 4)

    def test_active_scan_blocks_all_mutations_and_normal_close(self):
        row_id = self.storage.insert_row(self.table, (1, "Alice", 20))
        scan = self.storage.scan_rows(self.table)

        for operation in (
            lambda: self.storage.create_heap(2),
            lambda: self.storage.insert_row(self.table, (2, "Bob", 17)),
            lambda: self.storage.delete_row(self.table, row_id),
            lambda: self.storage.reclaim_empty_pages(self.table),
            self.storage.close,
        ):
            with self.subTest(operation=operation):
                self.assert_error(ACTIVE_SCAN, operation)
        scan.close()
        scan.close()
        self.assertEqual(self.storage.active_scan_count, 0)

    def test_scan_error_closes_itself_and_reports_page_cycle(self):
        self.storage.insert_row(self.table, (1, "A" * 3000, 20))
        self.storage.insert_row(self.table, (2, "B" * 3000, 21))
        # 根页仍合法，让 scan_rows 能成功返回扫描器；第二页自指后，
        # 错误会发生在迭代过程中，正好验证异常路径会自动 close。
        raw = bytearray(self.buffer.get_page(3))
        struct.pack_into("<I", raw, 12, 3)
        self.buffer.write_page(3, bytes(raw))

        scan = self.storage.scan_rows(self.table)
        self.assert_error(PAGE_CORRUPTED, lambda: list(scan))
        self.assertEqual(self.storage.active_scan_count, 0)

    def test_reclaim_unlinks_consecutive_empty_pages_and_keeps_root(self):
        rows = tuple((index, str(index) * 3000, 20) for index in range(1, 4))
        first, second, third = (
            self.storage.insert_row(self.table, row) for row in rows
        )
        self.storage.delete_row(self.table, second)
        self.storage.delete_row(self.table, third)

        self.assertEqual(self.storage.reclaim_empty_pages(self.table), 2)
        root = DataPage(self.buffer.get_page(2), page_id=2, expected_table_id=1)
        self.assertEqual(root.header.next_page_id, INVALID_PAGE_ID)
        self.assertEqual(list(self.storage.scan_rows(self.table)), [StoredRow(first, rows[0])])
        with self.assertRaises(KeyError):
            self.buffer.get_page(3)
        with self.assertRaises(KeyError):
            self.buffer.get_page(4)

    def test_empty_root_is_reset_without_losing_nonempty_successor(self):
        first_row = (1, "A" * 3000, 20)
        second_row = (2, "B" * 3000, 21)
        first = self.storage.insert_row(self.table, first_row)
        second = self.storage.insert_row(self.table, second_row)
        self.storage.delete_row(self.table, first)

        self.assertEqual(self.storage.reclaim_empty_pages(self.table), 0)
        root = DataPage(self.buffer.get_page(2), page_id=2, expected_table_id=1)
        self.assertEqual(root.header.slot_count, 0)
        self.assertEqual(root.header.next_page_id, 3)
        self.assertEqual(list(self.storage.scan_rows(self.table)), [StoredRow(second, second_row)])

    def test_row_id_must_refer_to_a_page_in_the_target_heap(self):
        self.storage.insert_row(self.table, (1, "Alice", 20))
        self.assert_error(
            INVALID_ARGUMENT,
            lambda: self.storage.delete_row(self.table, RowId(99, 0)),
        )

    def test_codec_contract_failure_happens_before_page_access(self):
        before_stats = self.buffer.stats()
        before_writes = tuple(self.file_manager.write_log)
        with patch.object(self.storage._codec, "encoded_size", return_value=1), \
             patch.object(self.storage._codec, "encode", return_value=b"xx"):
            self.assert_error(
                ROW_ENCODING_ERROR,
                lambda: self.storage.insert_row(self.table, (1, "Alice", 20)),
            )
        self.assertEqual(self.buffer.stats(), before_stats)
        self.assertEqual(tuple(self.file_manager.write_log), before_writes)

    def test_decode_failure_closes_scan_and_does_not_block_later_cleanup(self):
        self.storage.insert_row(self.table, (1, "Alice", 20))
        scan = self.storage.scan_rows(self.table)
        with patch.object(self.storage._codec, "decode", side_effect=ValueError("injected")):
            with self.assertRaisesRegex(ValueError, "injected"):
                next(scan)
        self.assertEqual(self.storage.active_scan_count, 0)

    def test_buffer_write_and_sync_failures_are_not_reported_as_success(self):
        write_failure = DbError(
            ErrorStage.STORAGE,
            IO_WRITE_FAILED,
            "injected write failure",
            context={"operation": "write_page"},
        )
        with patch.object(self.buffer, "write_page", side_effect=write_failure):
            with self.assertRaises(DbError) as raised:
                self.storage.insert_row(self.table, (1, "Alice", 20))
        self.assertIs(raised.exception, write_failure)

        before_sync = self.file_manager.sync_count
        sync_failure = DbError(
            ErrorStage.STORAGE,
            IO_SYNC_FAILED,
            "injected sync failure",
            context={"operation": "sync"},
        )
        with patch.object(self.file_manager, "sync", side_effect=sync_failure):
            with self.assertRaises(DbError) as raised:
                self.storage.sync()
        self.assertIs(raised.exception, sync_failure)
        self.assertEqual(self.file_manager.sync_count, before_sync)

    def test_page_allocation_failure_leaves_existing_chain_readable(self):
        full_row = (1, "x" * 4036, 20)
        self.storage.insert_row(self.table, full_row)
        with patch.object(self.buffer, "new_page", side_effect=OSError("allocate failed")):
            with self.assertRaisesRegex(OSError, "allocate failed"):
                self.storage.insert_row(self.table, (2, "Bob", 21))
        self.assertEqual(
            [record.values for record in self.storage.scan_rows(self.table)],
            [full_row],
        )

    def test_release_failure_happens_only_after_page_is_unlinked(self):
        first = self.storage.insert_row(self.table, (1, "A" * 3000, 20))
        second = self.storage.insert_row(self.table, (2, "B" * 3000, 21))
        self.storage.delete_row(self.table, second)

        with patch.object(self.buffer, "free_page", side_effect=OSError("free failed")):
            with self.assertRaisesRegex(OSError, "free failed"):
                self.storage.reclaim_empty_pages(self.table)

        # 项目不承诺 I/O 失败时回滚，但必须保证链不会继续指向准备释放的页。
        root = DataPage(self.buffer.get_page(2), page_id=2, expected_table_id=1)
        self.assertEqual(root.header.next_page_id, INVALID_PAGE_ID)
        self.assertEqual(
            list(self.storage.scan_rows(self.table)),
            [StoredRow(first, (1, "A" * 3000, 20))],
        )

    def test_root_read_failure_does_not_register_an_unusable_scan(self):
        with patch.object(self.buffer, "get_page", side_effect=OSError("read failed")):
            with self.assertRaisesRegex(OSError, "read failed"):
                self.storage.scan_rows(self.table)
        self.assertEqual(self.storage.active_scan_count, 0)

    def test_formal_filter_and_delete_run_on_page_backed_storage(self):
        self.storage.insert_row(self.table, (1, "Alice", 20))
        self.storage.insert_row(self.table, (2, "Bob", 17))
        location = span("DELETE FROM student WHERE age >= 18;")
        predicate = BoundBinary(
            ExprOp.GE,
            BoundColumn(2, DataType.INT, location),
            BoundLiteral(18, DataType.INT, location),
            DataType.BOOL,
            location,
            location,
        )
        filtered = FilterPlan(SeqScanPlan(self.table, location), predicate, location)

        result = Executor().execute(
            DeletePlan(self.table, filtered, location),
            ExecutionContext(object(), self.storage),
        )
        self.assertEqual(result.affected_rows, 1)
        self.assertEqual(
            [record.values for record in self.storage.scan_rows(self.table)],
            [(2, "Bob", 17)],
        )

    def test_sync_order_and_abort_without_flush(self):
        events = []
        flush = self.buffer.flush_all
        sync = self.file_manager.sync

        with patch.object(
            self.buffer,
            "flush_all",
            side_effect=lambda: (events.append("flush"), flush())[1],
        ), patch.object(
            self.file_manager,
            "sync",
            side_effect=lambda: (events.append("sync"), sync())[1],
        ):
            self.storage.sync()
        self.assertEqual(events, ["flush", "sync"])

        file_manager = InMemoryFileManager()
        buffer = InMemoryBufferPool(file_manager)
        storage = StorageEngine(buffer, RowCodec(), file_manager)
        storage.create_heap(1)
        scan_table = self.table
        scan = storage.scan_rows(scan_table)
        with patch.object(buffer, "flush_all", wraps=buffer.flush_all) as flush_all:
            storage.abort()
        flush_all.assert_not_called()
        self.assertTrue(file_manager.is_closed)
        self.assertEqual(storage.active_scan_count, 0)
        with self.assertRaises(StopIteration):
            next(scan)

    def test_catalog_and_executor_use_the_page_backed_storage(self):
        """验证正式建表、目录登记、插入和查询确实经过 DataPage。"""
        file_manager = InMemoryFileManager()
        buffer = InMemoryBufferPool(file_manager, capacity=2)
        storage = StorageEngine(buffer, RowCodec(), file_manager)
        self.addCleanup(storage.abort)
        catalog = CatalogManager.bootstrap_or_load(storage, True)
        context = ExecutionContext(catalog, storage)
        executor = Executor()
        location = span("CREATE TABLE student(id INT, name VARCHAR, age INT);")

        created = executor.execute(
            CreateTablePlan("student", STUDENT_SCHEMA, location), context
        )
        table = catalog.find_table("student")
        inserted = executor.execute(
            InsertPlan(table, (1, "Alice", 20), location), context
        )
        selected = executor.execute(
            ProjectPlan(
                SeqScanPlan(table, location),
                (1, 0),
                (
                    ResultColumn("name", DataType.VARCHAR),
                    ResultColumn("id", DataType.INT),
                ),
                location,
            ),
            context,
        )

        self.assertEqual(created.affected_rows, 0)
        self.assertEqual(inserted.affected_rows, 1)
        self.assertEqual(selected.rows, [("Alice", 1)])
        # 从系统目录页重新装载，证明目录记录不是只保存在 Catalog 的内存对象中。
        restored = CatalogManager.bootstrap_or_load(storage, False)
        self.assertEqual(restored.find_table("student"), table)


class RealPageStoragePersistenceTests(unittest.TestCase):
    """StorageEngine 与正式 BufferPool/FileManager 的关闭重开验证。"""

    def test_reclaimed_pages_are_reused_without_stale_rows_after_reopen(self):
        cases = (("lru", 1), ("fifo", 2))
        for policy, capacity in cases:
            with self.subTest(policy=policy, capacity=capacity), \
                 tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "reclaim-reuse.db"
                rows = tuple(
                    (index, chr(64 + index) * 3000, 20 + index)
                    for index in range(1, 4)
                )

                file_manager = FileManager.open(str(path))
                buffer = BufferPool(
                    file_manager,
                    capacity=capacity,
                    policy=policy,
                )
                storage = StorageEngine(buffer, RowCodec(), file_manager)
                catalog = CatalogManager.bootstrap_or_load(storage, True)
                context = ExecutionContext(catalog, storage)
                executor = Executor()
                location = span("CREATE TABLE student(id INT, name VARCHAR, age INT);")
                executor.execute(
                    CreateTablePlan("student", STUDENT_SCHEMA, location),
                    context,
                )
                student = catalog.find_table("student")
                row_ids = tuple(storage.insert_row(student, row) for row in rows)
                self.assertEqual([row_id.page_id for row_id in row_ids], [2, 3, 4])

                # 两张非根页变空后会依次进入空闲链；最后释放的 page 4
                # 应成为下一张表的根页，而不是继续增长数据库文件。
                self.assertTrue(storage.delete_row(student, row_ids[1]))
                self.assertTrue(storage.delete_row(student, row_ids[2]))
                self.assertEqual(storage.reclaim_empty_pages(student), 2)
                executor.execute(
                    CreateTablePlan("replacement", STUDENT_SCHEMA, location),
                    context,
                )
                replacement = catalog.find_table("replacement")
                self.assertEqual(replacement.ref.root_page_id, row_ids[2].page_id)
                fresh_row = (99, "fresh", 30)
                storage.insert_row(replacement, fresh_row)
                storage.close()

                reopened_file = FileManager.open(str(path))
                reopened_buffer = BufferPool(
                    reopened_file,
                    capacity=capacity,
                    policy=policy,
                )
                reopened_storage = StorageEngine(
                    reopened_buffer,
                    RowCodec(),
                    reopened_file,
                )
                try:
                    restored = CatalogManager.bootstrap_or_load(
                        reopened_storage,
                        False,
                    )
                    restored_student = restored.find_table("student")
                    restored_replacement = restored.find_table("replacement")
                    self.assertEqual(
                        [record.values for record in reopened_storage.scan_rows(
                            restored_student
                        )],
                        [rows[0]],
                    )
                    self.assertEqual(
                        [record.values for record in reopened_storage.scan_rows(
                            restored_replacement
                        )],
                        [fresh_row],
                    )
                    self.assertEqual(
                        restored_replacement.ref.root_page_id,
                        row_ids[2].page_id,
                    )
                finally:
                    reopened_storage.close()

    def test_catalog_spanning_pages_is_fully_restored(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "catalog-pages.db"
            columns = tuple(
                ColumnDef(
                    f"column_{index:02d}_{'x' * 45}",
                    DataType.INT if index % 2 == 0 else DataType.VARCHAR,
                )
                for index in range(64)
            )
            wide_schema = Schema(columns)

            file_manager = FileManager.open(str(path))
            buffer = BufferPool(file_manager, capacity=2, policy="fifo")
            storage = StorageEngine(buffer, RowCodec(), file_manager)
            catalog = CatalogManager.bootstrap_or_load(storage, True)
            location = span("CREATE TABLE wide_table(column_00_x INT);")
            Executor().execute(
                CreateTablePlan("wide_table", wide_schema, location),
                ExecutionContext(catalog, storage),
            )
            table = catalog.find_table("wide_table")
            catalog_records = list(storage.scan_rows(SYSTEM_CATALOG_TABLE))
            self.assertEqual(len(catalog_records), len(columns))
            self.assertGreater(
                len({record.row_id.page_id for record in catalog_records}),
                1,
            )
            storage.close()

            reopened_file = FileManager.open(str(path))
            reopened_buffer = BufferPool(
                reopened_file,
                capacity=2,
                policy="fifo",
            )
            reopened_storage = StorageEngine(
                reopened_buffer,
                RowCodec(),
                reopened_file,
            )
            try:
                restored = CatalogManager.bootstrap_or_load(reopened_storage, False)
                self.assertEqual(restored.find_table("wide_table"), table)
                restored_records = list(
                    reopened_storage.scan_rows(SYSTEM_CATALOG_TABLE)
                )
                self.assertEqual(len(restored_records), len(columns))
                self.assertGreater(
                    len({record.row_id.page_id for record in restored_records}),
                    1,
                )
            finally:
                reopened_storage.close()

    def test_capacity_one_cross_page_rows_survive_reopen(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "storage-engine.db"
            rows = tuple(
                (index, chr(64 + index) * 3000, 20 + index)
                for index in range(1, 4)
            )

            file_manager = FileManager.open(str(path))
            buffer = BufferPool(file_manager, capacity=1, policy="lru")
            storage = StorageEngine(buffer, RowCodec(), file_manager)
            catalog = CatalogManager.bootstrap_or_load(storage, True)
            context = ExecutionContext(catalog, storage)
            executor = Executor()
            location = span("CREATE TABLE student(id INT, name VARCHAR, age INT);")
            executor.execute(
                CreateTablePlan("student", STUDENT_SCHEMA, location),
                context,
            )
            table = catalog.find_table("student")
            for row in rows:
                executor.execute(InsertPlan(table, row, location), context)
            storage.close()

            reopened_file = FileManager.open(str(path))
            reopened_buffer = BufferPool(reopened_file, capacity=1, policy="lru")
            reopened_storage = StorageEngine(
                reopened_buffer,
                RowCodec(),
                reopened_file,
            )
            try:
                restored = CatalogManager.bootstrap_or_load(reopened_storage, False)
                restored_table = restored.find_table("student")
                self.assertEqual(restored_table, table)
                self.assertEqual(
                    [record.values for record in reopened_storage.scan_rows(restored_table)],
                    list(rows),
                )
                self.assertGreater(reopened_buffer.stats().misses, 1)
            finally:
                reopened_storage.close()


if __name__ == "__main__":
    unittest.main()
