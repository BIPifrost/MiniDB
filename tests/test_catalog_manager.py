"""张振的v2目录调度回归；Mock只提供调用结果，不实现队友的存储或事务。"""
import json
import unittest
from unittest.mock import MagicMock, Mock, call

from tests.fixtures.contracts import STUDENT_CATALOG_ROWS, STUDENT_SCHEMA, STUDENT_TABLE
from tests.fakes.v2_catalog_contracts import SizeCodec
from minidb.catalog.catalog import SYSTEM_CATALOG_TABLE, SYSTEM_INDEXES_TABLE, Catalog
from minidb.catalog.catalog_manager import CatalogManager, CatalogServices
from minidb.core.errors import DbError, ErrorStage
from minidb.core.records import RowId, StoredRow
from minidb.core.schema import ColumnDef, DataType, Schema, TableDef, TableRef
from minidb.core.transaction import TransactionGuard, TransactionState


class CatalogManagerTests(unittest.TestCase):
    def setUp(self):
        self.storage = Mock(spec_set=["initialize_reserved_heap", "validate_table_root",
                                      "scan_rows", "catalog_services"])
        self.scan = MagicMock(spec_set=["__iter__", "close"])
        self.index_scan = MagicMock(spec_set=["__iter__", "close"])
        self.scan.__iter__.side_effect = lambda: iter(
            StoredRow(RowId(1, i, 1), row) for i, row in enumerate(reversed(STUDENT_CATALOG_ROWS)))
        self.index_scan.__iter__.side_effect = lambda: iter(())
        self.storage.scan_rows.side_effect = lambda table: (
            self.scan if table.ref.table_id == 0 else self.index_scan)
        self.guard = TransactionGuard(TransactionState.IDLE)
        self.codec = SizeCodec()
        self.write = Mock()
        self.storage.catalog_services = CatalogServices(self.guard, self.codec, lambda: 2,
                                                        self.write, Mock())

    def active(self):
        """仅推进测试的正式guard；生产事务转换仍由TransactionManager负责。"""
        self.guard.transition_to(TransactionState.PREPARING)
        self.guard.transition_to(TransactionState.ACTIVE)

    def test_new_database_initializes_both_reserved_heaps(self):
        self.storage.catalog_services = CatalogServices(
            TransactionGuard(TransactionState.BOOTSTRAP), self.codec, lambda: 2, self.write, Mock())
        manager = CatalogManager.bootstrap_or_load(self.storage, True)
        self.assertEqual(self.storage.mock_calls, [
            call.initialize_reserved_heap(SYSTEM_CATALOG_TABLE), call.validate_table_root(SYSTEM_CATALOG_TABLE),
            call.initialize_reserved_heap(SYSTEM_INDEXES_TABLE), call.validate_table_root(SYSTEM_INDEXES_TABLE)])
        self.assertEqual(manager.list_tables(), [])
        self.assertEqual(manager.generation, 0)
        self.write.assert_not_called()

    def test_existing_database_loads_closes_then_validates_user_roots(self):
        events = []
        self.storage.validate_table_root.side_effect = lambda table: events.append(table.ref.name)
        self.scan.close.side_effect = lambda: events.append("close_table")
        self.index_scan.close.side_effect = lambda: events.append("close_index")
        manager = CatalogManager.bootstrap_or_load(self.storage, False)
        self.assertEqual(events, ["_sys_catalog", "_sys_indexes", "close_table", "close_index", "student"])
        self.assertEqual(manager.find_table("STUDENT"), STUDENT_TABLE)
        self.active()
        self.assertEqual(manager.reserve_table_id(), 2)
        self.storage.initialize_reserved_heap.assert_not_called()

    def test_broken_rows_close_scan_and_do_not_validate_users(self):
        self.scan.__iter__.side_effect = lambda: iter(
            StoredRow(RowId(1, i, 1), row) for i, row in enumerate(STUDENT_CATALOG_ROWS[:-1]))
        with self.assertRaises(DbError) as caught:
            CatalogManager.bootstrap_or_load(self.storage, False)
        self.assertEqual(caught.exception.code, "CATALOG_CORRUPTED")
        self.assertIn("缺列", caught.exception.context["reason"])
        self.assertNotIn("row_id", caught.exception.context)
        self.scan.close.assert_called_once_with()
        self.assertEqual(self.storage.validate_table_root.call_args_list,
                         [call(SYSTEM_CATALOG_TABLE), call(SYSTEM_INDEXES_TABLE)])

    def test_corrupt_record_reports_physical_position(self):
        first = STUDENT_CATALOG_ROWS[0]
        for bad in (("bad_id",) + first[1:], first):
            with self.subTest(row=bad):
                self.scan.reset_mock()
                records = (StoredRow(RowId(1, 0, 1), first), StoredRow(RowId(8, 5, 2), bad))
                self.scan.__iter__.side_effect = lambda: iter(records)
                with self.assertRaises(DbError) as caught:
                    CatalogManager.bootstrap_or_load(self.storage, False)
                self.assertEqual(caught.exception.code, "CATALOG_CORRUPTED")
                self.assertEqual(caught.exception.context["row_id"], {"page_id": 8, "slot_id": 5, "generation": 2})
                self.assertEqual(caught.exception.context["table_name"], "_sys_catalog")
                json.dumps(dict(caught.exception.context), ensure_ascii=False)
                self.scan.close.assert_called_once_with()
                self.write.assert_not_called()

    def test_corruption_position_survives_scan_close_failure(self):
        row = ("bad_id",) + STUDENT_CATALOG_ROWS[0][1:]
        self.scan.__iter__.side_effect = lambda: iter((StoredRow(RowId(9, 2, 1), row),))
        self.scan.close.side_effect = DbError(ErrorStage.STORAGE, "IO_CLOSE_FAILED", "关闭失败")
        with self.assertRaises(DbError) as caught:
            CatalogManager.bootstrap_or_load(self.storage, False)
        error = caught.exception
        self.assertEqual(error.code, "CATALOG_CORRUPTED")
        self.assertEqual(error.context["row_id"], {"page_id": 9, "slot_id": 2, "generation": 1})
        self.assertEqual(error.context["cleanup_errors"][0]["code"], "IO_CLOSE_FAILED")
        self.scan.close.assert_called_once_with()

    def test_index_corruption_keeps_index_position_and_closes_both_scans(self):
        """索引目录同样保留坏行首错，不能误标成上一张表目录的位置。"""
        row = (1, "ix_age", 1, 2, 4, "not_bool", "USER")
        self.index_scan.__iter__.side_effect = lambda: iter((StoredRow(RowId(2, 4, 3), row),))
        self.index_scan.close.side_effect = OSError("索引扫描关闭失败")
        manager = CatalogManager(self.storage, Catalog((STUDENT_TABLE,)))
        original = manager._catalog
        with self.assertRaises(DbError) as caught:
            manager.reload_from_storage()
        self.assertEqual(caught.exception.code, "CATALOG_CORRUPTED")
        self.assertEqual(caught.exception.context["table_name"], "_sys_indexes")
        self.assertEqual(caught.exception.context["row_id"], {"page_id": 2, "slot_id": 4, "generation": 3})
        self.assertEqual(caught.exception.context["cleanup_errors"][0]["message"], "索引扫描关闭失败")
        self.assertIs(manager._catalog, original)
        self.assertEqual(manager.generation, 0)
        self.scan.close.assert_called_once_with()
        self.index_scan.close.assert_called_once_with()

    def test_read_failure_keeps_first_error_when_close_also_fails(self):
        original = OSError("读取失败")
        def broken():
            yield StoredRow(RowId(1, 0, 1), STUDENT_CATALOG_ROWS[0])
            raise original
        self.scan.__iter__.side_effect = broken
        self.scan.close.side_effect = OSError("关闭失败")
        with self.assertRaises(OSError) as caught:
            CatalogManager.bootstrap_or_load(self.storage, False)
        self.assertIs(caught.exception, original)
        self.assertIn("关闭失败", original.__notes__[0])
        self.scan.close.assert_called_once_with()

    def test_close_failure_prevents_returning_a_loaded_manager(self):
        self.scan.close.side_effect = OSError("close failed")
        with self.assertRaises(OSError):
            CatalogManager.bootstrap_or_load(self.storage, False)
        self.assertEqual(self.storage.validate_table_root.call_args_list,
                         [call(SYSTEM_CATALOG_TABLE), call(SYSTEM_INDEXES_TABLE)])

    def test_user_root_failure_is_propagated_after_scan_closed(self):
        original = OSError("用户根页校验失败")
        self.storage.validate_table_root.side_effect = [None, None, original]
        with self.assertRaises(OSError) as caught:
            CatalogManager.bootstrap_or_load(self.storage, False)
        self.assertIs(caught.exception, original)
        self.scan.close.assert_called_once_with()
        self.index_scan.close.assert_called_once_with()

    def test_user_root_db_error_keeps_user_table_name(self):
        original = DbError(ErrorStage.STORAGE, "IO_READ_FAILED", "用户表根页读取失败",
                           context={"operation": "read_page", "table_name": "student", "table_id": 1})
        self.storage.validate_table_root.side_effect = [None, None, original]
        with self.assertRaises(DbError) as caught:
            CatalogManager.bootstrap_or_load(self.storage, False)
        self.assertIs(caught.exception, original)
        self.assertEqual(original.context["table_name"], "student")
        self.scan.close.assert_called_once_with()

    def test_shared_storage_errors_keep_identity_and_cleanup_details(self):
        original = DbError(ErrorStage.STORAGE, "IO_READ_FAILED", "读取失败",
                           context={"operation": "read_page", "page_id": 1})
        self.scan.__iter__.side_effect = original
        self.scan.close.side_effect = DbError(ErrorStage.STORAGE, "IO_CLOSE_FAILED", "关闭失败")
        with self.assertRaises(DbError) as caught:
            CatalogManager.bootstrap_or_load(self.storage, False)
        self.assertIs(caught.exception, original)
        self.assertEqual(original.context["operation"], "read_page")
        self.assertEqual(original.context["table_name"], "_sys_catalog")
        self.assertNotIn("row_id", original.context)
        self.assertEqual(original.context["cleanup_errors"][0]["code"], "IO_CLOSE_FAILED")
        self.scan.close.assert_called_once_with()

    def test_register_publishes_only_after_batch_write(self):
        self.active()
        manager = CatalogManager(self.storage, Catalog())
        old = manager._catalog
        def write(table, rows):
            self.assertIsNone(manager.find_table("student"))
            self.assertEqual(table, SYSTEM_CATALOG_TABLE)
            self.assertEqual(rows, STUDENT_CATALOG_ROWS)
            self.assertEqual(len(self.codec.calls), 3)
        self.write.side_effect = write
        manager.persist_and_register(STUDENT_TABLE)
        self.write.assert_called_once_with(SYSTEM_CATALOG_TABLE, STUDENT_CATALOG_ROWS)
        self.assertEqual(manager.list_tables(), [STUDENT_TABLE])
        self.assertEqual(old.list_tables(), [])
        self.assertEqual(manager.reserve_table_id(), 2)

    def test_preflight_failure_makes_no_writes(self):
        self.active()
        manager = CatalogManager(self.storage, Catalog())
        self.codec.fixed = 4057
        with self.assertRaises(DbError) as caught:
            manager.persist_and_register(STUDENT_TABLE)
        self.assertEqual(caught.exception.code, "ROW_TOO_LARGE")
        self.write.assert_not_called()
        self.storage.validate_table_root.assert_not_called()
        self.assertEqual(manager.list_tables(), [])

    def test_partial_write_failure_keeps_old_snapshot(self):
        self.active()
        manager = CatalogManager(self.storage, Catalog())
        failure = OSError("目录批量写中途失败")
        self.write.side_effect = failure
        with self.assertRaises(OSError) as caught:
            manager.persist_and_register(STUDENT_TABLE)
        self.assertIs(caught.exception, failure)
        self.write.assert_called_once_with(SYSTEM_CATALOG_TABLE, STUDENT_CATALOG_ROWS)
        self.assertEqual(manager.list_tables(), [])

    def test_duplicate_names_ids_and_roots_fail_before_storage(self):
        self.active()
        manager = CatalogManager(self.storage, Catalog((STUDENT_TABLE,)))
        cases = ((STUDENT_TABLE, "TABLE_EXISTS"),
                 (TableDef(TableRef(1, "course", 4), STUDENT_SCHEMA), "INVALID_ARGUMENT"),
                 (TableDef(TableRef(2, "course", 3), STUDENT_SCHEMA), "INVALID_ARGUMENT"),
                 (SYSTEM_CATALOG_TABLE, "INVALID_ARGUMENT"), (None, "INVALID_ARGUMENT"))
        for table, code in cases:
            with self.subTest(code=code, table=table):
                with self.assertRaises(DbError) as caught:
                    manager.persist_and_register(table)
                self.assertEqual(caught.exception.code, code)
        self.write.assert_not_called()
        self.storage.validate_table_root.assert_not_called()

    def test_table_id_exhaustion_and_gaps(self):
        self.active()
        last = TableDef(TableRef(0xFFFFFFFC, "last", 3), Schema((ColumnDef("id", DataType.INT),)))
        manager = CatalogManager(self.storage, Catalog((last,)))
        self.assertEqual(manager.reserve_table_id(), 0xFFFFFFFD)
        with self.assertRaises(DbError) as caught:
            manager.reserve_table_id()
        self.assertEqual(caught.exception.code, "ID_EXHAUSTED")
        self.assertEqual(caught.exception.context["limit"], 0xFFFFFFFD)

    def test_startup_rejects_non_bool_and_missing_storage_interface(self):
        with self.assertRaises(DbError) as caught:
            CatalogManager.bootstrap_or_load(self.storage, 1)
        self.assertEqual(caught.exception.code, "INVALID_ARGUMENT")
        with self.assertRaisesRegex(NotImplementedError, "catalog_services"):
            CatalogManager.bootstrap_or_load(object(), True)
        self.write.assert_not_called()


if __name__ == "__main__":
    unittest.main()
