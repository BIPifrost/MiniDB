"""仅语法检查的验收测试。

这些测试对应工作计划第 15.14 节 P03：语法检查不访问目录或数据库，
在真正的分号处恢复，遇到词法错误后停止，并正确处理字符串/注释中的分号。
"""

import unittest

from minidb.compiler.lexer import Lexer
from minidb.compiler.parser import Parser
from minidb.core.errors import (
    ErrorStage,
    UNEXPECTED_EOF,
    UNEXPECTED_TOKEN,
    UNSUPPORTED_FEATURE,
)
from minidb.core.source import SourceText


def check(sql: str):
    """使用正式 Lexer → Parser 链路执行纯语法检查。"""
    return Parser().check_syntax(Lexer().scan(SourceText("<syntax-test>", sql)))


class SyntaxCheckTests(unittest.TestCase):
    """测试 parser.py 的 check_syntax() 和分号恢复协议。"""

    def test_p03_recovers_multiple_syntax_errors(self):
        # P03 固定样例：两条错误语句之间的合法语句仍应被统计。
        result = check("SELECT FROM t; SELECT * FROM t; SELECT FROM t;")
        self.assertEqual(result.valid_statement_count, 1)
        self.assertEqual([error.code for error in result.errors], [
            UNEXPECTED_TOKEN,
            UNEXPECTED_TOKEN,
        ])
        self.assertFalse(result.stopped_on_lexical_error)

    def test_semicolons_inside_string_and_comments_are_not_sync_points(self):
        # 测试 Lexer 对字符串和块注释的处理不会把内部分号误当成语句边界。
        result = check(
            "SELECT * FROM t WHERE name = 'A;B';"
            "SELECT /* hidden; semicolon */ * FROM t;"
        )
        self.assertEqual(result.valid_statement_count, 2)
        self.assertEqual(result.errors, ())

    def test_empty_statements_are_ignored(self):
        # 连续空分号不生成错误，也不增加成功语句数。
        result = check(";;; SELECT * FROM t; ;; ")
        self.assertEqual(result.valid_statement_count, 1)
        self.assertEqual(result.errors, ())

    def test_missing_final_semicolon_reports_one_eof_error(self):
        # 到 EOF 仍缺少语句分号时只报告一次 UNEXPECTED_EOF。
        result = check("SELECT * FROM t")
        self.assertEqual(result.valid_statement_count, 0)
        self.assertEqual([error.code for error in result.errors], [UNEXPECTED_EOF])
        self.assertFalse(result.stopped_on_lexical_error)

    def test_unsupported_feature_is_recorded_and_recovery_continues(self):
        # 扩展关键字属于语法阶段的不支持功能；分号后仍继续检查下一条。
        result = check("CREATE UPDATE t(id INT); SELECT * FROM t;")
        self.assertEqual(result.valid_statement_count, 1)
        self.assertEqual([error.code for error in result.errors], [UNSUPPORTED_FEATURE])
        self.assertFalse(result.stopped_on_lexical_error)

    def test_lexical_error_stops_remaining_input(self):
        # Lexer 产生非法字符后，Parser 记录词法错误并停止，不猜测后续边界。
        result = check("SELECT * FROM t; SELECT @ FROM t; SELECT * FROM t;")
        self.assertEqual(result.valid_statement_count, 1)
        self.assertEqual(len(result.errors), 1)
        self.assertIs(result.errors[0].stage, ErrorStage.LEXICAL)
        self.assertTrue(result.stopped_on_lexical_error)


if __name__ == "__main__":
    unittest.main()
