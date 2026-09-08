"""目录记录的固定样例、恢复顺序与损坏检测；不模拟页式存储。"""

import importlib.util
import json
import unittest
from itertools import permutations
from unittest.mock import patch

from fixtures.contracts import STUDENT_CATALOG_ROWS, STUDENT_SCHEMA, STUDENT_TABLE
from minidb.catalog.catalog import SYSTEM_CATALOG_TABLE, Catalog
from minidb.catalog.catalog_rows import catalog_from_rows, table_to_catalog_rows
from minidb.core.schema import ColumnDef, DataType, Schema, TableDef, TableRef


_HAS_SHARED_ERRORS = importlib.util.find_spec("minidb.core.errors") is not None
if _HAS_SHARED_ERRORS:
    from minidb.core.errors import CATALOG_CORRUPTED, INVALID_ARGUMENT, DbError, ErrorStage

needs_shared_errors = unittest.skipUnless(
    _HAS_SHARED_ERRORS, "等待赵凯航提供 minidb/core/errors.py，未验证公共错误接口"
)


def changed(row, index, value):
    """复制一条元组并替换指定字段，用来构造损坏样例，原记录保持不变。"""
    return row[:index] + (value,) + row[index + 1:]


class CatalogRowsWriteTests(unittest.TestCase):
    """验证表定义如何转换为固定七字段目录行。"""
    def test_student_rows_match_independently_written_fixture(self) -> None:
        """转换结果与独立手写的七字段预期比较，避免自证正确。"""
        self.assertEqual(table_to_catalog_rows(STUDENT_TABLE), STUDENT_CATALOG_ROWS)

    def test_one_column_uses_count_one_and_index_zero(self) -> None:
        """单列表的列数为 1、列序号从 0 开始。"""
        table = TableDef(TableRef(9, "course", 12), Schema((ColumnDef("title", DataType.VARCHAR),)))
        self.assertEqual(table_to_catalog_rows(table), ((9, "course", 12, 1, 0, "title", "VARCHAR"),))

    def test_sixty_four_columns_keep_their_order_and_total_count(self) -> None:
        """64 列边界转换和倒序恢复后仍保持原列序。"""
        table = TableDef(
            TableRef(2, "wide", 3),
            Schema(tuple(ColumnDef(f"c{i}", DataType.INT) for i in range(64))),
        )
        rows = table_to_catalog_rows(table)
        self.assertEqual(len(rows), 64)
        self.assertEqual(rows[0], (2, "wide", 3, 64, 0, "c0", "INT"))
        self.assertEqual(rows[-1], (2, "wide", 3, 64, 63, "c63", "INT"))
        self.assertEqual(catalog_from_rows(reversed(rows)).find_table("wide"), table)

    def test_output_is_immutable_and_does_not_change_the_source(self) -> None:
        """输出目录行不可变，转换过程不修改输入 TableDef。"""
        rows = table_to_catalog_rows(STUDENT_TABLE)
        with self.assertRaises(TypeError):
            rows[0][1] = "other"
        self.assertIs(STUDENT_TABLE.schema, STUDENT_SCHEMA)
        self.assertEqual(STUDENT_TABLE.ref.name, "student")

    def test_invalid_arguments_and_system_table_are_rejected(self) -> None:
        """转换函数只接受普通用户表的正式 TableDef。"""
        for value in (None, {}, STUDENT_SCHEMA, SYSTEM_CATALOG_TABLE):
            with self.subTest(value=value):
                stop = RuntimeError("停止于目录转换参数拒绝处")
                with patch("minidb.catalog.catalog_rows._invalid_argument", side_effect=stop) as report:
                    with self.assertRaises(RuntimeError) as raised:
                        table_to_catalog_rows(value)
                self.assertIs(raised.exception, stop)
                self.assertEqual(report.call_args.args[:2], ("table_to_catalog_rows", "table"))


