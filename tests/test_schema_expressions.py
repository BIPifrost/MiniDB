"""张振的 Schema 和表达式类型规则测试，直接验证正式公共错误。"""

from __future__ import annotations

import json
import unittest
from dataclasses import FrozenInstanceError
from itertools import product

from minidb.core.expressions import ExprOp, resolve_result_type
from minidb.core.errors import INVALID_ARGUMENT, DbError, ErrorStage
from minidb.core.schema import ColumnDef, DataType, Schema, TypeSpec


class SchemaTests(unittest.TestCase):
    """验证列顺序、按名称查列、类型限制和不可变对象。"""
    def setUp(self) -> None:
        """每个测试开始前建立独立样例，避免前一个测试的状态影响后一个。"""
        self.schema = Schema((
            ColumnDef("id", DataType.INT),
            ColumnDef("name", DataType.VARCHAR),
            ColumnDef("age", DataType.INT),
        ))

    def assert_invalid(self, call, operation: str) -> DbError:
        """使用正式 DbError 验证语义参数错误及其可序列化上下文。"""
        with self.assertRaises(DbError) as raised:
            call()
        error = raised.exception
        self.assertEqual(error.code, INVALID_ARGUMENT)
        self.assertIs(error.stage, ErrorStage.SEMANTIC)
        self.assertIsNone(error.span)
        self.assertEqual(error.context["operation"], operation)
        self.assertTrue({"field", "expected", "actual"} <= error.context.keys())
        json.dumps(error.context, ensure_ascii=False)
        return error

    def test_lookup_uses_schema_order_and_ignores_name_case(self) -> None:
        """查列忽略名称大小写，返回的索引仍按 Schema 原始顺序。"""
        self.assertEqual(self.schema.find_column("AGE"), (2, self.schema.columns[2]))
        self.assertEqual(self.schema.find_column("Name"), (1, self.schema.columns[1]))
        self.assertEqual(self.schema.find_column("id"), (0, self.schema.columns[0]))
        self.assertEqual(tuple(c.name for c in self.schema.columns), ("id", "name", "age"))

    def test_missing_column_returns_none(self) -> None:
        """不存在的列返回 None，交给语义分析器补上具体错误位置。"""
        self.assertIsNone(self.schema.find_column("missing_col"))

    def test_lookup_rejects_invalid_names_without_trimming(self) -> None:
        """非法名字必须拒绝，不自动删空格来把错误输入变成合法名字。"""
        for name in ("", " id", "id ", "id\n", "student.id", "名字", "1id", "a-b", "x" * 65, None, 1, True):
            with self.subTest(name=name):
                self.assert_invalid(lambda: self.schema.find_column(name), "Schema.find_column")

    def test_identifier_length_boundaries(self) -> None:
        """长度 1 和 64 的合法名称可以使用。"""
        for name in ("x", "_", "_col_09", "x" * 64):
            with self.subTest(name=name):
                schema = Schema((ColumnDef(name, DataType.INT),))
                self.assertEqual(schema.find_column(name.upper()), (0, schema.columns[0]))

    def test_column_requires_an_already_normalized_name(self) -> None:
        """ColumnDef 接收的名字必须已经小写。"""
        self.assert_invalid(lambda: ColumnDef("Age", DataType.INT), "ColumnDef")

    def test_column_rejects_invalid_names(self) -> None:
        """空名字、中文、点号、数字开头和超长名字不能建列定义。"""
        for name in ("", " age", "中文", "a.b", "1age", "x" * 65, 42):
            with self.subTest(name=name):
                self.assert_invalid(lambda: ColumnDef(name, DataType.INT), "ColumnDef")

    def test_column_type_rejects_raw_strings_and_python_types(self) -> None:
        """v2允许BOOL列，但不允许字符串、Python类型或bool值冒充类型声明。"""
        for data_type in ("INT", "VARCHAR", int, str, bool, True, None):
            with self.subTest(data_type=data_type):
                self.assert_invalid(lambda: ColumnDef("age", data_type), "ColumnDef")

    def test_v2_column_types_preserve_full_type_spec(self) -> None:
        for spec in (TypeSpec(DataType.BOOL), TypeSpec(DataType.DATE),
                     TypeSpec(DataType.DECIMAL, precision=6, scale=2)):
            with self.subTest(spec=spec):
                column = ColumnDef("value", spec)
                self.assertEqual(column.type_spec, spec)
                self.assertIs(column.data_type, spec.kind)
                self.assertTrue(column.nullable)

    def test_column_count_boundaries(self) -> None:
        """Schema 接受 1 列和 64 列这两个合法边界。"""
        for count in (1, 64):
            with self.subTest(count=count):
                schema = Schema(tuple(ColumnDef(f"c{i}", DataType.INT) for i in range(count)))
                self.assertEqual(len(schema.columns), count)
                self.assertEqual(schema.find_column(f"c{count - 1}")[0], count - 1)

    def test_invalid_column_counts(self) -> None:
        """0 列和 65 列超出本项目范围。"""
        for count in (0, 65):
            with self.subTest(count=count):
                self.assert_invalid(
                    lambda: Schema(tuple(ColumnDef(f"c{i}", DataType.INT) for i in range(count))),
                    "Schema",
                )

    def test_duplicate_column_is_rejected(self) -> None:
        """同一个 Schema 中不允许两列同名。"""
        self.assert_invalid(
            lambda: Schema((ColumnDef("id", DataType.INT), ColumnDef("id", DataType.VARCHAR))),
            "Schema",
        )

    def test_schema_rejects_mutable_sequence_and_wrong_elements(self) -> None:
        """Schema 只接受由 ColumnDef 组成的元组。"""
        for columns in (list(self.schema.columns), None, ("id",), (None,)):
            with self.subTest(columns=columns):
                self.assert_invalid(lambda: Schema(columns), "Schema")

    def test_schema_and_nested_columns_are_immutable(self) -> None:
        """列集合和其中的列定义都不能被调用者修改。"""
        with self.assertRaises(FrozenInstanceError):
            self.schema.columns = ()
        with self.assertRaises(FrozenInstanceError):
            self.schema.columns[0].name = "changed"
        with self.assertRaises(TypeError):
            self.schema.columns[0] = ColumnDef("changed", DataType.INT)


