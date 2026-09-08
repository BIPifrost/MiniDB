"""张振的目录调度测试。

Mock 只记录 StorageEngine 的调用和预置返回值，不实现存储、编码或公共类型。
本文件验证目录管理逻辑，不作为真实落盘、重启或 RowCodec 对接的通过证据。
"""

import unittest
from unittest.mock import MagicMock, Mock, call, patch

from fixtures.contracts import STUDENT_CATALOG_ROWS, STUDENT_SCHEMA, STUDENT_TABLE
from minidb.catalog.catalog import SYSTEM_CATALOG_TABLE, Catalog
from minidb.catalog.catalog_manager import CatalogManager
from minidb.core.errors import DbError, ErrorStage, IO_READ_FAILED, IO_CLOSE_FAILED
from minidb.core.schema import ColumnDef, DataType, Schema, TableDef, TableRef


class CatalogManagerTests(unittest.TestCase):
    """重点检查调用顺序、失败后不发布目录以及扫描关闭责任。"""

    def setUp(self):
        """只创建带已确定方法名的 Mock，避免测试误调用不存在的存储接口。"""
        self.storage = Mock(spec_set=[
            "initialize_reserved_heap", "validate_table_root", "scan_rows", "insert_row",
        ])
        self.scan = MagicMock(spec_set=["__iter__", "close"])
        self.storage.scan_rows.return_value = self.scan
        self.scan.__iter__.side_effect = lambda: iter(
            Mock(values=row) for row in reversed(STUDENT_CATALOG_ROWS)
        )

    def test_new_database_initializes_only_reserved_heap(self):
        """新文件先初始化再验证系统根页，初始目录为空且从表号 1 开始。"""
        manager = CatalogManager.bootstrap_or_load(self.storage, True)
        self.assertEqual(self.storage.mock_calls, [
            call.initialize_reserved_heap(SYSTEM_CATALOG_TABLE),
            call.validate_table_root(SYSTEM_CATALOG_TABLE),
        ])
        self.assertEqual(manager.list_tables(), [])
        self.assertEqual(manager.reserve_table_id(), 1)

    def test_existing_database_loads_closes_then_validates_user_roots(self):
        """已有文件不得再次初始化；用户根页校验发生在目录扫描关闭之后。"""
        events = []
        self.storage.validate_table_root.side_effect = lambda table: events.append(table.ref.name)
        self.scan.close.side_effect = lambda: events.append("close")
        manager = CatalogManager.bootstrap_or_load(self.storage, False)
        self.assertEqual(events, ["_sys_catalog", "close", "student"])
        self.assertEqual(manager.find_table("STUDENT"), STUDENT_TABLE)
        self.assertEqual(manager.reserve_table_id(), 2)
        self.storage.initialize_reserved_heap.assert_not_called()
        self.scan.close.assert_called_once_with()

    def test_broken_rows_close_scan_and_do_not_validate_users(self):
        """目录缺列时关闭扫描，并且不继续假装已经恢复出用户表。"""
        self.scan.__iter__.side_effect = lambda: iter(Mock(values=row) for row in STUDENT_CATALOG_ROWS[:-1])
        stop = RuntimeError("目录记录拒绝信号")
        with patch("minidb.catalog.catalog_rows._corrupted", side_effect=stop):
            with self.assertRaises(RuntimeError) as raised:
                CatalogManager.bootstrap_or_load(self.storage, False)
        self.assertIs(raised.exception, stop)
        self.scan.close.assert_called_once_with()
        self.storage.validate_table_root.assert_called_once_with(SYSTEM_CATALOG_TABLE)

    def test_read_failure_keeps_first_error_when_close_also_fails(self):
        """读取和关闭同时失败时传播第一次异常，并用 note 保存关闭失败原因。"""
        original = OSError("读取失败")

        def broken_rows():
            """在读到部分真实目录值之后模拟上游异常，不模拟磁盘。"""
            yield Mock(values=STUDENT_CATALOG_ROWS[0])
            raise original

        self.scan.__iter__.side_effect = broken_rows
        self.scan.close.side_effect = OSError("关闭失败")
        with self.assertRaises(OSError) as raised:
            CatalogManager.bootstrap_or_load(self.storage, False)
        self.assertIs(raised.exception, original)
        self.assertIn("关闭失败", original.__notes__[0])
        self.scan.close.assert_called_once_with()

    def test_close_failure_prevents_returning_a_loaded_manager(self):
        """即使目录值都合法，扫描关闭失败也不能视为启动成功。"""
        self.scan.close.side_effect = OSError("close failed")
        with self.assertRaises(OSError):
            CatalogManager.bootstrap_or_load(self.storage, False)
        self.storage.validate_table_root.assert_called_once_with(SYSTEM_CATALOG_TABLE)

    def test_user_root_failure_is_propagated_after_scan_closed(self):
        """用户根页不匹配时停止加载，原始存储异常保持不变。"""
        original = OSError("用户根页校验失败")
        self.storage.validate_table_root.side_effect = [None, original]
        with self.assertRaises(OSError) as raised:
            CatalogManager.bootstrap_or_load(self.storage, False)
        self.assertIs(raised.exception, original)
        self.scan.close.assert_called_once_with()

    def test_user_root_db_error_keeps_user_table_name(self):
        # 测试 catalog_manager.py 的 _catalog_context：
        # 用户表错误不能被默认的系统目录名 _sys_catalog 覆盖。
        original = DbError(ErrorStage.STORAGE, IO_READ_FAILED, "用户表根页读取失败",
                           context={"operation": "read_page"})
        self.storage.validate_table_root.side_effect = [None, original]
        with self.assertRaises(DbError) as raised:
            CatalogManager.bootstrap_or_load(self.storage, False)
        self.assertIs(raised.exception, original)
        self.assertEqual(original.context["table_name"], "student")
        self.assertEqual(original.context["table_id"], STUDENT_TABLE.ref.table_id)
        self.scan.close.assert_called_once_with()

    def test_shared_storage_errors_keep_identity_and_cleanup_details(self):
        """接通同一套错误类型后，目录保留底层主错误，并附带关闭错误的信息。"""
        original = DbError(ErrorStage.STORAGE, IO_READ_FAILED, "读取失败",
                           context={"operation": "read_page", "page_id": 1})
        cleanup = DbError(ErrorStage.STORAGE, IO_CLOSE_FAILED, "关闭失败",
                          context={"operation": "close"})
        self.scan.__iter__.side_effect = original
        self.scan.close.side_effect = cleanup
        with self.assertRaises(DbError) as raised:
            CatalogManager.bootstrap_or_load(self.storage, False)
        self.assertIs(raised.exception, original)
        self.assertEqual(original.code, IO_READ_FAILED)
        self.assertIs(original.stage, ErrorStage.STORAGE)
        self.assertEqual(original.context["operation"], "read_page")
        self.assertEqual(original.context["table_name"], "_sys_catalog")
        self.assertEqual(original.context["cleanup_errors"], [{
            "stage": "STORAGE", "code": IO_CLOSE_FAILED, "message": "关闭失败",
            "context": {"operation": "close"},
        }])
        self.scan.close.assert_called_once_with()

    def test_register_publishes_only_after_every_insert(self):
        """每次目录行写入时新表仍不可见，最后一行成功后才发布新快照。"""
        manager = CatalogManager(self.storage, Catalog())
        old = manager._catalog
        order = []
        self.storage.validate_table_root.side_effect = lambda table: order.append("root")

        def insert(table, row):
            """只观察管理器是否过早公布表，不实现插入或返回假 RowId。"""
            self.assertIsNone(manager.find_table("student"))
            order.append("insert")

        self.storage.insert_row.side_effect = insert
        with patch("minidb.catalog.catalog_manager._preflight_rows", side_effect=lambda rows: order.append("preflight")) as preflight:
            manager.persist_and_register(STUDENT_TABLE)
        self.assertEqual(order, ["root", "preflight", "insert", "insert", "insert"])
        preflight.assert_called_once_with(STUDENT_CATALOG_ROWS)
        self.assertEqual(self.storage.insert_row.call_args_list,
                         [call(SYSTEM_CATALOG_TABLE, row) for row in STUDENT_CATALOG_ROWS])
        self.assertEqual(manager.list_tables(), [STUDENT_TABLE])
        self.assertEqual(old.list_tables(), [])
        self.assertEqual(manager.reserve_table_id(), 2)

    def test_preflight_failure_makes_no_writes(self):
        """任意目录行在编码预检处失败时，不调用一次 insert_row。"""
        manager = CatalogManager(self.storage, Catalog())
        with patch("minidb.catalog.catalog_manager._preflight_rows", side_effect=ValueError("预检失败")):
            with self.assertRaises(ValueError):
                manager.persist_and_register(STUDENT_TABLE)
        self.storage.insert_row.assert_not_called()
        self.assertEqual(manager.list_tables(), [])

    def test_partial_write_failure_keeps_old_snapshot(self):
        """第二条目录记录写入失败后保持旧内存目录，不伪称磁盘已经回滚。"""
        manager = CatalogManager(self.storage, Catalog())
        failure = OSError("第二行写入失败")
        self.storage.insert_row.side_effect = [None, failure]
        with patch("minidb.catalog.catalog_manager._preflight_rows"):
            with self.assertRaises(OSError) as raised:
                manager.persist_and_register(STUDENT_TABLE)
        self.assertIs(raised.exception, failure)
        self.assertEqual(self.storage.insert_row.call_count, 2)
        self.assertEqual(manager.list_tables(), [])

    def test_duplicate_names_ids_and_roots_fail_before_storage(self):
        """在任何存储调用之前拒绝重复名字、表号、根页及系统表。"""
        manager = CatalogManager(self.storage, Catalog((STUDENT_TABLE,)))
        cases = (
            (STUDENT_TABLE, "TABLE_EXISTS"),
            (TableDef(TableRef(1, "course", 3), STUDENT_SCHEMA), "INVALID_ARGUMENT"),
            (TableDef(TableRef(2, "course", 2), STUDENT_SCHEMA), "INVALID_ARGUMENT"),
            (SYSTEM_CATALOG_TABLE, "INVALID_ARGUMENT"),
            (None, "INVALID_ARGUMENT"),
        )
        for table, code in cases:
            with self.subTest(code=code, table=table):
                with patch("minidb.catalog.catalog_manager._error", side_effect=RuntimeError("拒绝")) as report:
                    with self.assertRaises(RuntimeError):
                        manager.persist_and_register(table)
                self.assertEqual(report.call_args.args[0], code)
        self.assertEqual(self.storage.mock_calls, [])

    def test_table_id_exhaustion_and_gaps(self):
        """从最大表号之后继续编号，允许空号；发出最后一个合法号后拒绝溢出。"""
        last = TableDef(TableRef(0xFFFFFFFD, "last", 2), Schema((ColumnDef("id", DataType.INT),)))
        manager = CatalogManager(self.storage, Catalog((last,)))
        self.assertEqual(manager.reserve_table_id(), 0xFFFFFFFE)
        with patch("minidb.catalog.catalog_manager._error", side_effect=RuntimeError("耗尽")) as report:
            with self.assertRaises(RuntimeError):
                manager.reserve_table_id()
        self.assertEqual(report.call_args.args[0], "ID_EXHAUSTED")
        self.assertEqual(report.call_args.kwargs, {"id_kind": "table", "limit": 0xFFFFFFFE})

    def test_startup_rejects_non_bool_and_missing_storage_interface(self):
        """参数检查不用把数字 1 当作 True，也不接受缺方法的存储对象。"""
        for storage, is_new in ((self.storage, 1), (object(), True)):
            with patch("minidb.catalog.catalog_manager._error", side_effect=RuntimeError("参数拒绝")) as report:
                with self.assertRaises(RuntimeError):
                    CatalogManager.bootstrap_or_load(storage, is_new)
            self.assertEqual(report.call_args.args[0], "INVALID_ARGUMENT")


if __name__ == "__main__":
    unittest.main()