class CatalogRowsReadTests(unittest.TestCase):
    """验证完整目录行如何恢复成按 Schema 排序的表定义。"""
    def test_empty_rows_restore_an_empty_user_catalog(self) -> None:
        """没有目录行时恢复为空用户目录。"""
        for rows in ((), [], iter(())):
            with self.subTest(rows=rows):
                self.assertEqual(catalog_from_rows(rows).list_tables(), [])

    def test_handwritten_rows_restore_the_expected_table(self) -> None:
        """手工目录行能恢复为约定的 student 完整定义。"""
        catalog = catalog_from_rows(STUDENT_CATALOG_ROWS)
        self.assertEqual(catalog.list_tables(), [STUDENT_TABLE])
        self.assertEqual(catalog.find_table("STUDENT").schema.find_column("AGE")[0], 2)

    def test_all_student_row_permutations_preserve_column_order(self) -> None:
        """三条目录行的所有排列都恢复为同样列序。"""
        for rows in permutations(STUDENT_CATALOG_ROWS):
            with self.subTest(rows=rows):
                self.assertEqual(catalog_from_rows(rows).find_table("student"), STUDENT_TABLE)

    def test_interleaved_tables_and_nonconsecutive_ids(self) -> None:
        """不同表交错出现且表号不连续时仍正确分组。"""
        rows = (
            (7, "course", 4, 2, 1, "title", "VARCHAR"),
            STUDENT_CATALOG_ROWS[2],
            (7, "course", 4, 2, 0, "cid", "INT"),
            STUDENT_CATALOG_ROWS[0],
            STUDENT_CATALOG_ROWS[1],
        )
        catalog = catalog_from_rows(rows)
        self.assertEqual([t.ref.table_id for t in catalog.list_tables()], [1, 7])
        self.assertEqual(catalog.find_table("student"), STUDENT_TABLE)
        self.assertEqual(catalog.find_table("course").schema.columns, (
            ColumnDef("cid", DataType.INT), ColumnDef("title", DataType.VARCHAR),
        ))

    def test_input_iterator_is_consumed_once_and_input_list_is_unchanged(self) -> None:
        """输入流只消费一次，原有列表的顺序保持不变。"""
        visited = []
        rows = list(reversed(STUDENT_CATALOG_ROWS))

        def source():
            """按当前测试设定逐条提供记录，用于观察遍历、异常传播或关闭行为。"""
            for row in rows:
                visited.append(row)
                yield row

        catalog = catalog_from_rows(source())
        self.assertEqual(visited, rows)
        self.assertEqual(rows, list(reversed(STUDENT_CATALOG_ROWS)))
        self.assertEqual(catalog.find_table("student"), STUDENT_TABLE)

    def test_large_metadata_stream(self) -> None:
        # 100 张表、每表 64 列；这里只验证目录记录流，不声称已测试物理跨页。
        """用 6400 条内存目录记录检查分组逻辑，不视为跨页测试。"""
        def rows():
            """按测试规定生成多表多列的目录记录流，不读取真实数据页。"""
            for column_index in reversed(range(64)):
                for table_id in reversed(range(1, 101)):
                    yield (table_id, f"t{table_id}", table_id + 1, 64, column_index, f"c{column_index}", "INT")

        catalog = catalog_from_rows(rows())
        self.assertEqual(len(catalog.list_tables()), 100)
        self.assertEqual([t.ref.table_id for t in catalog.list_tables()], list(range(1, 101)))
        self.assertEqual(catalog.find_table("T100").schema.find_column("C63")[0], 63)

    def test_maximum_ids_and_identifier_lengths_are_preserved(self) -> None:
        """最大合法编号和最长合法名字不会在恢复时被截断。"""
        name = "t" * 64
        column = "c" * 64
        rows = ((0xFFFFFFFE, name, 0xFFFFFFFE, 1, 0, column, "INT"),)
        table = catalog_from_rows(rows).find_table(name.upper())
        self.assertEqual(table.ref, TableRef(0xFFFFFFFE, name, 0xFFFFFFFE))
        self.assertEqual(table.schema.columns, (ColumnDef(column, DataType.INT),))

    def test_column_names_can_use_system_prefix(self) -> None:
        # 规划只禁止用户表名占用 _sys_，不扩大为列名限制。
        """保留前缀只限制表名，不能错误扩展到列名。"""
        rows = ((1, "student", 2, 1, 0, "_sys_value", "INT"),)
        self.assertIsNotNone(catalog_from_rows(rows).find_table("student").schema.find_column("_sys_value"))

    def test_reader_exception_is_propagated_without_a_partial_catalog(self) -> None:
        """上游读取中途失败时传播原异常，不发布已经读到的部分目录。"""
        original = OSError("模拟上游读取失败")

        def source():
            """按当前测试设定逐条提供记录，用于观察遍历、异常传播或关闭行为。"""
            yield from STUDENT_CATALOG_ROWS
            raise original

        with patch("minidb.catalog.catalog_rows.Catalog") as publish:
            with self.assertRaises(OSError) as raised:
                catalog_from_rows(source())
        self.assertIs(raised.exception, original)
        publish.assert_not_called()

    def test_loader_does_not_close_an_iterator_owned_by_the_caller(self) -> None:
        # 不冒充正式 RowScan：此处只是带 finally 的普通记录生成器。
        """纯目录转换函数不接管输入迭代器的关闭责任。"""
        closed = []

        def source():
            """按当前测试设定逐条提供记录，用于观察遍历、异常传播或关闭行为。"""
            try:
                yield (1, "student", 2, 0, 0, "id", "INT")
                yield from STUDENT_CATALOG_ROWS
            finally:
                closed.append(True)

        stream = source()
        with patch("minidb.catalog.catalog_rows._corrupted", side_effect=RuntimeError("停止")):
            with self.assertRaises(RuntimeError):
                catalog_from_rows(stream)
        self.assertEqual(closed, [])
        stream.close()
        self.assertEqual(closed, [True])


