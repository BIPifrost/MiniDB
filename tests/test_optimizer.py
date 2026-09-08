"""Optimizer 交接测试：验证 optimizer.py 与 Zhang 的 Bound/Plan 及 Executor 约定。"""

import unittest

from fixtures.contracts import STUDENT_TABLE
from minidb.compiler.bound import BoundBinary, BoundColumn, BoundLiteral
from minidb.compiler.optimizer import Optimizer
from minidb.compiler.plan import DeletePlan, FilterPlan, ProjectPlan, SeqScanPlan
from minidb.core.expressions import ExprOp
from minidb.core.result import ResultColumn
from minidb.core.schema import DataType
from minidb.core.source import SourcePos, SourceSpan


def span(start: int, end: int) -> SourceSpan:
    return SourceSpan(SourcePos(1, start + 1, start), SourcePos(1, end + 1, end), "<test>")


class OptimizerTests(unittest.TestCase):
    """固定计划结构，验证结果等价、原计划不变和执行器可接受。"""

    def setUp(self):
        self.span = span(0, 1)
        self.scan = SeqScanPlan(STUDENT_TABLE, self.span)
        columns = STUDENT_TABLE.schema.columns
        self.project = lambda child: ProjectPlan(
            child,
            (1,),
            (ResultColumn(columns[1].name, columns[1].data_type),),
            self.span,
        )

    def test_constant_comparison_and_boolean_identity_are_folded(self):
        # 验证 optimizer.py 的常量比较折叠和 True AND x 规则。
        always_true = BoundBinary(
            ExprOp.EQ,
            BoundLiteral(1, DataType.INT, self.span),
            BoundLiteral(1, DataType.INT, self.span),
            DataType.BOOL,
            self.span,
            self.span,
        )
        age_check = BoundBinary(
            ExprOp.GE,
            BoundColumn(2, DataType.INT, self.span),
            BoundLiteral(18, DataType.INT, self.span),
            DataType.BOOL,
            self.span,
            self.span,
        )
        original_predicate = BoundBinary(
            ExprOp.AND, always_true, age_check, DataType.BOOL, self.span, self.span
        )
        original = self.project(FilterPlan(self.scan, original_predicate, self.span))

        optimized = Optimizer().optimize(original)

        self.assertIsInstance(optimized, ProjectPlan)
        self.assertIsInstance(optimized.child, FilterPlan)
        self.assertEqual(optimized.child.predicate, age_check)
        # 原计划仍保留完整的常量条件，供 trace 展示且不被原地修改。
        self.assertIs(original.child.predicate, original_predicate)
        self.assertIsInstance(original.child.predicate.left, BoundBinary)

    def test_true_filter_is_removed_from_project(self):
        # 验证恒真 Filter 消除后仍是 Executor 接受的 Project(SeqScan) 结构。
        predicate = BoundBinary(
            ExprOp.EQ,
            BoundLiteral(1, DataType.INT, self.span),
            BoundLiteral(1, DataType.INT, self.span),
            DataType.BOOL,
            self.span,
            self.span,
        )
        optimized = Optimizer().optimize(
            self.project(FilterPlan(self.scan, predicate, self.span))
        )
        self.assertIsInstance(optimized, ProjectPlan)
        self.assertIsInstance(optimized.child, SeqScanPlan)

    def test_false_condition_remains_a_filter(self):
        # False 条件不能删除 Filter，否则会把“无结果”错误变成“全表结果”。
        predicate = BoundBinary(
            ExprOp.EQ,
            BoundLiteral(1, DataType.INT, self.span),
            BoundLiteral(2, DataType.INT, self.span),
            DataType.BOOL,
            self.span,
            self.span,
        )
        optimized = Optimizer().optimize(
            self.project(FilterPlan(self.scan, predicate, self.span))
        )
        self.assertIsInstance(optimized.child, FilterPlan)
        self.assertEqual(optimized.child.predicate.value, False)

    def test_delete_keeps_scan_and_removes_only_true_filter(self):
        # 验证 Delete 不会生成 Project，仍保留扫描结构供 Executor 使用 RowId。
        predicate = BoundBinary(
            ExprOp.EQ,
            BoundLiteral(1, DataType.INT, self.span),
            BoundLiteral(1, DataType.INT, self.span),
            DataType.BOOL,
            self.span,
            self.span,
        )
        optimized = Optimizer().optimize(
            DeletePlan(self.scan.table, FilterPlan(self.scan, predicate, self.span), self.span)
        )
        self.assertIsInstance(optimized, DeletePlan)
        self.assertIsInstance(optimized.child, SeqScanPlan)


if __name__ == "__main__":
    unittest.main()