class ExpressionTypeTests(unittest.TestCase):
    """验证所有操作符和类型组合是否符合项目的唯一类型规则。"""
    def test_all_operator_and_type_combinations(self) -> None:
        """枚举所有类型组合，对照独立列出的规则表验证结果。"""
        # v2第4.4节：数值交叉比较、字符串和日期排序、BOOL仅等于/不等于。
        # 0—3个操作数共1404种组合；不通过调用生产比较函数生成预期结果。
        ordered_pairs = (
            (DataType.INT, DataType.INT), (DataType.INT, DataType.DECIMAL),
            (DataType.DECIMAL, DataType.INT), (DataType.DECIMAL, DataType.DECIMAL),
            (DataType.VARCHAR, DataType.VARCHAR), (DataType.DATE, DataType.DATE),
        )
        bool_pair = (DataType.BOOL, DataType.BOOL)
        allowed = {
            (ExprOp.EQ, bool_pair), (ExprOp.NE, bool_pair),
            (ExprOp.AND, bool_pair), (ExprOp.OR, bool_pair),
            (ExprOp.NOT, (DataType.BOOL,)),
        }
        allowed.update((op, pair) for op in (ExprOp.EQ, ExprOp.NE, ExprOp.LT, ExprOp.LE, ExprOp.GT, ExprOp.GE)
                       for pair in ordered_pairs)
        for op in ExprOp:
            for arity in range(4):
                for operands in product(DataType, repeat=arity):
                    with self.subTest(op=op, operands=operands):
                        expected = DataType.BOOL if (op, operands) in allowed else None
                        self.assertIs(resolve_result_type(op, operands), expected)

    def test_longer_operand_tuple_is_not_accepted(self) -> None:
        """运算符不能接收四个操作数。"""
        for op in ExprOp:
            with self.subTest(op=op):
                self.assertIsNone(resolve_result_type(op, (DataType.INT,) * 4))

    def test_invalid_api_arguments_are_plan_errors(self) -> None:
        """类型规则函数的编程参数错误使用 PLAN 阶段。"""
        cases = (
            ("EQ", (DataType.INT, DataType.INT), "op"),
            (None, (), "op"),
            (True, (), "op"),
            (ExprOp.EQ, [DataType.INT, DataType.INT], "operand_types"),
            (ExprOp.EQ, None, "operand_types"),
            (ExprOp.EQ, (DataType.INT, "INT"), "operand_types[1]"),
            (ExprOp.NOT, (bool,), "operand_types[0]"),
            (ExprOp.NOT, (True,), "operand_types[0]"),
            # 即使元数错误，非枚举元素仍是参数错误，不能被提前返回 None 掩盖。
            (ExprOp.NOT, (DataType.BOOL, "BOOL"), "operand_types[1]"),
        )
        for op, operands, field in cases:
            with self.subTest(op=op, operands=operands):
                with self.assertRaises(DbError) as raised:
                    resolve_result_type(op, operands)
                error = raised.exception
                self.assertEqual(error.code, INVALID_ARGUMENT)
                self.assertIs(error.stage, ErrorStage.PLAN)
                self.assertIsNone(error.span)
                self.assertEqual(error.context["operation"], "resolve_result_type")
                self.assertEqual(error.context["field"], field)
                json.dumps(error.context, ensure_ascii=False)

    def test_shared_enum_names_and_values_match_the_contract(self) -> None:
        """公共枚举的名字和值与工作计划保持一致。"""
        self.assertEqual({item.name: item.value for item in DataType}, {
            "INT": "INT", "VARCHAR": "VARCHAR", "BOOL": "BOOL", "DATE": "DATE", "DECIMAL": "DECIMAL",
        })
        self.assertEqual({item.name: item.value for item in ExprOp}, {
            name: name for name in ("EQ", "NE", "LT", "LE", "GT", "GE", "AND", "OR", "NOT")
        })


if __name__ == "__main__":
    unittest.main()
