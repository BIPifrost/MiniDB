"""正式页式 StorageEngine 与临时内存 BufferPool 的对接测试。"""

import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from minidb.catalog.catalog import SYSTEM_CATALOG_TABLE, SYSTEM_INDEXES_TABLE
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
    STALE_ROW,
    DbError,
    ErrorStage,
)
from minidb.core.expressions import ExprOp
from minidb.core.records import (
    RowId,
    RowMovement,
    RowScan,
    StoredRow,
    _issue_validated_write_token,
)
from minidb.core.result import ResultColumn
from minidb.core.schema import ColumnDef, DataType, Schema
from minidb.core.transaction import TransactionGuard, TransactionState
from minidb.engine.context import ExecutionContext
from minidb.engine.executor import Executor
from minidb.cli.session import Session
from minidb.storage.data_page import DataPage
from minidb.storage.buffer_pool import BufferPool
from minidb.storage.file_manager import FileManager
from minidb.storage.row_codec import RowCodec
from minidb.storage.storage_engine import StorageEngine
from tests.fakes.in_memory_buffer_pool import InMemoryBufferPool, InMemoryFileManager
from tests.fixtures.contracts import STUDENT_SCHEMA, STUDENT_TABLE, span


class PageStorageEngineTests(unittest.TestCase):
    def setUp(self):
        self.guard = TransactionGuard(TransactionState.BOOTSTRAP)
        self.file_manager = InMemoryFileManager(guard=self.guard)
        self.buffer = InMemoryBufferPool(self.file_manager, capacity=2)
        self.storage = StorageEngine(
            self.buffer, RowCodec(), self.file_manager, self.guard
        )
        self.authorized_tokens = []
        self.session_id = uuid4()
        self.storage.bind_write_authorizer(
            session_id=self.session_id,
            catalog_generation=lambda: 0,
            token_is_authorized=lambda candidate: any(
                candidate is token for token in self.authorized_tokens
            ),
        )
        self.storage.initialize_reserved_heap(SYSTEM_CATALOG_TABLE)
        self.storage.initialize_reserved_heap(SYSTEM_INDEXES_TABLE)
        self.guard.transition_to(TransactionState.IDLE)
        self.guard.transition_to(TransactionState.PREPARING)
        self.guard.transition_to(TransactionState.ACTIVE)
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

    def _with_token(self, action):
        token = _issue_validated_write_token(self.session_id, 0, uuid4())
        self.authorized_tokens.append(token)
        try:
            return action(token)
        finally:
            self.authorized_tokens.remove(token)

    def insert(self, row, table=None):
        target = self.table if table is None else table
        movement = self._with_token(
            lambda token: self.storage.insert_row(target, row, token)
        )
        self.assertIsInstance(movement, RowMovement)
        return movement.new.row_id

    def delete(self, row_id, table=None):
        target = self.table if table is None else table
        expected = self.storage.fetch_row(target, row_id)
        movements = self._with_token(
            lambda token: self.storage.delete_rows(target, (expected,), token)
        )
        return bool(movements)

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
        guard = TransactionGuard(TransactionState.BOOTSTRAP)
        file_manager = InMemoryFileManager(guard=guard)
        buffer = InMemoryBufferPool(file_manager)
        storage = StorageEngine(buffer, RowCodec(), file_manager, guard)
        self.addCleanup(storage.abort)

        storage.initialize_reserved_heap(SYSTEM_CATALOG_TABLE)
        storage.validate_table_root(SYSTEM_CATALOG_TABLE)
        self.assert_error(
            PAGE_CORRUPTED,
            lambda: storage.initialize_reserved_heap(SYSTEM_CATALOG_TABLE),
        )

    def test_single_page_insert_scan_delete_and_repeat_delete(self):
        row = (1, "Alice", 20)
        row_id = self.insert(row)

        scan = self.storage.scan_rows(self.table)
        self.assertIsInstance(scan, RowScan)
        self.assertEqual(list(scan), [StoredRow(row_id, row)])
        self.assertEqual(self.storage.active_scan_count, 0)

        self.assertTrue(self.delete(row_id))
        self.assert_error(STALE_ROW, lambda: self.delete(row_id))
        self.assertEqual(list(self.storage.scan_rows(self.table)), [])

    def test_empty_scan_requests_the_root_page_only_once(self):
        """创建扫描时已经校验根页，迭代不能再虚增一次缓存命中。"""
        before = self.buffer.stats().requests
        scan = self.storage.scan_rows(self.table)
        self.assertEqual(self.buffer.stats().requests - before, 1)
        self.assertEqual(list(scan), [])
        self.assertEqual(self.buffer.stats().requests - before, 1)

    def test_large_rows_span_pages_and_scan_in_chain_order(self):
        rows = tuple(
            (index, character * 1000, 20 + index)
            for index, character in enumerate(("甲", "乙", "丙"), 1)
        )
        row_ids = tuple(self.insert(row) for row in rows)

        self.assertEqual([row_id.page_id for row_id in row_ids], [3, 4, 5])
        self.assertEqual(list(self.storage.scan_rows(self.table)), [
            StoredRow(row_id, row) for row_id, row in zip(row_ids, rows)
        ])
        root = DataPage(self.buffer.get_page(3), page_id=3, expected_table_id=1)
        middle = DataPage(self.buffer.get_page(4), page_id=4, expected_table_id=1)
        self.assertEqual(root.header.next_page_id, 4)
        self.assertEqual(middle.header.next_page_id, 5)

    def test_active_scan_blocks_all_mutations_and_normal_close(self):
        row_id = self.insert((1, "Alice", 20))
        scan = self.storage.scan_rows(self.table)

        for operation in (
            lambda: self.storage.create_heap(2),
            lambda: self.insert((2, "Bob", 17)),
            lambda: self.delete(row_id),
            lambda: self.storage.reclaim_empty_pages(self.table),
            self.storage.close,
        ):
            with self.subTest(operation=operation):
                self.assert_error(ACTIVE_SCAN, operation)
        scan.close()
        scan.close()
        self.assertEqual(self.storage.active_scan_count, 0)

    def test_scan_error_closes_itself_and_reports_page_cycle(self):
        self.insert((1, "甲" * 1000, 20))
        self.insert((2, "乙" * 1000, 21))
        # 根页仍合法，让 scan_rows 能成功返回扫描器；第二页自指后，
        # 错误会发生在迭代过程中，正好验证异常路径会自动 close。
        raw = bytearray(self.buffer.get_page(4))
        struct.pack_into("<I", raw, 12, 4)
        self.buffer.write_page(4, bytes(raw))

        scan = self.storage.scan_rows(self.table)
        self.assert_error(PAGE_CORRUPTED, lambda: list(scan))
        self.assertEqual(self.storage.active_scan_count, 0)

    def test_reclaim_unlinks_consecutive_empty_pages_and_keeps_root(self):
        rows = tuple(
            (index, character * 1000, 20)
            for index, character in enumerate(("甲", "乙", "丙"), 1)
        )
        first, second, third = (
            self.insert(row) for row in rows
        )
        self.delete(second)
        self.delete(third)

        # v2 delete_rows 在同一次受控写入中已经回收空的非根页。
        self.assertEqual(self.storage.reclaim_empty_pages(self.table), 0)
        root = DataPage(self.buffer.get_page(3), page_id=3, expected_table_id=1)
        self.assertEqual(root.header.next_page_id, INVALID_PAGE_ID)
        self.assertEqual(list(self.storage.scan_rows(self.table)), [StoredRow(first, rows[0])])
        with self.assertRaises(KeyError):
            self.buffer.get_page(4)
        with self.assertRaises(KeyError):
            self.buffer.get_page(5)

    def test_empty_root_is_reset_without_losing_nonempty_successor(self):
        first_row = (1, "甲" * 1000, 20)
        second_row = (2, "乙" * 1000, 21)
        first = self.insert(first_row)
        second = self.insert(second_row)
        self.delete(first)

        self.assertEqual(self.storage.reclaim_empty_pages(self.table), 0)
        root = DataPage(self.buffer.get_page(3), page_id=3, expected_table_id=1)
        self.assertEqual(root.header.slot_count, 0)
        self.assertEqual(root.header.next_page_id, 4)
        self.assertEqual(list(self.storage.scan_rows(self.table)), [StoredRow(second, second_row)])

    def test_row_id_must_refer_to_a_page_in_the_target_heap(self):
        self.insert((1, "Alice", 20))
        self.assert_error(
            INVALID_ARGUMENT,
            lambda: self.storage.fetch_row(self.table, RowId(99, 0)),
        )

    def test_codec_contract_failure_happens_before_page_access(self):
        before_stats = self.buffer.stats()
        before_writes = tuple(self.file_manager.write_log)
        with patch.object(self.storage._codec, "encoded_size", return_value=1), \
             patch.object(self.storage._codec, "encode", return_value=b"xx"):
            self.assert_error(
                ROW_ENCODING_ERROR,
                lambda: self.insert((1, "Alice", 20)),
            )
        self.assertEqual(self.buffer.stats(), before_stats)
        self.assertEqual(tuple(self.file_manager.write_log), before_writes)

    def test_decode_failure_closes_scan_and_does_not_block_later_cleanup(self):
        self.insert((1, "Alice", 20))
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
        with patch.object(self.buffer, "write_if_current", side_effect=write_failure):
            with self.assertRaises(DbError) as raised:
                self.insert((1, "Alice", 20))
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
                self.guard.transition_to(TransactionState.COMMITTING)
                self.storage.flush_for_commit()
        self.assertIs(raised.exception, sync_failure)
        self.assertEqual(self.file_manager.sync_count, before_sync)

    def test_page_allocation_failure_leaves_existing_chain_readable(self):
        full_row = (1, "甲" * 1024, 20)
        self.insert(full_row)
        with patch.object(self.buffer, "new_page", side_effect=OSError("allocate failed")):
            with self.assertRaisesRegex(OSError, "allocate failed"):
                self.insert((2, "乙" * 1024, 21))
        self.assertEqual(
            [record.values for record in self.storage.scan_rows(self.table)],
            [full_row],
        )

    def test_release_failure_happens_only_after_page_is_unlinked(self):
        first = self.insert((1, "甲" * 1000, 20))
        second = self.insert((2, "乙" * 1000, 21))
        with patch.object(self.buffer, "free_page", side_effect=OSError("free failed")):
            with self.assertRaisesRegex(OSError, "free failed"):
                self.delete(second)

        # 项目不承诺 I/O 失败时回滚，但必须保证链不会继续指向准备释放的页。
        root = DataPage(self.buffer.get_page(3), page_id=3, expected_table_id=1)
        self.assertEqual(root.header.next_page_id, INVALID_PAGE_ID)
        self.assertEqual(
            list(self.storage.scan_rows(self.table)),
            [StoredRow(first, (1, "甲" * 1000, 20))],
        )

    def test_root_read_failure_does_not_register_an_unusable_scan(self):
        with patch.object(self.buffer, "get_page", side_effect=OSError("read failed")):
            with self.assertRaisesRegex(OSError, "read failed"):
                self.storage.scan_rows(self.table)
        self.assertEqual(self.storage.active_scan_count, 0)

    def test_formal_filter_and_delete_run_on_page_backed_storage(self):
        self.insert((1, "Alice", 20))
        self.insert((2, "Bob", 17))
        location = span("DELETE FROM student WHERE age >= 18;")
        predicate = BoundBinary(
            ExprOp.GE,
            BoundColumn(2, DataType.INT, location, True),
            BoundLiteral(18, DataType.INT, location),
            DataType.BOOL,
            location,
            location,
            True,
        )
        filtered = FilterPlan(SeqScanPlan(self.table, location), predicate, location)

        stream = Executor()._execute_stream(
            filtered, ExecutionContext(object(), self.storage)
        )
        try:
            expected = tuple(
                StoredRow(record.row_id, record.values) for record in stream
            )
        finally:
            stream.close()
        movements = self._with_token(
            lambda token: self.storage.delete_rows(self.table, expected, token)
        )
        self.assertEqual(len(movements), 1)
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
            self.guard.transition_to(TransactionState.COMMITTING)
            self.storage.flush_for_commit()
        self.assertEqual(events, ["flush", "sync"])

        scan = self.storage.scan_rows(self.table)
        with patch.object(
            self.buffer, "flush_all", wraps=self.buffer.flush_all
        ) as flush_all:
            self.storage.abort()
        flush_all.assert_not_called()
        self.assertTrue(self.file_manager.is_closed)
        self.assertEqual(self.storage.active_scan_count, 0)
        with self.assertRaises(StopIteration):
            next(scan)

    def test_catalog_and_executor_use_the_page_backed_storage(self):
        """验证正式建表、目录登记、插入和查询确实经过 DataPage。"""
        with tempfile.TemporaryDirectory() as directory:
            session = Session.open(
                str(Path(directory) / "page-backed.db"), buffer_pages=2
            )
            try:
                created = session.execute_text(
                    "CREATE TABLE student(id INT, name VARCHAR, age INT);"
                )[0]
                inserted = session.execute_text(
                    "INSERT INTO student(id, name, age) VALUES (1, 'Alice', 20);"
                )[0]
                selected = session.execute_text(
                    "SELECT name, id FROM student;", materialize=True
                )[0]
                table = session.catalog.find_table("student")
                restored = CatalogManager.bootstrap_or_load(
                    session.storage, False
                )

                self.assertEqual(created.affected_rows, 0)
                self.assertEqual(inserted.affected_rows, 1)
                self.assertEqual(selected.rows, [("Alice", 1)])
                self.assertEqual(restored.find_table("student"), table)
            finally:
                session.close()