class CatalogRowsCorruptionTests(unittest.TestCase):
    """逐项破坏目录记录，确认加载器能发现不一致并停止发布结果。"""
    def assert_corrupted(self, rows, field: str):
        # 只替换本模块的错误出口，验证拒绝行为，不模拟公共 DbError。
        """断言目录行被拒绝，并确认失败时没有发布半个 Catalog。"""
        stop = RuntimeError("停止于目录损坏报告处")
        with patch("minidb.catalog.catalog_rows._corrupted", side_effect=stop) as report:
            with patch("minidb.catalog.catalog_rows.Catalog") as publish:
                with self.assertRaises(RuntimeError) as raised:
                    catalog_from_rows(rows)
        self.assertIs(raised.exception, stop)
        report.assert_called_once()
        self.assertEqual(report.call_args.args[0], field)
        publish.assert_not_called()
        return report.call_args

    def test_wrong_row_shape_is_rejected(self) -> None:
        """目录行必须是恰好七字段的元组。"""
        for row in (None, {}, "bad row", (), STUDENT_CATALOG_ROWS[0][:-1], STUDENT_CATALOG_ROWS[0] + (8,), list(STUDENT_CATALOG_ROWS[0])):
            with self.subTest(row=row):
                self.assert_corrupted((row,), "rows[0]")

    def test_integer_fields_reject_bool_float_strings_and_none(self) -> None:
        """INT 字段不能自动接受 bool、浮点数、数字字符串或 None。"""
        for index, field in ((0, "table_id"), (2, "root_page_id"), (3, "column_count"), (4, "column_index")):
            for value in (True, False, "1", 1.0, None):
                with self.subTest(field=field, value=value):
                    self.assert_corrupted((changed(STUDENT_CATALOG_ROWS[0], index, value),), f"rows[0].{field}")

    def test_string_fields_reject_non_strings(self) -> None:
        """目录的名字和类型文本必须为真正字符串。"""
        for index, field in ((1, "table_name"), (5, "column_name"), (6, "column_type")):
            for value in (True, 1, b"INT", None, DataType.INT):
                with self.subTest(field=field, value=value):
                    self.assert_corrupted((changed(STUDENT_CATALOG_ROWS[0], index, value),), f"rows[0].{field}")

    def test_out_of_range_values_are_rejected(self) -> None:
        """表号、页号、列数和列序号均检查范围。"""
        cases = (
            (0, "table_id", (-1, 0, 0xFFFFFFFF)),
            (2, "root_page_id", (-1, 0, 1, 0xFFFFFFFF)),
            (3, "column_count", (-1, 0, 65)),
            (4, "column_index", (-1, 3, 64)),
        )
        for index, field, values in cases:
            for value in values:
                with self.subTest(field=field, value=value):
                    self.assert_corrupted((changed(STUDENT_CATALOG_ROWS[0], index, value),), f"rows[0].{field}")

    def test_names_are_not_silently_normalized_or_trimmed(self) -> None:
        """磁盘目录名字不合法时拒绝加载，不自动修复。"""
        for index, field in ((1, "table_name"), (5, "column_name")):
            for value in ("Student", "", " name", "name ", "name\n", "1name", "t.name", "中文", "a" * 65):
                with self.subTest(field=field, value=value):
                    self.assert_corrupted((changed(STUDENT_CATALOG_ROWS[0], index, value),), f"rows[0].{field}")

    def test_reserved_table_names_are_rejected(self) -> None:
        """用户目录记录不能使用 _sys_ 表名前缀。"""
        for name in ("_sys_catalog", "_sys_other"):
            with self.subTest(name=name):
                self.assert_corrupted((changed(STUDENT_CATALOG_ROWS[0], 1, name),), "rows[0].table_name")

    def test_column_type_requires_exact_supported_name(self) -> None:
        """目录列类型只接受准确的 INT 或 VARCHAR 文本。"""
        for value in ("BOOL", "FLOAT", "int", "varchar", "VARCHAR(20)", "", "INT "):
            with self.subTest(value=value):
                self.assert_corrupted((changed(STUDENT_CATALOG_ROWS[0], 6, value),), "rows[0].column_type")

    def test_missing_first_middle_or_last_column_is_detected(self) -> None:
        """缺第一列、中间列或最后一列都能通过总列数发现。"""
        for omitted in range(3):
            with self.subTest(omitted=omitted):
                rows = STUDENT_CATALOG_ROWS[:omitted] + STUDENT_CATALOG_ROWS[omitted + 1:]
                reported = self.assert_corrupted(rows, "column_index")
                self.assertEqual(reported.args[2], [0, 1, 2])
                self.assertEqual(reported.args[3], [i for i in range(3) if i != omitted])
                self.assertEqual(reported.kwargs["table_id"], 1)

    def test_duplicate_column_index_is_detected_even_for_identical_rows(self) -> None:
        """即使重复行内容相同，也不能重复登记同一列序号。"""
        rows = STUDENT_CATALOG_ROWS + (STUDENT_CATALOG_ROWS[0],)
        self.assert_corrupted(rows, "rows[3].column_index")

    def test_duplicate_column_name_is_detected(self) -> None:
        """同一表中不同列序号也不能重复列名。"""
        rows = (STUDENT_CATALOG_ROWS[0], changed(STUDENT_CATALOG_ROWS[1], 5, "id"))
        self.assert_corrupted(rows, "rows[1].column_name")

    def test_conflicting_headers_within_one_table_are_rejected(self) -> None:
        """同一表各行的表名、根页号和总列数必须一致。"""
        for index, field, value in ((1, "table_name", "other"), (2, "root_page_id", 8), (3, "column_count", 4)):
            with self.subTest(field=field):
                rows = (STUDENT_CATALOG_ROWS[0], changed(STUDENT_CATALOG_ROWS[1], index, value))
                self.assert_corrupted(rows, f"rows[1].{field}")

    def test_different_table_ids_cannot_share_names_or_roots(self) -> None:
        """不同表号不能指向相同表名或根页。"""
        for row, field in (
            ((7, "student", 4, 1, 0, "cid", "INT"), "table_name"),
            ((7, "course", 2, 1, 0, "cid", "INT"), "root_page_id"),
        ):
            with self.subTest(field=field):
                self.assert_corrupted(STUDENT_CATALOG_ROWS + (row,), f"rows[3].{field}")

    def test_an_incomplete_second_table_does_not_publish_the_first(self) -> None:
        """后一张表不完整时整次加载失败，不先公布前一张表。"""
        original = Catalog((STUDENT_TABLE,))
        rows = STUDENT_CATALOG_ROWS + ((7, "course", 4, 2, 0, "cid", "INT"),)
        report = self.assert_corrupted(rows, "column_index")
        self.assertEqual(report.kwargs["table_id"], 7)
        self.assertEqual(original.list_tables(), [STUDENT_TABLE])

    def test_non_iterable_input_is_an_argument_error(self) -> None:
        """不能迭代的输入属于接口参数错误。"""
        for value in (None, 1, True):
            with self.subTest(value=value):
                stop = RuntimeError("停止于参数拒绝处")
                with patch("minidb.catalog.catalog_rows._invalid_argument", side_effect=stop) as report:
                    with self.assertRaises(RuntimeError) as raised:
                        catalog_from_rows(value)
                self.assertIs(raised.exception, stop)
                self.assertEqual(report.call_args.args[:2], ("catalog_from_rows", "rows"))


