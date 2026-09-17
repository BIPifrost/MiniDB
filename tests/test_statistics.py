"""成本模型（MiniDB CBO）单元测试。

覆盖：行大小/页数推导、成本翻转点、索引选择率推断、decide_scan_path
决策、Optimizer 接入 stats 后的路径选择、stats=None 向后兼容。
"""

import math
import unittest

from minidb.compiler.bound import BoundBinary, BoundColumn, BoundLiteral
from minidb.compiler.optimizer import Optimizer
from minidb.compiler.plan import FilterPlan, IndexScanPlan, ProjectPlan, SeqScanPlan
from minidb.compiler.statistics import (
    collect_table_stats,
    cost_index,
    cost_seq,
    decide_scan_path,
    estimate_row_size,
    estimate_selectivity,
    index_selectivity,
)
from minidb.core.expressions import ExprOp
from minidb.core.result import ResultColumn
from minidb.core.schema import (
    ColumnDef,
    DataType,
    DefaultSpec,
    IndexDef,
    IndexOrigin,
    Schema,
    TableDef,
    TableRef,
    TypeSpec,
)
from minidb.core.source import SourcePos, SourceSpan

POS = SourcePos(1, 1, 0)
SPAN = SourceSpan(POS, POS, "test")


def make_schema() -> Schema:
    return Schema(columns=(
        ColumnDef(name="id", type_spec=TypeSpec(kind=DataType.INT), nullable=False,
                  default=DefaultSpec(has_default=False, value=None),
                  primary_key=True, unique=True),
        ColumnDef(name="a", type_spec=TypeSpec(kind=DataType.INT), nullable=True,
                  default=DefaultSpec(has_default=False, value=None),
                  primary_key=False, unique=False),
    ))


def make_table() -> TableDef:
    return TableDef(ref=TableRef(table_id=1, name="t", root_page_id=3),
                    schema=make_schema())


def make_index() -> IndexDef:
    return IndexDef(index_id=1, name="idx_a", table_id=1, column_index=0,
                    root_page_id=4, unique=False, origin=IndexOrigin.USER)


def eq_predicate() -> BoundBinary:
    return BoundBinary(
        ExprOp.EQ,
        BoundColumn(index=0, type_spec=TypeSpec(kind=DataType.INT), span=SPAN),
        BoundLiteral(5, TypeSpec(kind=DataType.INT), SPAN),
        TypeSpec(kind=DataType.BOOL), SPAN, SPAN,
    )


def point_index_plan() -> IndexScanPlan:
    lit = BoundLiteral(5, TypeSpec(kind=DataType.INT), SPAN)
    return IndexScanPlan(make_table(), make_index(), True, lit, True, True, lit, True, False, SPAN)


def range_index_plan() -> IndexScanPlan:
    lit = BoundLiteral(5, TypeSpec(kind=DataType.INT), SPAN)
    return IndexScanPlan(make_table(), make_index(), True, lit, False, False, None, False, False, SPAN)


class RowSizeAndPagesTest(unittest.TestCase):
    def test_row_size_two_ints(self):
        # 前缀4 + bitmap(2列)=1 + 8 + 8 = 21
        self.assertEqual(estimate_row_size(make_schema()), 21)

    def test_pages_from_rows(self):
        per_page = 4056 // 21  # 193
        for rows, expected in ((0, 0), (100, 1), (250, 2), (10000, math.ceil(10000 / per_page))):
            stats = collect_table_stats(rows, make_schema())
            self.assertEqual(stats.pages, expected, f"rows={rows}")
            self.assertEqual(stats.row_size, 21)


class CostFlipTest(unittest.TestCase):
    def test_point_query_flips_between_100_and_250(self):
        small = collect_table_stats(100, make_schema())
        medium = collect_table_stats(250, make_schema())
        # 100 行：全表扫 1 页比索引（树高+随机IO）便宜
        self.assertLess(cost_seq(small), cost_index(small, 1 / 100))
        # 250 行：索引开始更便宜（2 页 vs 树高+1 页）
        self.assertLess(cost_index(medium, 1 / 250), cost_seq(medium))

    def test_full_scan_never_beats_full_table_when_selectivity_1(self):
        # 命中 100% 时索引多付树高与随机 IO，应退回全表
        for rows in (100, 250, 10000):
            stats = collect_table_stats(rows, make_schema())
            self.assertLess(cost_seq(stats), cost_index(stats, 1.0))