class RealPageStoragePersistenceTests(unittest.TestCase):
    """通过正式 Session 验证页存储关闭重开，不再手工拼装 v1 组件。"""

    def test_reclaimed_pages_are_reused_without_stale_rows_after_reopen(self):
        cases = (("lru", 1), ("fifo", 2))
        for policy, capacity in cases:
            with self.subTest(policy=policy, capacity=capacity), \
                 tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "reclaim-reuse.db"
                rows = tuple(
                    (index, character * 1000, 20 + index)
                    for index, character in enumerate(("甲", "乙", "丙"), 1)
                )
                session = Session.open(
                    str(path), buffer_pages=capacity, policy=policy
                )
                try:
                    session.execute_text(
                        "CREATE TABLE student(id INT, name VARCHAR, age INT);"
                    )
                    for row in rows:
                        session.execute_text(
                            "INSERT INTO student(id, name, age) VALUES "
                            f"({row[0]}, '{row[1]}', {row[2]});"
                        )
                    student = session.catalog.find_table("student")
                    row_ids = tuple(
                        record.row_id for record in session.storage.scan_rows(student)
                    )
                    self.assertEqual(
                        [row_id.page_id for row_id in row_ids], [3, 4, 5]
                    )

                    deleted = session.execute_text(
                        "DELETE FROM student WHERE id >= 2;"
                    )[0]
                    self.assertEqual(deleted.affected_rows, 2)
                    session.execute_text(
                        "CREATE TABLE replacement(id INT, name VARCHAR, age INT);"
                    )
                    replacement = session.catalog.find_table("replacement")
                    self.assertEqual(
                        replacement.ref.root_page_id,
                        row_ids[2].page_id,
                    )
                    session.execute_text(
                        "INSERT INTO replacement(id, name, age) "
                        "VALUES (99, 'fresh', 30);"
                    )
                finally:
                    session.close()

                reopened = Session.open(
                    str(path), buffer_pages=capacity, policy=policy
                )
                try:
                    restored_student = reopened.catalog.find_table("student")
                    restored_replacement = reopened.catalog.find_table("replacement")
                    self.assertEqual(
                        [record.values for record in reopened.storage.scan_rows(
                            restored_student
                        )],
                        [rows[0]],
                    )
                    self.assertEqual(
                        [record.values for record in reopened.storage.scan_rows(
                            restored_replacement
                        )],
                        [(99, "fresh", 30)],
                    )
                    self.assertEqual(
                        restored_replacement.ref.root_page_id,
                        row_ids[2].page_id,
                    )
                finally:
                    reopened.close()

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

            declarations = ", ".join(
                f"{column.name} {column.data_type.value}"
                for column in columns
            )
            session = Session.open(str(path), buffer_pages=2, policy="fifo")
            try:
                session.execute_text(
                    f"CREATE TABLE wide_table({declarations});"
                )
                table = session.catalog.find_table("wide_table")
                catalog_records = list(
                    session.storage.scan_rows(SYSTEM_CATALOG_TABLE)
                )
                self.assertEqual(len(catalog_records), len(columns))
                self.assertGreater(
                    len({record.row_id.page_id for record in catalog_records}),
                    1,
                )
            finally:
                session.close()

            reopened = Session.open(str(path), buffer_pages=2, policy="fifo")
            try:
                self.assertEqual(reopened.catalog.find_table("wide_table"), table)
                restored_records = list(
                    reopened.storage.scan_rows(SYSTEM_CATALOG_TABLE)
                )
                self.assertEqual(len(restored_records), len(columns))
                self.assertGreater(
                    len({record.row_id.page_id for record in restored_records}), 1
                )
            finally:
                reopened.close()

    def test_capacity_one_cross_page_rows_survive_reopen(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "storage-engine.db"
            rows = tuple(
                (index, character * 1000, 20 + index)
                for index, character in enumerate(("甲", "乙", "丙"), 1)
            )
            session = Session.open(str(path), buffer_pages=1, policy="lru")
            try:
                session.execute_text(
                    "CREATE TABLE student(id INT, name VARCHAR, age INT);"
                )
                for row in rows:
                    session.execute_text(
                        "INSERT INTO student(id, name, age) VALUES "
                        f"({row[0]}, '{row[1]}', {row[2]});"
                    )
                table = session.catalog.find_table("student")
            finally:
                session.close()

            reopened = Session.open(str(path), buffer_pages=1, policy="lru")
            try:
                restored_table = reopened.catalog.find_table("student")
                self.assertEqual(restored_table, table)
                self.assertEqual(
                    [record.values for record in reopened.storage.scan_rows(restored_table)],
                    list(rows),
                )
                self.assertGreater(reopened.buffer_pool.stats().misses, 1)
            finally:
                reopened.close()


if __name__ == "__main__":
    unittest.main()
