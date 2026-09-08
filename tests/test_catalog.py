"""张振第二步的表定义、只读目录与固定系统表测试。"""

import importlib.util
import json
import unittest
from dataclasses import FrozenInstanceError
from unittest.mock import patch

from fixtures.contracts import STUDENT_SCHEMA, STUDENT_TABLE
from minidb.catalog.catalog import SYSTEM_CATALOG_TABLE, Catalog
from minidb.core.catalog_protocol import CatalogRead
from minidb.core.schema import (
    SYSTEM_CATALOG_SCHEMA,
    ColumnDef,
    DataType,
    Schema,
    TableDef,
    TableRef,
)


_HAS_SHARED_ERRORS = importlib.util.find_spec("minidb.core.errors") is not None
if _HAS_SHARED_ERRORS:
    from minidb.core.errors import INVALID_ARGUMENT, DbError, ErrorStage

needs_shared_errors = unittest.skipUnless(
    _HAS_SHARED_ERRORS, "等待赵凯航提供 minidb/core/errors.py，未验证公共错误接口"
)


class ValidationAssertions(unittest.TestCase):
    """供目录测试复用的断言：拦截本模块错误出口，不代写公共异常。"""
    def assert_rejected(self, call, *, target: str, operation: str, field: str) -> None:
        # 只拦截张振模块自己的错误出口，验证不合法对象不会构造成功。
        # 不模拟 DbError，也不把该检查当成公共错误类型/阶段的对接验证。
        """把当前模块的错误出口替换为停止信号，检查错误发生的接口和字段。"""
        stop = RuntimeError("测试在本模块的参数拒绝处停止")
        with patch(target, side_effect=stop) as report:
            with self.assertRaises(RuntimeError) as raised:
                call()
        self.assertIs(raised.exception, stop)
        report.assert_called_once()
        self.assertEqual(report.call_args.args[:2], (operation, field))


