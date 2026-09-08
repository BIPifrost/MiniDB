"""Parser 交接测试：验证 parser.py 与 Lexer、AST、Semantic 的公共接口。"""

import unittest

from minidb.compiler.ast import (
    BinaryExpr,
    CreateTableStmt,
    DeleteStmt,
    InsertStmt,
    LiteralExpr,
    SelectStmt,
    UnaryExpr,
)
from minidb.compiler.lexer import Lexer
from minidb.compiler.parser import Parser
from minidb.core.errors import (
    DbError,
    ErrorStage,
    INT_OUT_OF_RANGE,
    UNEXPECTED_TOKEN,
    UNSUPPORTED_FEATURE,
)
from minidb.core.expressions import ExprOp
from minidb.core.schema import DataType
from minidb.core.source import SourceText


def parse(sql: str):
    source = SourceText("<test>", sql)
    return list(Parser().iter_statements(Lexer().scan(source)))


class ParserStatementTests(unittest.TestCase):
    """验证四类 AST 语句及其字段顺序，供 Semantic 直接消费。"""

    def test_four_statement_kinds(self):
        statements = parse(
            "CREATE TABLE student(id INT, name VARCHAR);"
            "INSERT INTO student(id,name) VALUES (1,'Alice');"
            "SELECT name FROM student WHERE id = 1;"
            "DELETE FROM student WHERE id = 1;"
        )
        self.assertEqual(
            [type(statement) for statement in statements],
            [CreateTableStmt, InsertStmt, SelectStmt, DeleteStmt],
        )
        self.assertEqual(statements[0].columns[0].data_type, DataType.INT)
        self.assertEqual(statements[1].values[0], LiteralExpr(1, DataType.INT, statements[1].values[0].span))
        self.assertEqual(statements[2].columns[0].text, "name")
        self.assertIsInstance(statements[3].where, BinaryExpr)

    def test_negative_integer_and_string_are_preserved(self):
        statement = parse("INSERT INTO t(id,name) VALUES (-9223372036854775808,'MiXeD');")[0]
        self.assertEqual(statement.values[0].value, -9223372036854775808)
        self.assertEqual(statement.values[1].value, "MiXeD")
        self.assertEqual(statement.values[0].data_type, DataType.INT)
        self.assertEqual(statement.values[1].data_type, DataType.VARCHAR)

    def test_expression_precedence_and_not(self):
        statement = parse(
            "SELECT * FROM t WHERE a = 1 OR b = 2 AND NOT (c = 3);"
        )[0]
        expression = statement.where
        self.assertIsInstance(expression, BinaryExpr)
        self.assertIs(expression.op, ExprOp.OR)
        self.assertIsInstance(expression.right, BinaryExpr)
        self.assertIs(expression.right.op, ExprOp.AND)
        self.assertIsInstance(expression.right.right, UnaryExpr)
        self.assertIs(expression.right.right.op, ExprOp.NOT)

    def test_statement_span_includes_semicolon(self):
        statement = parse("SELECT id FROM t;")[0]
        self.assertEqual(statement.span.start.offset, 0)
        self.assertEqual(statement.span.end.offset, len("SELECT id FROM t;"))
        self.assertEqual(statement.table_name.span.start.offset, 15)


class ParserErrorTests(unittest.TestCase):
    """验证 Parser 使用统一 SYNTAX 错误，并区分不支持功能。"""

    def assert_syntax_error(self, sql: str, code: str):
        with self.assertRaises(DbError) as caught:
            parse(sql)
        self.assertIs(caught.exception.stage, ErrorStage.SYNTAX)
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def test_decimal_is_unsupported_after_lexing(self):
        error = self.assert_syntax_error("SELECT 3.14 FROM t;", UNSUPPORTED_FEATURE)
        self.assertEqual(error.context["feature"], "3.14")

    def test_arithmetic_is_unsupported(self):
        error = self.assert_syntax_error("SELECT id FROM t WHERE id + 1;", UNSUPPORTED_FEATURE)
        self.assertEqual(error.context["feature"], "+")

    def test_integer_range_is_checked_without_python_overflow(self):
        error = self.assert_syntax_error(
            "INSERT INTO t(id) VALUES (9223372036854775808);",
            INT_OUT_OF_RANGE,
        )
        self.assertEqual(error.context["max_value"], 9223372036854775807)

    def test_syntax_check_recovers_at_semicolons(self):
        result = Parser().check_syntax(
            Lexer().scan(SourceText("<test>", "SELECT FROM t; SELECT * FROM t; SELECT FROM t;"))
        )
        self.assertEqual(result.valid_statement_count, 1)
        self.assertEqual(len(result.errors), 2)
        self.assertTrue(all(error.code == UNEXPECTED_TOKEN for error in result.errors))
        self.assertFalse(result.stopped_on_lexical_error)

    def test_syntax_check_stops_after_lexical_error(self):
        result = Parser().check_syntax(
            Lexer().scan(SourceText("<test>", "SELECT * FROM t; SELECT @ FROM t; SELECT * FROM t;"))
        )
        self.assertEqual(result.valid_statement_count, 1)
        self.assertEqual(len(result.errors), 1)
        self.assertTrue(result.stopped_on_lexical_error)

    def test_syntax_recovery_records_lexical_error(self):
        # 验证 parser.py 的分号恢复：恢复过程中读到非法字符时，
        # 应返回结果并标记词法错误，而不是把 DbError 直接抛出。
        result = Parser().check_syntax(
            Lexer().scan(SourceText("<test>", "SELECT FROM t @; SELECT * FROM t;"))
        )
        self.assertEqual(result.valid_statement_count, 0)
        self.assertEqual(len(result.errors), 2)
        self.assertEqual(result.errors[0].code, UNEXPECTED_TOKEN)
        self.assertEqual(result.errors[1].stage, ErrorStage.LEXICAL)
        self.assertTrue(result.stopped_on_lexical_error)

    def test_extended_keyword_is_unsupported_in_expect_position(self):
        # 验证 parser.py 的统一错误分类：扩展关键字不应被误报为普通语法错误。
        for sql in (
            "CREATE UPDATE t(id INT);",
            "CREATE TABLE t(id UPDATE);",
        ):
            error = self.assert_syntax_error(sql, UNSUPPORTED_FEATURE)
            self.assertIn("feature", error.context)


class ParserLazinessTests(unittest.TestCase):
    """验证第一条语句返回时不会提前触发后续非法输入。"""

    def test_iter_statements_is_lazy_between_statements(self):
        source = SourceText("<test>", "SELECT * FROM t; @invalid")
        statements = Parser().iter_statements(Lexer().scan(source))
        first = next(statements)
        self.assertIsInstance(first, SelectStmt)
        with self.assertRaises(DbError):
            next(statements)


if __name__ == "__main__":
    unittest.main()
