"""张振的 Bound/Plan 边界测试：使用正式节点、类型规则和 DbError。

这些测试检查结构和类型，不执行 SQL、不使用存储，也不实现其他人的接口。
位置使用正式 SourceSpan，入口执行真实的位置和父子包含关系检查。
"""

import json
import unittest
from dataclasses import replace
from unittest.mock import patch

from fixtures.contracts import STUDENT_SCHEMA, STUDENT_TABLE
from minidb.catalog.catalog import SYSTEM_CATALOG_TABLE
from minidb.compiler.bound import (
    BoundBinary, BoundColumn, BoundCreate, BoundDelete, BoundInsert,
    BoundLiteral, BoundSelect, BoundUnary,
)
from minidb.compiler.bound_validation import validate_bound
from minidb.compiler.plan import (
    CreateTablePlan, DeletePlan, FilterPlan, InsertPlan, ProjectPlan,
    SeqScanPlan, validate_plan,
)
from minidb.compiler.planner import Planner
from minidb.core.errors import DbError, ErrorStage, INVALID_PLAN
from minidb.core.expressions import ExprOp, resolve_result_type
from minidb.core.result import ResultColumn
from minidb.core.schema import DataType, TableDef, TableRef
from minidb.core.source import SourcePos, SourceSpan


class BoundPlanValidationTests(unittest.TestCase):
    """通过公开校验入口和 Planner 验证边界，错误必须使用 PLAN/INVALID_PLAN。"""

    def setUp(self):
        """准备 student 的正式类型，以及供结构边界测试使用的合法位置。"""
        self.span = SourceSpan(SourcePos(1, 1, 0), SourcePos(1, 2, 1), "<validation>")
        self.scan = SeqScanPlan(STUDENT_TABLE, self.span)
        self.columns = (ResultColumn("name", DataType.VARCHAR),)
        # age >= 18，条件使用原表的第 2 列，最终查询只输出第 1 列 name。
        self.condition = BoundBinary(
            ExprOp.GE, BoundColumn(2, DataType.INT, self.span),
            BoundLiteral(18, DataType.INT, self.span), DataType.BOOL,
            self.span, self.span,
        )

    def _selected(self, predicate):
        """用待测条件构造完整 BoundSelect，经过真实 Bound 校验。"""
        return BoundSelect(STUDENT_TABLE, (1,), self.columns, predicate, self.span)

    def _projected(self, child):
        """构造完整查询计划，内部扫描不能直接当公开计划根使用。"""
        return ProjectPlan(child, (1,), self.columns, self.span)

    def assert_invalid_plan(self, action, field):
        """断言正式错误码、阶段和问题字段，上下文必须可以输出为 JSON。"""
        with self.assertRaises(DbError) as raised:
            action()
        error = raised.exception
        self.assertEqual(error.code, INVALID_PLAN)
        self.assertIs(error.stage, ErrorStage.PLAN)
        self.assertEqual(error.context["field"], field)
        self.assertTrue({"operation", "expected", "actual"} <= error.context.keys())
        json.dumps(error.context, ensure_ascii=False)

    def _assert_invalid_condition(self, predicate, field):
        """相同坏条件在 Planner 的 Bound 入口和最终 Plan 入口都应被拒绝。"""
        self.assert_invalid_plan(lambda: Planner().build(self._selected(predicate)), field)
        filtered = FilterPlan(self.scan, predicate, self.span)
        self.assert_invalid_plan(lambda: validate_plan(self._projected(filtered)), field)

    def test_nested_not_keeps_valid_condition_and_column_binding(self):
        """多层 NOT 不丢失内层比较、类型和原表列索引，也不提前求值。"""
        predicate = self.condition
        for _ in range(8):
            predicate = BoundUnary(ExprOp.NOT, predicate, DataType.BOOL, self.span, self.span)
        plan = Planner().build(self._selected(predicate))
        self.assertEqual(plan.column_indexes, (1,))
        self.assertIs(plan.child.predicate, predicate)
        node = plan.child.predicate
        for _ in range(8):
            self.assertIsInstance(node, BoundUnary)
            self.assertIs(node.op, ExprOp.NOT)
            self.assertIs(node.data_type, DataType.BOOL)
            node = node.operand
        self.assertIs(node, self.condition)
        self.assertEqual(node.left.index, 2)

    def test_not_requires_boolean_operand_at_every_level(self):
        """外层 NOT 不能掩盖内层 NOT 的整数或字符串操作数错误。"""
        for literal in (BoundLiteral(1, DataType.INT, self.span),
                        BoundLiteral("yes", DataType.VARCHAR, self.span)):
            with self.subTest(data_type=literal.data_type):
                inner = BoundUnary(ExprOp.NOT, literal, DataType.BOOL, self.span, self.span)
                outer = BoundUnary(ExprOp.NOT, inner, DataType.BOOL, self.span, self.span)
                self._assert_invalid_condition(outer, "predicate.data_type")

    def test_and_or_validate_both_sides_despite_constant_result(self):
        """即使遇到 FALSE AND 或 TRUE OR，另一侧的坏列索引和类型仍须检查。"""
        invalid_conditions = (
            (replace(self.condition, left=BoundColumn(3, DataType.INT, self.span)), "predicate.index"),
            (replace(self.condition, left=BoundColumn(2, DataType.VARCHAR, self.span)), "predicate.data_type"),
        )
        for op, value in ((ExprOp.AND, False), (ExprOp.OR, True)):
            for bad, field in invalid_conditions:
                constant = BoundLiteral(value, DataType.BOOL, self.span)
                for left, right in ((constant, bad), (bad, constant)):
                    with self.subTest(op=op, field=field, bad_on_left=left is bad):
                        predicate = BoundBinary(op, left, right, DataType.BOOL, self.span, self.span)
                        self._assert_invalid_condition(predicate, field)

    def test_column_indexes_must_be_in_range_and_exact_integers(self):
        """列索引不能越界，也不能把 bool、浮点数或字符串当作整数。"""
        for index in (-1, 3, True, 2.0, "2", None):
            with self.subTest(index=index):
                bad = replace(self.condition, left=BoundColumn(index, DataType.INT, self.span))
                self._assert_invalid_condition(bad, "predicate.index")

    def test_column_type_must_match_original_schema(self):
        """绑定的 age 类型必须来自原表 INT 定义，不能由节点自行改变。"""
        for data_type in (DataType.VARCHAR, DataType.BOOL, "INT", None):
            with self.subTest(data_type=data_type):
                bad = replace(self.condition, left=BoundColumn(2, data_type, self.span))
                self._assert_invalid_condition(bad, "predicate.data_type")

    def test_literal_values_must_match_type_and_int64_range(self):
        """字面量不隐式转换类型，INT 的两侧越界值都拒绝。"""
        cases = ((True, DataType.INT), ("18", DataType.INT), (18, DataType.VARCHAR),
                 (1, DataType.BOOL), (-(1 << 63) - 1, DataType.INT), (1 << 63, DataType.INT))
        for value, data_type in cases:
            with self.subTest(value=value, data_type=data_type):
                bad = replace(self.condition, right=BoundLiteral(value, data_type, self.span))
                self._assert_invalid_condition(bad, "value")
        for value in (-(1 << 63), (1 << 63) - 1):
            valid = replace(self.condition, right=BoundLiteral(value, DataType.INT, self.span))
            Planner().build(self._selected(valid))

    def test_operator_shape_and_result_type_are_checked(self):
        """NOT 只能是一元节点，比较只能是二元节点，运算结果必须与类型规则一致。"""
        cases = (
            (BoundUnary(ExprOp.EQ, self.condition, DataType.BOOL, self.span, self.span), "predicate.op"),
            (replace(self.condition, op=ExprOp.NOT), "predicate.op"),
            (replace(self.condition, op="GE"), "predicate.op"),
            (replace(self.condition, data_type=DataType.INT), "predicate.data_type"),
            (replace(self.condition, right=BoundLiteral("18", DataType.VARCHAR, self.span)), "predicate.data_type"),
        )
        for predicate, field in cases:
            with self.subTest(field=field, node=type(predicate).__name__):
                self._assert_invalid_condition(predicate, field)

    def test_predicate_requires_bool_or_no_where(self):
        """没有 WHERE 时省略 Filter；裸整数条件或空 Filter 都不是合法计划。"""
        plan = Planner().build(self._selected(None))
        self.assertIsInstance(plan.child, SeqScanPlan)
        self._assert_invalid_condition(BoundLiteral(18, DataType.INT, self.span), "predicate")
        self.assert_invalid_plan(
            lambda: validate_plan(self._projected(FilterPlan(self.scan, None, self.span))), "predicate",
        )

    def test_unknown_expression_node_is_rejected(self):
        """条件必须使用正式 Bound 节点，缺失的子节点也不能继续执行。"""
        for predicate in (object(), replace(self.condition, right=None)):
            self._assert_invalid_condition(predicate, "predicate")

    def test_expression_cycles_are_rejected(self):
        """故意破坏 frozen 对象来模拟坏输入，检查自环和两节点环都能退出报错。"""
        first = BoundUnary(ExprOp.NOT, self.condition, DataType.BOOL, self.span, self.span)
        # 正常业务代码不会这样修改冻结对象；这里只用于触发循环引用边界。
        object.__setattr__(first, "operand", first)
        self._assert_invalid_condition(first, "predicate")
        second = BoundUnary(ExprOp.NOT, first, DataType.BOOL, self.span, self.span)
        object.__setattr__(first, "operand", second)
        self._assert_invalid_condition(first, "predicate")
        # 左分支先通过校验，右分支中的环仍必须被发现。
        mixed = BoundBinary(ExprOp.AND, self.condition, first, DataType.BOOL, self.span, self.span)
        self._assert_invalid_condition(mixed, "predicate")

    def test_shared_expression_subtree_is_not_mistaken_for_cycle(self):
        """左右分支共享同一棵合法子树是允许的，不能被误认为祖先循环。"""
        predicate = BoundBinary(ExprOp.AND, self.condition, self.condition,
                                DataType.BOOL, self.span, self.span)
        plan = Planner().build(self._selected(predicate))
        self.assertIs(plan.child.predicate.left, plan.child.predicate.right)
        validate_plan(plan)

    def test_shared_expression_checks_each_operator_once_per_validation(self):
        """10 层共用子树只有 10 个运算节点，每次校验只需查 10 次类型规则。"""
        predicate = BoundLiteral(True, DataType.BOOL, self.span)
        for _ in range(10):
            predicate = BoundBinary(ExprOp.AND, predicate, predicate,
                                    DataType.BOOL, self.span, self.span)
        bound = self._selected(predicate)
        plan = self._projected(FilterPlan(self.scan, predicate, self.span))
        for action in (lambda: validate_bound(bound, plan=True), lambda: validate_plan(plan)):
            with self.subTest(entry=action):
                # wraps 保留真实类型判断；统计调用次数，避免用机器速度作为测试标准。
                with patch("minidb.compiler.bound_validation.resolve_result_type",
                           wraps=resolve_result_type) as resolve:
                    action()
                self.assertEqual(resolve.call_count, 10)

    def test_validated_nodes_are_not_reused_across_tables_or_calls(self):
        """同一条件换到另一张表必须重新校验，不能沿用上一张表的列类型结论。"""
        bound = self._selected(self.condition)
        validate_bound(bound, plan=True)
        # 另一张表也有 age，但类型是 VARCHAR；原来的 INT 列绑定已不适用。
        columns = STUDENT_SCHEMA.columns
        schema = replace(STUDENT_SCHEMA, columns=columns[:2] + (replace(columns[2], data_type=DataType.VARCHAR),))
        other = TableDef(TableRef(2, "other", 3), schema)
        self.assert_invalid_plan(
            lambda: validate_bound(replace(bound, table=other), plan=True), "predicate.data_type",
        )
        # 上一次失败也不能污染下一次对原表的检查。
        validate_bound(bound, plan=True)

    def test_projection_rejects_invalid_indexes(self):
        """投影必须是非空索引元组，每项都指向原表中存在的列。"""
        for indexes in ((), [1], (-1,), (3,), (True,), (1.0,), ("1",)):
            with self.subTest(indexes=indexes):
                bound = replace(self._selected(None), projection=indexes)
                self.assert_invalid_plan(lambda: Planner().build(bound), "projection")
                plan = replace(self._projected(self.scan), column_indexes=indexes)
                self.assert_invalid_plan(lambda: validate_plan(plan), "projection")

    def test_projection_metadata_matches_indexes_and_preserves_duplicates(self):
        """输出列的数量、名字和类型必须与索引对应；合法的重复投影则保留。"""
        invalid_columns = ((), list(self.columns), ("name",),
                           (ResultColumn("age", DataType.VARCHAR),),
                           (ResultColumn("name", DataType.INT),),
                           (ResultColumn("name", "VARCHAR"),))
        for columns in invalid_columns:
            with self.subTest(columns=columns):
                bound = replace(self._selected(None), output_columns=columns)
                self.assert_invalid_plan(lambda: Planner().build(bound), "output_columns")
                plan = replace(self._projected(self.scan), output_columns=columns)
                self.assert_invalid_plan(lambda: validate_plan(plan), "output_columns")
        bound = replace(self._selected(None), projection=(1, 1), output_columns=self.columns * 2)
        plan = Planner().build(bound)
        self.assertEqual(plan.column_indexes, (1, 1))
        self.assertEqual([column.name for column in plan.output_columns], ["name", "name"])

    def test_plan_cycles_are_rejected(self):
        """Filter 的 child 出现自环或两节点环时报告 INVALID_PLAN。"""
        first = FilterPlan(self.scan, self.condition, self.span)
        object.__setattr__(first, "child", first)
        self.assert_invalid_plan(lambda: validate_plan(self._projected(first)), "child")
        second = FilterPlan(first, self.condition, self.span)
        object.__setattr__(first, "child", second)
        self.assert_invalid_plan(lambda: validate_plan(self._projected(first)), "child")

    def test_only_complete_plans_are_accepted_as_roots(self):
        """SeqScan、Filter、Bound 对象和任意其他对象都不能冒充完整 Plan 根。"""
        for plan in (self.scan, FilterPlan(self.scan, self.condition, self.span),
                     self._selected(None), None, object()):
            with self.subTest(node=type(plan).__name__):
                self.assert_invalid_plan(lambda: validate_plan(plan), "root")

    def test_stream_rejects_projection_or_missing_child(self):
        """内部行流只能来自扫描或过滤，投影结果不能重新用作过滤或删除输入。"""
        for child in (self._projected(self.scan), None):
            plans = (self._projected(child), DeletePlan(STUDENT_TABLE, child, self.span),
                     self._projected(FilterPlan(child, self.condition, self.span)))
            for plan in plans:
                with self.subTest(root=type(plan).__name__, child=type(child).__name__):
                    self.assert_invalid_plan(lambda: validate_plan(plan), "child")

    def test_delete_rejects_scan_of_another_table(self):
        """即使两张表的列结构相同，也不能用另一张表的扫描结果删除当前表。"""
        other = TableDef(TableRef(2, "other", 3), STUDENT_SCHEMA)
        scan = SeqScanPlan(other, self.span)
        for child in (scan, FilterPlan(scan, self.condition, self.span)):
            self.assert_invalid_plan(
                lambda: validate_plan(DeletePlan(STUDENT_TABLE, child, self.span)), "table",
            )

    def test_insert_rows_are_validated_before_building_plan(self):
        """INSERT 的绑定输入和生成计划都拒绝错误长度、可变列表及错误类型。"""
        cases = (([1, "Alice", 20], "row"), ((1, "Alice"), "row"),
                 ((True, "Alice", 20), "row[0]"), ((1, 2, 20), "row[1]"),
                 ((1, "Alice", 1 << 63), "row[2]"))
        for row, field in cases:
            with self.subTest(row=row):
                self.assert_invalid_plan(
                    lambda: Planner().build(BoundInsert(STUDENT_TABLE, row, self.span)), field,
                )
                self.assert_invalid_plan(lambda: validate_plan(InsertPlan(STUDENT_TABLE, row, self.span)), field)

    def test_user_plans_reject_system_table(self):
        """系统目录只能通过目录管理器访问，用户计划不能直接扫描或删除它。"""
        scan = SeqScanPlan(SYSTEM_CATALOG_TABLE, self.span)
        for plan in (self._projected(scan), DeletePlan(SYSTEM_CATALOG_TABLE, scan, self.span)):
            self.assert_invalid_plan(lambda: validate_plan(plan), "table")

    def test_planner_builds_all_statement_types_with_real_validation(self):
        """四类 Bound 均经过真实结构校验；仅源码位置依赖可能被隔离。"""
        cases = (
            (BoundCreate("course", STUDENT_SCHEMA, self.span), CreateTablePlan),
            (BoundInsert(STUDENT_TABLE, (1, "Alice", 20), self.span), InsertPlan),
            (self._selected(self.condition), ProjectPlan),
            (BoundDelete(STUDENT_TABLE, self.condition, self.span), DeletePlan),
        )
        for bound, expected_type in cases:
            with self.subTest(bound=type(bound).__name__):
                validate_bound(bound, plan=True)
                plan = Planner().build(bound)
                self.assertIsInstance(plan, expected_type)
                validate_plan(plan)


if __name__ == "__main__":
    unittest.main()