class TableDefinitionTests(ValidationAssertions):
    """检查表身份、编号边界、固定系统 Schema 和不可变性。"""
    def test_shared_student_fixture_matches_contract(self) -> None:
        """student 固定样例的表号、根页号及三列结构符合计划。"""
        self.assertEqual(STUDENT_TABLE.ref, TableRef(1, "student", 2))
        self.assertIs(STUDENT_TABLE.schema, STUDENT_SCHEMA)
        self.assertEqual(
            [(c.name, c.data_type) for c in STUDENT_TABLE.schema.columns],
            [("id", DataType.INT), ("name", DataType.VARCHAR), ("age", DataType.INT)],
        )
        self.assertEqual(STUDENT_TABLE.schema.find_column("AGE")[0], 2)

    def test_user_table_id_and_root_boundaries(self) -> None:
        """用户表号和根页号的上下边界均可构造。"""
        for table_id in (1, 0xFFFFFFFE):
            for root_page_id in (2, 0xFFFFFFFE):
                with self.subTest(table_id=table_id, root_page_id=root_page_id):
                    ref = TableRef(table_id, "t", root_page_id)
                    table = TableDef(ref, STUDENT_SCHEMA)
                    self.assertEqual(table.ref.table_id, table_id)
                    self.assertEqual(table.ref.root_page_id, root_page_id)

    def test_table_and_reference_are_immutable(self) -> None:
        """表身份、根页号和完整表结构都不可在原对象上改写。"""
        with self.assertRaises(FrozenInstanceError):
            STUDENT_TABLE.ref.name = "other"
        with self.assertRaises(FrozenInstanceError):
            STUDENT_TABLE.ref.root_page_id = 10
        with self.assertRaises(FrozenInstanceError):
            STUDENT_TABLE.schema = Schema((ColumnDef("x", DataType.INT),))

    def test_system_table_has_fixed_identity_and_seven_columns(self) -> None:
        """系统目录固定在表号 0、根页 1，并使用约定的七列。"""
        self.assertEqual(SYSTEM_CATALOG_TABLE.ref, TableRef(0, "_sys_catalog", 1))
        self.assertIs(SYSTEM_CATALOG_TABLE.schema, SYSTEM_CATALOG_SCHEMA)
        self.assertEqual(
            [(c.name, c.data_type.value) for c in SYSTEM_CATALOG_TABLE.schema.columns],
            [
                ("table_id", "INT"), ("table_name", "VARCHAR"),
                ("root_page_id", "INT"), ("column_count", "INT"),
                ("column_index", "INT"), ("column_name", "VARCHAR"),
                ("column_type", "VARCHAR"),
            ],
        )

    def test_equivalent_system_schema_is_accepted_by_value(self) -> None:
        """内容相同的系统 Schema 可以使用，不强求同一个 Python 对象。"""
        copy = Schema(tuple(ColumnDef(c.name, c.data_type) for c in SYSTEM_CATALOG_SCHEMA.columns))
        table = TableDef(SYSTEM_CATALOG_TABLE.ref, copy)
        self.assertEqual(table, SYSTEM_CATALOG_TABLE)

    def test_invalid_table_ids_are_rejected(self) -> None:
        """bool、字符串、负数和哨兵值都不能作为用户表号。"""
        for table_id in (True, False, "1", 1.0, None, -1, 0xFFFFFFFF):
            with self.subTest(table_id=table_id):
                self.assert_rejected(
                    lambda: TableRef(table_id, "student", 2),
                    target="minidb.core.schema._invalid", operation="TableRef", field="table_id",
                )

    def test_invalid_root_ids_are_rejected(self) -> None:
        """非法编号和用户表不可占用的保留页号会被拒绝。"""
        for page_id in (True, False, "2", 2.0, None, -1, 0, 1, 0xFFFFFFFF):
            with self.subTest(page_id=page_id):
                self.assert_rejected(
                    lambda: TableRef(1, "student", page_id),
                    target="minidb.core.schema._invalid", operation="TableRef", field="root_page_id",
                )

    def test_names_must_be_normalized_and_valid(self) -> None:
        """表名必须已经小写且字符、长度合法。"""
        for name in (None, 1, "Student", "", " student", "student ", "学生", "a.b", "1a", "a" * 65):
            with self.subTest(name=name):
                self.assert_rejected(
                    lambda: TableRef(1, name, 2),
                    target="minidb.core.schema._invalid", operation="TableRef", field="name",
                )
        self.assertEqual(TableRef(1, "a" * 64, 2).name, "a" * 64)

    def test_reserved_identity_cannot_be_used_by_user_tables(self) -> None:
        """用户表不能占系统名字、表号或根页。"""
        cases = (
            (0, "student", 1, "name"),
            (0, "_sys_catalog", 2, "root_page_id"),
            (1, "_sys_catalog", 2, "name"),
            (1, "_sys_other", 2, "name"),
        )
        for table_id, name, root_page_id, field in cases:
            with self.subTest(table_id=table_id, name=name, root_page_id=root_page_id):
                self.assert_rejected(
                    lambda: TableRef(table_id, name, root_page_id),
                    target="minidb.core.schema._invalid", operation="TableRef", field=field,
                )

    def test_table_definition_requires_formal_ref_and_schema(self) -> None:
        """TableDef 必须由正式 TableRef 和 Schema 组成。"""
        for ref, schema, field in ((None, STUDENT_SCHEMA, "ref"), (STUDENT_TABLE.ref, None, "schema")):
            with self.subTest(field=field):
                self.assert_rejected(
                    lambda: TableDef(ref, schema),
                    target="minidb.core.schema._invalid", operation="TableDef", field=field,
                )

    def test_system_schema_cannot_be_replaced_reordered_or_retyped(self) -> None:
        """系统目录字段不能被删减、重排或更改类型。"""
        columns = SYSTEM_CATALOG_SCHEMA.columns
        wrong_schemas = (
            STUDENT_SCHEMA,
            Schema(tuple(reversed(columns))),
            Schema(columns[:-1]),
            Schema((ColumnDef("table_id", DataType.VARCHAR),) + columns[1:]),
        )
        for schema in wrong_schemas:
            with self.subTest(schema=schema):
                self.assert_rejected(
                    lambda: TableDef(SYSTEM_CATALOG_TABLE.ref, schema),
                    target="minidb.core.schema._invalid", operation="TableDef", field="schema",
                )

    @needs_shared_errors
    def test_table_validation_uses_the_shared_error_contract(self) -> None:
        """使用真实错误模块验证 TableRef 的参数拒绝接口。"""
        with self.assertRaises(DbError) as raised:
            TableRef(True, "student", 2)
        error = raised.exception
        self.assertEqual(error.code, INVALID_ARGUMENT)
        self.assertIs(error.stage, ErrorStage.SEMANTIC)
        self.assertIsNone(error.span)
        self.assertEqual(error.context["operation"], "TableRef")
        self.assertEqual(error.context["field"], "table_id")
        json.dumps(error.context, ensure_ascii=False)