@needs_shared_errors
class CatalogRowsErrorContractTests(unittest.TestCase):
    """接入真实 DbError 后检查错误码、阶段和上下文字段。"""
    def test_corruption_has_storage_stage_and_table_context(self) -> None:
        """目录损坏应使用 STORAGE 阶段，并携带已知表号。"""
        with self.assertRaises(DbError) as raised:
            catalog_from_rows(STUDENT_CATALOG_ROWS[:-1])
        error = raised.exception
        self.assertEqual(error.code, CATALOG_CORRUPTED)
        self.assertIs(error.stage, ErrorStage.STORAGE)
        self.assertIsNone(error.span)
        self.assertEqual(error.context["operation"], "catalog_from_rows")
        self.assertEqual(error.context["table_id"], 1)
        self.assertEqual(error.context["expected"], [0, 1, 2])
        self.assertEqual(error.context["actual"], [0, 1])
        self.assertIn("reason", error.context)
        json.dumps(error.context, ensure_ascii=False, allow_nan=False)

    def test_argument_errors_use_the_shared_contract(self) -> None:
        """参数错误采用公共 INVALID_ARGUMENT，context 必须可序列化。"""
        for call in (lambda: table_to_catalog_rows(SYSTEM_CATALOG_TABLE), lambda: catalog_from_rows(None)):
            with self.subTest(call=call):
                with self.assertRaises(DbError) as raised:
                    call()
                error = raised.exception
                self.assertEqual(error.code, INVALID_ARGUMENT)
                self.assertIs(error.stage, ErrorStage.STORAGE)
                self.assertIsNone(error.span)
                json.dumps(error.context, ensure_ascii=False, allow_nan=False)

    def test_malformed_row_context_is_json_safe_without_a_fake_table_id(self) -> None:
        """坏行诊断不伪造表号，也不把循环对象或 NaN 放入 JSON。"""
        circular = []
        circular.append(circular)
        for row in (None, circular, (object(),), (float("nan"),)):
            with self.subTest(row=row):
                with self.assertRaises(DbError) as raised:
                    catalog_from_rows((row,))
                error = raised.exception
                self.assertEqual(error.code, CATALOG_CORRUPTED)
                self.assertNotIn("table_id", error.context)
                json.dumps(error.context, ensure_ascii=False, allow_nan=False)


if __name__ == "__main__":
    unittest.main()
