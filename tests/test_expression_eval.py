"""绑定表达式的运行时求值和短路行为测试。"""

import unittest

from minidb.compiler.bound import BoundBinary, BoundColumn, BoundLiteral, BoundUnary
from minidb.core.errors import INVALID_ARGUMENT, INVALID_PLAN, DbError
from minidb.core.expressions import ExprOp
from minidb.core.schema import DataType
from minidb.engine.expression_eval import evaluate
from tests.fixtures.contracts import span


class ExpressionEvalTests(unittest.TestCase):
    def setUp(self):
        self.location = span("SELECT name FROM student WHERE age >= 18;")

    def literal(self, value, data_type):
        return BoundLiteral(value, data_type, self.location)

    def binary(self, op, left, right):
        return BoundBinary(
            op,
            left,
            right,
            DataType.BOOL,
            self.location,
            self.location,
        )

    def assert_error(self, code, action):
        with self.assertRaises(DbError) as raised:
            action()
        self.assertEqual(raised.exception.code, code)
        return raised.exception

    def test_reads_columns_and_literals_without_looking_up_names(self):
        row = (1, "Alice", 20)
        self.assertEqual(
            evaluate(BoundColumn(1, DataType.VARCHAR, self.location), row),
            "Alice",
        )
        self.assertEqual(evaluate(self.literal(18, DataType.INT), row), 18)
        self.assertIs(evaluate(self.literal(True, DataType.BOOL), row), True)

    def test_all_supported_comparisons(self):
        cases = (
            (ExprOp.EQ, 2, 2, True),
            (ExprOp.NE, "a", "b", True),
            (ExprOp.LT, 1, 2, True),
            (ExprOp.LE, 2, 2, True),
            (ExprOp.GT, 3, 2, True),
            (ExprOp.GE, 2, 2, True),
        )
        for op, left, right, expected in cases:
            data_type = DataType.VARCHAR if type(left) is str else DataType.INT
            with self.subTest(op=op):
                expr = self.binary(
                    op,
                    self.literal(left, data_type),
                    self.literal(right, data_type),
                )
                self.assertIs(evaluate(expr, ()), expected)

    def test_not_and_nested_boolean_operations(self):
        true_value = self.literal(True, DataType.BOOL)
        false_value = self.literal(False, DataType.BOOL)
        negated = BoundUnary(
            ExprOp.NOT,
            false_value,
            DataType.BOOL,
            self.location,
            self.location,
        )
        expression = self.binary(
            ExprOp.AND,
            negated,
            self.binary(ExprOp.OR, false_value, true_value),
        )
        self.assertIs(evaluate(expression, ()), True)

    def test_and_or_short_circuit_the_unused_right_branch(self):
        # 右侧列号明显越界；若错误地求值它，就会抛 INVALID_PLAN。
        bad_right = BoundColumn(99, DataType.BOOL, self.location)
        false_and = self.binary(
            ExprOp.AND,
            self.literal(False, DataType.BOOL),
            bad_right,
        )
        true_or = self.binary(
            ExprOp.OR,
            self.literal(True, DataType.BOOL),
            bad_right,
        )
        self.assertIs(evaluate(false_and, ()), False)
        self.assertIs(evaluate(true_or, ()), True)

    def test_invalid_direct_calls_report_structured_errors(self):
        self.assert_error(INVALID_ARGUMENT, lambda: evaluate(self.literal(1, DataType.INT), [1]))
        self.assert_error(
            INVALID_PLAN,
            lambda: evaluate(BoundColumn(2, DataType.INT, self.location), (1,)),
        )
        self.assert_error(
            INVALID_PLAN,
            lambda: evaluate(self.literal(True, DataType.INT), ()),
        )


if __name__ == "__main__":
    unittest.main()
