"""张振的位置校验测试：数字规则、父子位置传递和正式位置对象的入口复核。

本文件不定义 SourcePos/SourceSpan。调用传递测试只用位置标记观察父子关系，
不能作为真实源码位置已验证的证据。
"""

import unittest
from dataclasses import replace
from unittest.mock import call, patch

from fixtures.contracts import CASE_SQL, STUDENT_TABLE, expected_bound, expected_plan, span
from minidb.compiler._checks import Check, _positions_in_order
from minidb.compiler.bound import BoundBinary, BoundColumn, BoundLiteral, BoundSelect, BoundUnary
from minidb.compiler.bound_validation import validate_bound
from minidb.compiler.plan import FilterPlan, ProjectPlan, SeqScanPlan, validate_plan
from minidb.core.errors import INVALID_ARGUMENT, INVALID_PLAN, DbError, ErrorStage
from minidb.core.expressions import ExprOp
from minidb.core.result import ResultColumn
from minidb.core.schema import DataType


class PositionRulesTests(unittest.TestCase):
    """只验证张振的坐标判断和调用传递，不伪造队友的位置类型。"""

    def test_character_offsets_allow_empty_spans_lf_and_crlf(self):
        """位置按字符计数；空范围、LF 和 CRLF 都有合法的表示。"""
        for start, end in (
            ((1, 1, 0), (1, 1, 0)),
            ((1, 1, 0), (1, 4, len("中\t😀"))),
            ((2, 3, 8), (2, 6, 11)),
            ((1, 4, 3), (2, 1, 4)),
            ((1, 4, 3), (2, 1, 5)),
            ((2, 7, 10), (4, 3, 14)),
        ):
            with self.subTest(start=start, end=end):
                self.assertTrue(_positions_in_order(start, end))

    def test_inconsistent_line_column_and_offset_are_rejected(self):
        """拒绝倒序、相同偏移却不同行列，以及把字节数当作字符偏移的情况。"""
        for start, end in (
            ((1, 4, 3), (1, 2, 1)),
            ((2, 1, 5), (1, 8, 7)),
            ((1, 4, 3), (2, 1, 3)),
            ((1, 1, 0), (1, 4, len("中\t😀".encode("utf-8")))),
            ((1, 1, 0), (3, 4, 4)),
            ((2, 1, 0), (2, 2, 1)),
            ((1, 1, 10), (1, 2, 11)),
        ):
            with self.subTest(start=start, end=end):
                self.assertFalse(_positions_in_order(start, end))

    def test_bound_and_plan_forward_every_parent_span(self):
        """观察校验调用：共享节点复用内部结果时，两条父子关系仍分别检查。"""
        statement_span, condition_span, shared_span, scan_span = (object() for _ in range(4))
        shared = BoundBinary(
            ExprOp.GE, BoundColumn(2, DataType.INT, shared_span),
            BoundLiteral(18, DataType.INT, shared_span), DataType.BOOL, shared_span, shared_span,
        )
        predicate = BoundBinary(ExprOp.AND, shared, shared, DataType.BOOL, condition_span, condition_span)
        columns = (ResultColumn("name", DataType.VARCHAR),)
        bound = BoundSelect(STUDENT_TABLE, (1,), columns, predicate, statement_span)
        with patch.object(Check, "span") as check_span:
            validate_bound(bound, plan=True)
        self.assertIn(call(condition_span, parent=statement_span), check_span.call_args_list)
        self.assertEqual(check_span.call_args_list.count(call(shared_span, parent=condition_span)), 2)
        self.assertIn(call(shared_span, parent=shared_span), check_span.call_args_list)

        filtered = FilterPlan(SeqScanPlan(STUDENT_TABLE, scan_span), predicate, condition_span)
        with patch.object(Check, "span") as check_span:
            validate_plan(ProjectPlan(filtered, (1,), columns, statement_span))
        self.assertIn(call(condition_span, parent=statement_span), check_span.call_args_list)
        self.assertIn(call(scan_span, parent=condition_span), check_span.call_args_list)
        self.assertIn(call(condition_span, parent=condition_span), check_span.call_args_list)