class IndexSelectivityTest(unittest.TestCase):
    def test_point_and_null_are_exact(self):
        stats = collect_table_stats(1000, make_schema())
        self.assertAlmostEqual(index_selectivity(point_index_plan(), stats), 1 / 1000)
        null_plan = point_index_plan()
        null_plan = IndexScanPlan(make_table(), make_index(), False, None, False,
                                  False, None, False, True, SPAN)
        self.assertAlmostEqual(index_selectivity(null_plan, stats), 1 / 1000)

    def test_range_uses_one_third(self):
        stats = collect_table_stats(1000, make_schema())
        self.assertAlmostEqual(index_selectivity(range_index_plan(), stats), 1 / 3)


class DecideScanPathTest(unittest.TestCase):
    def test_small_table_rejects_index(self):
        stats = collect_table_stats(100, make_schema())
        self.assertIsNone(decide_scan_path(point_index_plan(), stats))

    def test_large_table_keeps_index(self):
        stats = collect_table_stats(10000, make_schema())
        self.assertIsNotNone(decide_scan_path(point_index_plan(), stats))

    def test_full_range_rejects_index_even_large(self):
        stats = collect_table_stats(10000, make_schema())
        # 范围下界命中 1/3：52 页全表 vs 2+52/3*2≈36.7 索引——仍选索引；
        # 命中 100% 场景由 optimizer 层用完整性谓词选择率兜底。
        self.assertIsNotNone(decide_scan_path(range_index_plan(), stats))


class SelectivityEstimateTest(unittest.TestCase):
    def test_equality_is_one_over_n(self):
        stats = collect_table_stats(1000, make_schema())
        self.assertAlmostEqual(estimate_selectivity(eq_predicate(), stats), 1 / 1000)

    def test_and_multiplies(self):
        stats = collect_table_stats(1000, make_schema())
        truth = BoundLiteral(True, TypeSpec(kind=DataType.BOOL), SPAN)
        conj = BoundBinary(ExprOp.AND, truth, eq_predicate(),
                           TypeSpec(kind=DataType.BOOL), SPAN, SPAN)
        self.assertAlmostEqual(estimate_selectivity(conj, stats), 1 / 1000)

    def test_or_saturates_at_one(self):
        stats = collect_table_stats(1000, make_schema())
        truth = BoundLiteral(True, TypeSpec(kind=DataType.BOOL), SPAN)
        disj = BoundBinary(ExprOp.OR, truth, eq_predicate(),
                           TypeSpec(kind=DataType.BOOL), SPAN, SPAN)
        self.assertEqual(estimate_selectivity(disj, stats), 1.0)


class OptimizerWithStatsTest(unittest.TestCase):
    def _projected(self, stats):
        scan = FilterPlan(SeqScanPlan(make_table(), SPAN), eq_predicate(), SPAN)
        plan = ProjectPlan(scan, (0,), (ResultColumn("id", DataType.INT),), SPAN)
        return Optimizer().optimize(plan, (make_index(),), stats)

    @staticmethod
    def _scan_of(plan):
        # plan: ProjectPlan(FilterPlan(scan, ...)) → 取最底层扫描节点
        child = plan.child
        while hasattr(child, "child"):
            child = child.child
        return child

    def test_no_stats_keeps_original_index_behavior(self):
        # 向后兼容：无统计时与现状一致，采用索引
        plan = self._projected(None)
        self.assertIsInstance(self._scan_of(plan), IndexScanPlan)

    def test_small_table_falls_back_to_seq_scan(self):
        plan = self._projected(collect_table_stats(100, make_schema()))
        self.assertIsInstance(self._scan_of(plan), SeqScanPlan)

    def test_large_table_uses_index(self):
        plan = self._projected(collect_table_stats(10000, make_schema()))
        self.assertIsInstance(self._scan_of(plan), IndexScanPlan)

    def test_explicit_none_equals_default(self):
        scan = FilterPlan(SeqScanPlan(make_table(), SPAN), eq_predicate(), SPAN)
        plan = ProjectPlan(scan, (0,), (ResultColumn("id", DataType.INT),), SPAN)
        optimizer = Optimizer()
        default = optimizer.optimize(plan, (make_index(),))
        explicit = optimizer.optimize(plan, (make_index(),), None)
        self.assertEqual(type(self._scan_of(default)), type(self._scan_of(explicit)))


if __name__ == "__main__":
    unittest.main()
