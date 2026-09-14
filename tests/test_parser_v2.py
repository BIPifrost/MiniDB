"""赵凯航 SQL 前端 v2 交接测试：Lexer -> Parser -> AST。"""

import unittest
from decimal import Decimal

from minidb.compiler import ast
from minidb.compiler.lexer import Lexer
from minidb.compiler.parser import Parser
from minidb.core.errors import DbError, UNEXPECTED_TOKEN, UNSUPPORTED_FEATURE
from minidb.core.schema import DataType
from minidb.core.source import SourceText
from minidb.core.tokens import TokenKind


def parse(sql: str):
    return list(Parser().iter_statements(Lexer().scan(SourceText("<v2-parser-test>", sql))))


class LexerV2Tests(unittest.TestCase):
    def test_new_keywords_are_case_insensitive(self):
        sql = "bool DATE decimal set primary key unique default index on describe is"
        kinds = [token.kind for token in Lexer().scan(SourceText("<test>", sql))][:-1]
        self.assertEqual(
            kinds,
            [
                TokenKind.KW_BOOL,
                TokenKind.KW_DATE,
                TokenKind.KW_DECIMAL,
                TokenKind.KW_SET,
                TokenKind.KW_PRIMARY,
                TokenKind.KW_KEY,
                TokenKind.KW_UNIQUE,
                TokenKind.KW_DEFAULT,
                TokenKind.KW_INDEX,
                TokenKind.KW_ON,
                TokenKind.KW_DESCRIBE,
                TokenKind.KW_IS,
            ],
        )


class ParserV2Tests(unittest.TestCase):
    def test_create_table_types_constraints_and_literals(self):
        statement = parse(
            "CREATE TABLE student("
            "id INT PRIMARY KEY,"
            "name VARCHAR(64) NOT NULL,"
            "active BOOL DEFAULT TRUE,"
            "birthday DATE NULL,"
            "balance DECIMAL(12,2) DEFAULT 0.00"
            ");"
        )[0]
        self.assertIsInstance(statement, ast.CreateTableStmt)
        self.assertEqual(
            [column.type_decl.kind for column in statement.columns],
            [DataType.INT, DataType.VARCHAR, DataType.BOOL, DataType.DATE, DataType.DECIMAL],
        )
        self.assertEqual(statement.columns[1].type_decl.length, 64)
        self.assertEqual(
            (statement.columns[4].type_decl.precision, statement.columns[4].type_decl.scale),
            (12, 2),
        )
        self.assertEqual(statement.columns[0].constraints[0].kind, "PRIMARY_KEY")
        self.assertIs(statement.columns[2].constraints[0].value.value, True)
        self.assertEqual(statement.columns[4].constraints[0].value.value, Decimal("0.00"))

    def test_update_assignment_and_is_not_null(self):
        statement = parse(
            "UPDATE student SET name='新姓名',balance=125.50 "
            "WHERE active=TRUE AND birthday IS NOT NULL;"
        )[0]
        self.assertIsInstance(statement, ast.UpdateStmt)
        self.assertEqual([item.target.text for item in statement.assignments], ["name", "balance"])
        self.assertEqual(statement.assignments[1].value.value, Decimal("125.50"))
        self.assertIsInstance(statement.predicate, ast.BinaryExpr)
        self.assertIsInstance(statement.predicate.right, ast.IsNullExpr)
        self.assertTrue(statement.predicate.right.negated)

    def test_date_shape_is_left_for_semantic_validation(self):
        statement = parse("INSERT INTO t(d) VALUES (DATE '2025-02-29');")[0]
        literal = statement.values[0]
        self.assertEqual(literal.value, "2025-02-29")
        self.assertIs(literal.type_spec.kind, DataType.DATE)

    def test_create_index_describe_and_explain(self):
        statements = parse(
            "CREATE UNIQUE INDEX users_id ON users(id);"
            "DESCRIBE users;"
            "EXPLAIN UPDATE users SET id=id WHERE id=1;"
        )
        self.assertIsInstance(statements[0], ast.CreateIndexStmt)
        self.assertTrue(statements[0].unique)
        self.assertIsInstance(statements[1], ast.DescribeStmt)
        self.assertIsInstance(statements[2], ast.ExplainStmt)
        self.assertIsInstance(statements[2].statement, ast.UpdateStmt)

    def test_set_rejects_double_equals_and_arithmetic(self):
        for sql, code in (
            ("UPDATE t SET a==1;", UNEXPECTED_TOKEN),
            ("UPDATE t SET a=a+1;", UNSUPPORTED_FEATURE),
        ):
            with self.subTest(sql=sql), self.assertRaises(DbError) as caught:
                parse(sql)
            self.assertEqual(caught.exception.code, code)


if __name__ == "__main__":
    unittest.main()