class SourceValidationTests(unittest.TestCase):
    """使用正式位置对象，检查范围错误在 Bound 和 Plan 入口都被拒绝。"""

    def setUp(self):
        """每个用例的位置都来自同一条共享 SELECT SQL。"""
        self.sql = CASE_SQL["select"]
        self.bound = expected_bound("select")
        self.plan = expected_plan("select")

    def assert_plan_error(self, action):
        """位置属于计划结构的一部分，错误仍使用 PLAN/INVALID_PLAN。"""
        with self.assertRaises(DbError) as raised:
            action()
        self.assertEqual(raised.exception.code, INVALID_PLAN)
        self.assertIs(raised.exception.stage, ErrorStage.PLAN)

    def test_span_rejects_inconsistent_coordinates_in_both_stages(self):
        """故意破坏正式冻结对象，确保张振入口会复核队友传入的位置。"""
        for start, end in (((1, 5, 4), (1, 3, 6)), ((2, 1, 5), (1, 8, 7)),
                           ((1, 1, 0), (2, 1, 0)), ((1, 1, 10), (1, 2, 11))):
            bad = span(self.sql)
            for position, numbers in ((bad.start, start), (bad.end, end)):
                for field, number in zip(("line", "column", "offset"), numbers):
                    object.__setattr__(position, field, number)
            for plan_mode in (False, True):
                with self.subTest(start=start, end=end, plan=plan_mode):
                    with self.assertRaises(DbError) as raised:
                        Check("position_test", plan=plan_mode).span(bad)
                    self.assertEqual(raised.exception.code, INVALID_PLAN if plan_mode else INVALID_ARGUMENT)
        # 合法范围和位于其内部的列名也必须继续通过。
        Check("position_test").span(span(self.sql, "age"), parent=span(self.sql))

    def test_bound_rejects_foreign_and_out_of_parent_positions(self):
        """列名位置不能来自另一输入或跑到 WHERE 外；整个条件也不能超出语句。"""
        original = self.bound.predicate
        for bad_span in (replace(original.left.span, source_name="other.sql"), span(self.sql, "student")):
            bad = replace(original, left=replace(original.left, span=bad_span))
            self.assert_plan_error(lambda: validate_bound(replace(self.bound, predicate=bad), plan=True))
        self.assert_plan_error(lambda: validate_bound(replace(self.bound, span=span(self.sql, "SELECT")), plan=True))

    def test_plan_checks_scan_filter_and_predicate_positions(self):
        """扫描、过滤和条件位置都要属于父节点，不能拼接另一语句的计划。"""
        filtered = self.plan.child
        bad_filters = (
            replace(filtered, span=replace(filtered.span, source_name="other.sql")),
            replace(filtered, child=replace(filtered.child, span=replace(filtered.child.span, source_name="other.sql"))),
            replace(filtered, span=span(self.sql, "SELECT"), child=replace(filtered.child, span=span(self.sql, "SELECT"))),
        )
        for child in bad_filters:
            self.assert_plan_error(lambda: validate_plan(replace(self.plan, child=child)))

    def test_shared_subtree_is_checked_against_each_parent(self):
        """共享子树第一次位置合法，挂到第二个较小的父范围下仍需报错。"""
        shared = self.bound.predicate
        # 手工构造结构损坏的 Bound；不会将它视为 Parser 的合法输出。
        narrow = span(self.sql, "18")
        second = BoundUnary(ExprOp.NOT, shared, DataType.BOOL, narrow, narrow)
        root = BoundBinary(ExprOp.AND, shared, second, DataType.BOOL, shared.op_span, shared.span)
        self.assert_plan_error(lambda: validate_bound(replace(self.bound, predicate=root), plan=True))


if __name__ == "__main__":
    unittest.main()
