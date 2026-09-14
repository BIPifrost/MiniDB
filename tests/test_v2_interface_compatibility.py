"""新Bound/Plan接口对已有优化器的兼容回归；不提供队友缺失的执行功能。"""
import unittest
from dataclasses import replace

from minidb.compiler.bound import BoundColumn, BoundLiteral, BoundBinary, BoundIsNull, BoundAssignment
from minidb.compiler.optimizer import Optimizer
from minidb.compiler.plan import (
    SeqScanPlan, FilterPlan, ProjectPlan, InsertPlan, UpdatePlan,
    CreateIndexPlan, DescribePlan, ExplainPlan, validate_plan,
)
from minidb.catalog.catalog_rows import catalog_from_rows
from minidb.core.errors import DbError, ErrorStage
from minidb.core.expressions import ExprOp
from minidb.core.schema import TypeSpec, DataType, ColumnDef, Schema, TableDef, TableRef
from minidb.core.source import SourceSpan, SourcePos
from minidb.core.result import ResultColumn

SPAN = SourceSpan(SourcePos(1, 1, 0), SourcePos(1, 101, 100), "<interface>")
INT, BOOL = TypeSpec(DataType.INT), TypeSpec(DataType.BOOL)


class InterfaceCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.table = TableDef(TableRef(1, "users", 3), Schema((ColumnDef("id", INT),)))
        self.column = BoundColumn(0, INT, SPAN, True)

    def project(self, predicate):
        return ProjectPlan(FilterPlan(SeqScanPlan(self.table, SPAN), predicate, SPAN),
                           (0,), (ResultColumn("id", DataType.INT),), SPAN)

    def comparison(self, value):
        return BoundBinary(ExprOp.EQ, self.column, BoundLiteral(value, INT, SPAN),
                           BOOL, SPAN, SPAN, True)

    def test_rebuilt_expression_preserves_nullable(self):
        right = BoundBinary(ExprOp.AND, BoundLiteral(True, BOOL, SPAN), self.comparison(2),
                            BOOL, SPAN, SPAN, True)
        predicate = BoundBinary(ExprOp.OR, self.comparison(1), right, BOOL, SPAN, SPAN, True)
        optimized = Optimizer().optimize(self.project(predicate))
        self.assertTrue(optimized.child.predicate.nullable)
        self.assertEqual(optimized.child.predicate.type_spec, BOOL)
        validate_plan(optimized)

    def test_null_comparison_not_folded_using_python_none(self):
        for op in (ExprOp.EQ, ExprOp.NE, ExprOp.LT, ExprOp.AND):
            with self.subTest(op=op):
                # AND中的NULL已由语义模块绑定为BOOL；NULL=NULL可以保持未定型。
                spec = BOOL if op is ExprOp.AND else None
                predicate = BoundBinary(op, BoundLiteral(None, spec, SPAN), BoundLiteral(None, spec, SPAN),
                                        BOOL, SPAN, SPAN, True)
                optimized = Optimizer().optimize(self.project(predicate))
                self.assertIsInstance(optimized.child, FilterPlan)
                self.assertEqual(optimized.child.predicate, predicate)

    def test_new_expression_is_preserved_without_new_optimization_algorithm(self):
        predicate = BoundIsNull(self.column, False, SPAN, SPAN)
        original = self.project(predicate)
        self.assertEqual(Optimizer().optimize(original), original)

    def test_new_plan_nodes_pass_through_existing_optimizer(self):
        update = UpdatePlan(self.table, SeqScanPlan(self.table, SPAN),
                            (BoundAssignment(0, BoundLiteral(1, INT, SPAN), SPAN),), SPAN)
        for plan in (update, CreateIndexPlan("ix_id", self.table, 0, False, SPAN),
                     DescribePlan(self.table, SPAN), ExplainPlan(update, SPAN)):
            with self.subTest(plan=type(plan).__name__):
                self.assertIs(Optimizer().optimize(plan), plan)

    def test_invalid_plan_uses_registered_plan_error_without_new_business_codes(self):
        for row in ((1 << 63,),):
            with self.assertRaises(DbError) as caught:
                validate_plan(InsertPlan(self.table, row, SPAN))
            self.assertEqual(caught.exception.code, "INVALID_PLAN")
        required = replace(self.table, schema=Schema((ColumnDef("id", INT, nullable=False),)))
        with self.assertRaises(DbError) as caught:
            validate_plan(InsertPlan(required, (None,), SPAN))
        self.assertEqual(caught.exception.code, "INVALID_PLAN")

    def test_corrupt_catalog_uses_catalog_error_without_new_business_codes(self):
        # INT列非法携带长度参数；错误应稳定归类为目录损坏。
        row = (1, "users", 3, 1, 0, "id", "INT", 10, -1, -1,
               True, False, False, "NONE", "")
        with self.assertRaises(DbError) as caught:
            catalog_from_rows((row,))
        self.assertEqual(caught.exception.code, "CATALOG_CORRUPTED")

    def test_catalog_stops_at_table_limit_without_consuming_entire_input(self):
        consumed = []
        def rows():
            for i in range(1, 131):
                consumed.append(i)
                yield (i, "table_" + str(i), i + 2, 1, 0, "id", "INT", -1, -1, -1,
                       True, False, False, "NONE", "")
        with self.assertRaises(DbError) as caught:
            catalog_from_rows(rows())
        self.assertEqual(caught.exception.code, "CATALOG_CORRUPTED")
        self.assertEqual(len(consumed), 129)

    def test_catalog_preserves_input_errors(self):
        for original in (NotImplementedError("尚未提供扫描接口"),
                         DbError(ErrorStage.STORAGE, "IO_READ_FAILED", "读取失败")):
            def broken_rows():
                raise original
                yield  # 保持生成器形状，让错误发生在读取时。
            with self.assertRaises(type(original)) as caught:
                catalog_from_rows(broken_rows())
            self.assertIs(caught.exception, original)


if __name__ == "__main__":
    unittest.main()