class CatalogTests(ValidationAssertions):
    """检查内存目录的查表、排序、快照隔离以及重复信息拒绝行为。"""
    def setUp(self) -> None:
        """每个测试开始前建立独立样例，避免前一个测试的状态影响后一个。"""
        self.course = TableDef(TableRef(7, "course", 3), Schema((ColumnDef("cid", DataType.INT),)))
        self.catalog = Catalog((self.course, STUDENT_TABLE))

    def test_empty_catalog(self) -> None:
        """空用户目录列出空列表，查表返回 None。"""
        catalog = Catalog()
        self.assertEqual(catalog.list_tables(), [])
        self.assertIsNone(catalog.find_table("student"))

    def test_catalog_implements_only_the_required_read_protocol(self) -> None:
        """内存目录满足 CatalogRead，但不提供写入或分配编号功能。"""
        catalog: CatalogRead = self.catalog
        self.assertIsInstance(catalog, CatalogRead)
        self.assertIs(catalog.find_table("student"), STUDENT_TABLE)
        self.assertFalse(hasattr(catalog, "persist_and_register"))
        self.assertFalse(hasattr(catalog, "reserve_table_id"))

    def test_lookup_is_case_insensitive_and_keeps_table_identity(self) -> None:
        """不同大小写查询返回同一个原表定义对象。"""
        for name in ("student", "STUDENT", "Student"):
            with self.subTest(name=name):
                self.assertIs(self.catalog.find_table(name), STUDENT_TABLE)
        self.assertIsNone(self.catalog.find_table("missing_table"))

    def test_listing_is_sorted_by_table_id_and_returns_a_new_list(self) -> None:
        """列表按表号排序，修改返回列表不影响目录。"""
        result = self.catalog.list_tables()
        self.assertEqual(result, [STUDENT_TABLE, self.course])
        result.reverse()
        result.clear()
        self.assertEqual(self.catalog.list_tables(), [STUDENT_TABLE, self.course])

    def test_fixed_system_definition_is_outside_the_user_catalog(self) -> None:
        """用户查表和列举接口不暴露固定系统目录。"""
        self.assertIsNone(self.catalog.find_table("_SYS_CATALOG"))
        self.assertIsNone(self.catalog.find_table("_sys_other"))
        self.assertNotIn(SYSTEM_CATALOG_TABLE, self.catalog.list_tables())

    def test_snapshot_is_immutable(self) -> None:
        """目录元组和名字映射都不能原地改写。"""
        with self.assertRaises(FrozenInstanceError):
            self.catalog.tables = ()
        with self.assertRaises(TypeError):
            self.catalog._by_name["student"] = self.course
        self.assertIs(self.catalog.find_table("student"), STUDENT_TABLE)

    def test_new_snapshot_does_not_change_the_original(self) -> None:
        """增加表时新建快照，旧目录仍保持原样。"""
        old = Catalog((STUDENT_TABLE,))
        new = Catalog(old.tables + (self.course,))
        self.assertIsNone(old.find_table("course"))
        self.assertIs(new.find_table("course"), self.course)
        self.assertEqual(old.list_tables(), [STUDENT_TABLE])

    def test_lookup_rejects_invalid_names_without_trimming(self) -> None:
        """非法名字必须拒绝，不自动删空格来把错误输入变成合法名字。"""
        for name in (" student", "student ", "student\n", "学生", "a.b", "", "x" * 65, None, True):
            with self.subTest(name=name):
                self.assert_rejected(
                    lambda: self.catalog.find_table(name),
                    target="minidb.core.schema._invalid", operation="Catalog.find_table", field="name",
                )

    def test_constructor_rejects_mutable_inputs_and_invalid_elements(self) -> None:
        """目录构造器拒绝可变列表和非 TableDef 元素。"""
        for tables, field in (([], "tables"), (None, "tables"), ((None,), "tables[0]"), (({},), "tables[0]")):
            with self.subTest(tables=tables):
                self.assert_rejected(
                    lambda: Catalog(tables),
                    target="minidb.catalog.catalog._invalid", operation="Catalog", field=field,
                )

    def test_constructor_rejects_system_entries(self) -> None:
        """系统目录不能作为普通用户表登记进内存目录。"""
        self.assert_rejected(
            lambda: Catalog((SYSTEM_CATALOG_TABLE,)),
            target="minidb.catalog.catalog._invalid", operation="Catalog", field="tables[0]",
        )

    def test_constructor_rejects_duplicate_names_ids_and_roots(self) -> None:
        """不同用户表不能重复使用名字、表号或根页。"""
        cases = (
            (TableRef(2, "student", 3), "name"),
            (TableRef(1, "course", 3), "table_id"),
            (TableRef(2, "course", 2), "root_page_id"),
        )
        for ref, field in cases:
            with self.subTest(field=field):
                other = TableDef(ref, STUDENT_SCHEMA)
                self.assert_rejected(
                    lambda: Catalog((STUDENT_TABLE, other)),
                    target="minidb.catalog.catalog._invalid", operation="Catalog", field=f"tables[1].ref.{field}",
                )
                self.assertEqual(self.catalog.list_tables(), [STUDENT_TABLE, self.course])

    @needs_shared_errors
    def test_catalog_validation_uses_the_shared_error_contract(self) -> None:
        """接入公共错误后检查 Catalog 错误阶段和字段。"""
        calls = (
            (lambda: Catalog((STUDENT_TABLE, STUDENT_TABLE)), "Catalog", "tables[1].ref.name"),
            (lambda: self.catalog.find_table(" student"), "Catalog.find_table", "name"),
        )
        for call, operation, field in calls:
            with self.subTest(operation=operation):
                with self.assertRaises(DbError) as raised:
                    call()
                error = raised.exception
                self.assertEqual(error.code, INVALID_ARGUMENT)
                self.assertIs(error.stage, ErrorStage.SEMANTIC)
                self.assertIsNone(error.span)
                self.assertEqual(error.context["operation"], operation)
                self.assertEqual(error.context["field"], field)
                json.dumps(error.context, ensure_ascii=False)


if __name__ == "__main__":
    unittest.main()
