"""Lexer 的单元测试。

由赵凯航维护。覆盖工作计划第 13.1 节的 L01–L04 测试场景，
以及位置精确性、CRLF 处理、运算符最长匹配、惰性扫描等边界。

运行方式：
    python -m unittest tests.test_lexer -v
"""

import unittest

from minidb.compiler.lexer import Lexer
from minidb.core.errors import (
    IDENTIFIER_TOO_LONG,
    INVALID_CHARACTER,
    INVALID_NUMBER,
    UNTERMINATED_COMMENT,
    UNTERMINATED_STRING,
    DbError,
    ErrorStage,
)
from minidb.core.source import SourceText
from minidb.core.tokens import TokenKind


def scan(source_text: str, name: str = "<test>") -> list:
    """辅助函数：扫描 SQL 文本，返回 Token 列表（消费整个 generator）。"""
    source = SourceText(name=name, text=source_text)
    return list(Lexer().scan(source))


def scan_one(source_text: str) -> list:
    """扫描并返回除 EOF 外的 Token 列表。"""
    tokens = scan(source_text)
    # 最后一个应该是 EOF
    assert tokens[-1].kind == TokenKind.EOF
    return tokens[:-1]


class TestL01KeywordsAndComments(unittest.TestCase):
    """L01：大小写关键字、空白、两类注释 → Token 类型和位置正确。"""

    def test_uppercase_keywords(self):
        tokens = scan_one("SELECT FROM WHERE")
        kinds = [t.kind for t in tokens]
        self.assertEqual(kinds, [TokenKind.KW_SELECT, TokenKind.KW_FROM, TokenKind.KW_WHERE])

    def test_lowercase_keywords(self):
        tokens = scan_one("select from where")
        kinds = [t.kind for t in tokens]
        self.assertEqual(kinds, [TokenKind.KW_SELECT, TokenKind.KW_FROM, TokenKind.KW_WHERE])

    def test_mixed_case_keywords(self):
        """关键字大小写不敏感（工作计划第 2.2 节）。"""
        tokens = scan_one("Select FrOm WhErE")
        kinds = [t.kind for t in tokens]
        self.assertEqual(kinds, [TokenKind.KW_SELECT, TokenKind.KW_FROM, TokenKind.KW_WHERE])

    def test_keyword_lexeme_preserves_original_case(self):
        """关键字 lexeme 保留原始写法（工作计划第 6.1 节）。"""
        tokens = scan_one("Select")
        self.assertEqual(tokens[0].lexeme, "Select")

    def test_all_supported_keywords(self):
        """本期支持的 14 个关键字全部能识别。"""
        sql = "CREATE TABLE INSERT INTO VALUES SELECT FROM WHERE DELETE AND OR NOT INT VARCHAR"
        tokens = scan_one(sql)
        expected = [
            TokenKind.KW_CREATE, TokenKind.KW_TABLE, TokenKind.KW_INSERT,
            TokenKind.KW_INTO, TokenKind.KW_VALUES, TokenKind.KW_SELECT,
            TokenKind.KW_FROM, TokenKind.KW_WHERE, TokenKind.KW_DELETE,
            TokenKind.KW_AND, TokenKind.KW_OR, TokenKind.KW_NOT,
            TokenKind.KW_INT, TokenKind.KW_VARCHAR,
        ]
        self.assertEqual([t.kind for t in tokens], expected)

    def test_extended_keywords_recognized(self):
        """扩展关键字也被识别为 KW_*（Parser 后续拒绝，工作计划第 15.3 节）。"""
        sql = "UPDATE JOIN ORDER BY GROUP DISTINCT NULL TRUE FALSE EXPLAIN"
        tokens = scan_one(sql)
        for t in tokens:
            self.assertTrue(t.kind.name.startswith("KW_"), f"{t.lexeme} 应为关键字")

    def test_whitespace_skipped(self):
        """空格、制表符、换行被跳过，不产生 Token。"""
        tokens = scan_one("SELECT  \t  id\nFROM  t")
        kinds = [t.kind for t in tokens]
        self.assertEqual(kinds, [TokenKind.KW_SELECT, TokenKind.IDENT, TokenKind.KW_FROM, TokenKind.IDENT])

    def test_line_comment(self):
        """-- 行注释到行末（工作计划第 2.2 节）。"""
        tokens = scan_one("SELECT id -- this is a comment\nFROM t")
        kinds = [t.kind for t in tokens]
        self.assertEqual(kinds, [TokenKind.KW_SELECT, TokenKind.IDENT, TokenKind.KW_FROM, TokenKind.IDENT])

    def test_line_comment_at_end(self):
        """行注释在文件末尾。"""
        tokens = scan_one("SELECT id -- comment")
        kinds = [t.kind for t in tokens]
        self.assertEqual(kinds, [TokenKind.KW_SELECT, TokenKind.IDENT])

    def test_block_comment(self):
        """/* */ 块注释（工作计划第 2.2 节）。"""
        tokens = scan_one("SELECT /* comment */ id FROM t")
        kinds = [t.kind for t in tokens]
        self.assertEqual(kinds, [TokenKind.KW_SELECT, TokenKind.IDENT, TokenKind.KW_FROM, TokenKind.IDENT])

    def test_block_comment_multiline(self):
        """块注释跨越多行。"""
        tokens = scan_one("SELECT /* line1\nline2 */ id")
        kinds = [t.kind for t in tokens]
        self.assertEqual(kinds, [TokenKind.KW_SELECT, TokenKind.IDENT])
        # id 应该在第 2 行
        self.assertEqual(tokens[1].span.start.line, 2)

    def test_comment_with_special_chars(self):
        """注释中包含特殊字符不影响扫描。"""
        tokens = scan_one("SELECT -- ; 'string' /*\nid")
        kinds = [t.kind for t in tokens]
        self.assertEqual(kinds, [TokenKind.KW_SELECT, TokenKind.IDENT])


class TestL02Strings(unittest.TestCase):
    """L02：'Tom''s book'、'A;B'、中文与空字符串 → 内容和语句边界正确。"""

    def test_double_single_quote_escape(self):
        """两个连续单引号表示一个单引号（工作计划第 2.2 节）。"""
        tokens = scan_one("'Tom''s book'")
        self.assertEqual(len(tokens), 1)
        self.assertEqual(tokens[0].kind, TokenKind.STRING_LITERAL)
        self.assertEqual(tokens[0].value, "Tom's book")
        self.assertEqual(tokens[0].lexeme, "'Tom''s book'")

    def test_string_with_semicolon(self):
        """字符串内的分号不结束语句（工作计划第 2.3 节第 7 条）。"""
        tokens = scan_one("'A;B'")
        self.assertEqual(len(tokens), 1)
        self.assertEqual(tokens[0].kind, TokenKind.STRING_LITERAL)
        self.assertEqual(tokens[0].value, "A;B")

    def test_chinese_string(self):
        """中文字符串按 Unicode 字符计数。"""
        tokens = scan_one("'张三'")
        self.assertEqual(tokens[0].kind, TokenKind.STRING_LITERAL)
        self.assertEqual(tokens[0].value, "张三")
        # '张三' 共 4 个字符（2个单引号+2个中文），offset 从 0 到 4
        self.assertEqual(tokens[0].span.start.offset, 0)
        self.assertEqual(tokens[0].span.end.offset, 4)

    def test_empty_string(self):
        """空字符串。"""
        tokens = scan_one("''")
        self.assertEqual(tokens[0].kind, TokenKind.STRING_LITERAL)
        self.assertEqual(tokens[0].value, "")

    def test_string_preserves_content(self):
        """字符串内容不转小写，不解释反斜杠（工作计划第 2.2 节）。"""
        tokens = scan_one("'MixedCase\\n'")
        self.assertEqual(tokens[0].value, "MixedCase\\n")

    def test_string_with_newline(self):
        """字符串内部的换行内容保持原样（工作计划第 2.1 节）。"""
        tokens = scan_one("'line1\nline2'")
        self.assertEqual(tokens[0].value, "line1\nline2")

    def test_string_with_crlf_preserves_both_characters(self):
        tokens = scan_one("'line1\r\nline2'")
        self.assertEqual(tokens[0].value, "line1\r\nline2")


class TestL03Errors(unittest.TestCase):
    """L03：非法字符、未闭合字符串/注释、非法数字片段 → 按约定报词法错误。"""

    def test_invalid_character(self):
        """非法字符报 INVALID_CHARACTER。"""
        with self.assertRaises(DbError) as cm:
            scan("SELECT @id")
        self.assertEqual(cm.exception.code, INVALID_CHARACTER)
        self.assertEqual(cm.exception.stage, ErrorStage.LEXICAL)
        self.assertEqual(cm.exception.context["character"], "@")

    def test_invalid_character_exclamation(self):
        """单独的 ! 是非法字符。"""
        with self.assertRaises(DbError) as cm:
            scan("a ! b")
        self.assertEqual(cm.exception.code, INVALID_CHARACTER)

    def test_unterminated_string(self):
        """未闭合字符串报 UNTERMINATED_STRING。"""
        with self.assertRaises(DbError) as cm:
            scan("SELECT '未闭合")
        self.assertEqual(cm.exception.code, UNTERMINATED_STRING)
        self.assertEqual(cm.exception.stage, ErrorStage.LEXICAL)

    def test_unterminated_block_comment(self):
        """未闭合块注释报 UNTERMINATED_COMMENT。"""
        with self.assertRaises(DbError) as cm:
            scan("SELECT /* 未闭合注释")
        self.assertEqual(cm.exception.code, UNTERMINATED_COMMENT)

    def test_invalid_number_with_letters(self):
        """12abc 整体报 INVALID_NUMBER（工作计划第 15.3 节）。"""
        with self.assertRaises(DbError) as cm:
            scan("12abc")
        self.assertEqual(cm.exception.code, INVALID_NUMBER)
        self.assertEqual(cm.exception.context["lexeme"], "12abc")

    def test_invalid_number_scientific(self):
        """1e3 报 INVALID_NUMBER。"""
        with self.assertRaises(DbError) as cm:
            scan("1e3")
        self.assertEqual(cm.exception.code, INVALID_NUMBER)

    def test_invalid_number_multiple_dots(self):
        """1.2.3 报 INVALID_NUMBER。"""
        with self.assertRaises(DbError) as cm:
            scan("1.2.3")
        self.assertEqual(cm.exception.code, INVALID_NUMBER)

    def test_invalid_number_dot_prefix(self):
        """.5 报 INVALID_NUMBER（工作计划第 15.3 节）。"""
        with self.assertRaises(DbError) as cm:
            scan(".5")
        self.assertEqual(cm.exception.code, INVALID_NUMBER)

    def test_invalid_number_underscore(self):
        """1_000 报 INVALID_NUMBER。"""
        with self.assertRaises(DbError) as cm:
            scan("1_000")
        self.assertEqual(cm.exception.code, INVALID_NUMBER)

    def test_error_has_position(self):
        """词法错误带位置信息。"""
        with self.assertRaises(DbError) as cm:
            scan("SELECT @id")
        err = cm.exception
        self.assertIsNotNone(err.span)
        # @ 在第 1 行第 8 列（SELECT 6字符 + 空格1 = offset 7, column 8）
        self.assertEqual(err.span.start.line, 1)
        self.assertEqual(err.span.start.column, 8)

    def test_error_does_not_crash_interpreter(self):
        """词法错误抛出 DbError，不异常退出解释器（工作计划第 13.1 节 L03）。"""
        try:
            scan("SELECT @id")
            self.fail("应该抛出 DbError")
        except DbError:
            pass  # 正确：抛出结构化错误，不是 Python 原生异常


class TestL04Literals(unittest.TestCase):
    """L04：20、3.14、'Alice'、'Tom''s book' → Lexer 正确识别全部课件常量。"""

    def test_integer_literal(self):
        """整数 20 → INTEGER_LITERAL，value 保留数字串。"""
        tokens = scan_one("20")
        self.assertEqual(tokens[0].kind, TokenKind.INTEGER_LITERAL)
        self.assertEqual(tokens[0].lexeme, "20")
        self.assertEqual(tokens[0].value, "20")
        self.assertIsInstance(tokens[0].value, str)

    def test_decimal_literal(self):
        """3.14 → DECIMAL_LITERAL，value 保留数字串，不调用 float（工作计划第 1.3 节第 7 条）。"""
        tokens = scan_one("3.14")
        self.assertEqual(tokens[0].kind, TokenKind.DECIMAL_LITERAL)
        self.assertEqual(tokens[0].lexeme, "3.14")
        self.assertEqual(tokens[0].value, "3.14")
        self.assertIsInstance(tokens[0].value, str)

    def test_string_literal_alice(self):
        """'Alice' → STRING_LITERAL。"""
        tokens = scan_one("'Alice'")
        self.assertEqual(tokens[0].kind, TokenKind.STRING_LITERAL)
        self.assertEqual(tokens[0].value, "Alice")

    def test_string_literal_with_escape(self):
        """'Tom''s book' → STRING_LITERAL，value 为 Tom's book。"""
        tokens = scan_one("'Tom''s book'")
        self.assertEqual(tokens[0].kind, TokenKind.STRING_LITERAL)
        self.assertEqual(tokens[0].value, "Tom's book")

    def test_negative_integer_is_minus_plus_integer(self):
        """负号单独产生 MINUS Token，整数另产生 INTEGER_LITERAL（工作计划第 15.3 节）。"""
        tokens = scan_one("-5")
        self.assertEqual(len(tokens), 2)
        self.assertEqual(tokens[0].kind, TokenKind.MINUS)
        self.assertEqual(tokens[1].kind, TokenKind.INTEGER_LITERAL)
        self.assertEqual(tokens[1].value, "5")


class TestPositionAccuracy(unittest.TestCase):
    """位置精确性测试（工作计划第 6.1 节、第 15.3 节）。"""

    def test_simple_select_positions(self):
        """SELECT id FROM t; 的每个 Token 位置正确。"""
        tokens = scan("SELECT id FROM t;")
        # SELECT: offset 0-6, column 1-7
        self.assertEqual(tokens[0].span.start.offset, 0)
        self.assertEqual(tokens[0].span.end.offset, 6)
        self.assertEqual(tokens[0].span.start.column, 1)
        self.assertEqual(tokens[0].span.end.column, 7)
        # id: offset 7-9, column 8-10
        self.assertEqual(tokens[1].span.start.offset, 7)
        self.assertEqual(tokens[1].span.end.offset, 9)
        # FROM: offset 10-14
        self.assertEqual(tokens[2].span.start.offset, 10)
        self.assertEqual(tokens[2].span.end.offset, 14)
        # t: offset 15-16
        self.assertEqual(tokens[3].span.start.offset, 15)
        self.assertEqual(tokens[3].span.end.offset, 16)
        # ;: offset 16-17
        self.assertEqual(tokens[4].span.start.offset, 16)
        self.assertEqual(tokens[4].span.end.offset, 17)
        # EOF: offset 17
        self.assertEqual(tokens[5].kind, TokenKind.EOF)
        self.assertEqual(tokens[5].span.start.offset, 17)

    def test_multiline_positions(self):
        """多行 SQL 的行号正确。"""
        tokens = scan("SELECT\nid\nFROM\nt;")
        self.assertEqual(tokens[0].span.start.line, 1)  # SELECT
        self.assertEqual(tokens[1].span.start.line, 2)  # id
        self.assertEqual(tokens[2].span.start.line, 3)  # FROM
        self.assertEqual(tokens[3].span.start.line, 4)  # t

    def test_crlf_counts_as_one_newline(self):
        """CRLF 算一次换行，offset 占 2 字符（工作计划第 15.3 节）。"""
        tokens = scan("SELECT\r\nid")
        # SELECT 在第 1 行
        self.assertEqual(tokens[0].span.start.line, 1)
        # id 应该在第 2 行（CRLF 算一次换行）
        self.assertEqual(tokens[1].span.start.line, 2)
        # id 的 offset 应该是 8（SELECT 6 + CRLF 2 = 8）
        self.assertEqual(tokens[1].span.start.offset, 8)

    def test_tab_counts_as_one_character(self):
        """制表符按一个字符计数（工作计划第 6.1 节）。"""
        tokens = scan("SELECT\tid")
        # id 的 offset 应该是 7（SELECT 6 + tab 1 = 7）
        self.assertEqual(tokens[1].span.start.offset, 7)

    def test_span_source_name(self):
        """Token.span.source_name 等于 SourceText.name。"""
        source = SourceText(name="examples/demo.sql", text="SELECT 1;")
        tokens = list(Lexer().scan(source))
        self.assertEqual(tokens[0].span.source_name, "examples/demo.sql")


class TestOperators(unittest.TestCase):
    """运算符最长匹配测试（工作计划第 5.1 节任务 2）。"""

    def test_eq_single(self):
        tokens = scan_one("a = b")
        self.assertEqual(tokens[1].kind, TokenKind.EQ)
        self.assertEqual(tokens[1].lexeme, "=")

    def test_eq_double(self):
        """== 也产生 EQ，lexeme 保留 ==（工作计划第 15.3 节）。"""
        tokens = scan_one("a == b")
        self.assertEqual(tokens[1].kind, TokenKind.EQ)
        self.assertEqual(tokens[1].lexeme, "==")

    def test_ne_exclamation(self):
        """!= 产生 NE。"""
        tokens = scan_one("a != b")
        self.assertEqual(tokens[1].kind, TokenKind.NE)
        self.assertEqual(tokens[1].lexeme, "!=")

    def test_ne_angle(self):
        """<> 也产生 NE，lexeme 保留 <>（工作计划第 15.3 节）。"""
        tokens = scan_one("a <> b")
        self.assertEqual(tokens[1].kind, TokenKind.NE)
        self.assertEqual(tokens[1].lexeme, "<>")

    def test_lt(self):
        tokens = scan_one("a < b")
        self.assertEqual(tokens[1].kind, TokenKind.LT)

    def test_le(self):
        tokens = scan_one("a <= b")
        self.assertEqual(tokens[1].kind, TokenKind.LE)
        self.assertEqual(tokens[1].lexeme, "<=")

    def test_gt(self):
        tokens = scan_one("a > b")
        self.assertEqual(tokens[1].kind, TokenKind.GT)

    def test_ge(self):
        tokens = scan_one("a >= b")
        self.assertEqual(tokens[1].kind, TokenKind.GE)
        self.assertEqual(tokens[1].lexeme, ">=")

    def test_plus_minus_star_slash(self):
        tokens = scan_one("+ - * /")
        kinds = [t.kind for t in tokens]
        self.assertEqual(kinds, [TokenKind.PLUS, TokenKind.MINUS, TokenKind.STAR, TokenKind.SLASH])

    def test_all_comparison_operators(self):
        tokens = scan_one("= == != <> < <= > >=")
        kinds = [t.kind for t in tokens]
        expected = [TokenKind.EQ, TokenKind.EQ, TokenKind.NE, TokenKind.NE,
                    TokenKind.LT, TokenKind.LE, TokenKind.GT, TokenKind.GE]
        self.assertEqual(kinds, expected)


class TestDelimiters(unittest.TestCase):
    """分隔符测试。"""

    def test_parentheses(self):
        tokens = scan_one("( )")
        self.assertEqual(tokens[0].kind, TokenKind.LPAREN)
        self.assertEqual(tokens[1].kind, TokenKind.RPAREN)

    def test_comma(self):
        tokens = scan_one("a, b")
        self.assertEqual(tokens[1].kind, TokenKind.COMMA)

    def test_semicolon(self):
        tokens = scan_one("SELECT 1;")
        self.assertEqual(tokens[2].kind, TokenKind.SEMICOLON)


class TestIdentifier(unittest.TestCase):
    """标识符测试。"""

    def test_simple_identifier(self):
        tokens = scan_one("student")
        self.assertEqual(tokens[0].kind, TokenKind.IDENT)
        self.assertEqual(tokens[0].value, "student")

    def test_identifier_with_underscore(self):
        tokens = scan_one("_sys_catalog")
        self.assertEqual(tokens[0].kind, TokenKind.IDENT)
        self.assertEqual(tokens[0].value, "_sys_catalog")

    def test_identifier_with_digits(self):
        tokens = scan_one("col123")
        self.assertEqual(tokens[0].kind, TokenKind.IDENT)

    def test_identifier_preserves_case(self):
        """标识符 lexeme 和 value 保留原始大小写（工作计划第 6.1 节）。"""
        tokens = scan_one("StudentName")
        self.assertEqual(tokens[0].lexeme, "StudentName")
        self.assertEqual(tokens[0].value, "StudentName")

    def test_identifier_max_length_64(self):
        """标识符最多 64 字符（工作计划第 2.2 节）。"""
        long_name = "a" * 64
        tokens = scan_one(long_name)
        self.assertEqual(tokens[0].kind, TokenKind.IDENT)
        self.assertEqual(len(tokens[0].value), 64)

    def test_identifier_too_long(self):
        """标识符超过 64 字符报 IDENTIFIER_TOO_LONG。"""
        long_name = "a" * 65
        with self.assertRaises(DbError) as cm:
            scan(long_name)
        self.assertEqual(cm.exception.code, IDENTIFIER_TOO_LONG)
        self.assertEqual(cm.exception.context["max_length"], 64)


class TestEOF(unittest.TestCase):
    """EOF 测试。"""

    def test_eof_produced_once(self):
        """EOF 只产生一次（工作计划第 15.3 节）。"""
        tokens = scan("SELECT 1;")
        eof_count = sum(1 for t in tokens if t.kind == TokenKind.EOF)
        self.assertEqual(eof_count, 1)

    def test_eof_is_last(self):
        """EOF 是最后一个 Token。"""
        tokens = scan("SELECT 1;")
        self.assertEqual(tokens[-1].kind, TokenKind.EOF)

    def test_eof_lexeme_empty(self):
        """EOF 的 lexeme 为空字符串（工作计划第 15.3 节）。"""
        tokens = scan("SELECT 1;")
        self.assertEqual(tokens[-1].lexeme, "")

    def test_eof_value_none(self):
        tokens = scan("SELECT 1;")
        self.assertIsNone(tokens[-1].value)

    def test_empty_input_produces_only_eof(self):
        """空输入只产生 EOF。"""
        tokens = scan("")
        self.assertEqual(len(tokens), 1)
        self.assertEqual(tokens[0].kind, TokenKind.EOF)

    def test_whitespace_only_produces_only_eof(self):
        """只有空白的输入只产生 EOF。"""
        tokens = scan("   \t\n  ")
        self.assertEqual(len(tokens), 1)
        self.assertEqual(tokens[0].kind, TokenKind.EOF)


class TestLazyScanning(unittest.TestCase):
    """惰性扫描测试（工作计划第 7.1 节、第 2.3 节第 8 条）。"""

    def test_scan_returns_iterator(self):
        """scan 返回迭代器（generator）。"""
        source = SourceText(name="<test>", text="SELECT 1;")
        result = Lexer().scan(source)
        import types
        self.assertIsInstance(result, types.GeneratorType)

    def test_lazy_does_not_scan_ahead(self):
        """惰性扫描：不预先扫描后续非法字符（工作计划第 2.3 节第 8 条）。"""
        # 前面是合法 SQL，后面有非法字符
        source = SourceText(name="<test>", text="SELECT 1; @invalid")
        lexer = Lexer()
        tokens_iter = lexer.scan(source)
        # 消费前 4 个 Token（SELECT, 1, ;, EOF 应该在 ; 之后）
        # 实际上 ; 之后还有空白和 @invalid，但 EOF 应该在最后
        # 消费到 SELECT, 1, ;
        t1 = next(tokens_iter)
        t2 = next(tokens_iter)
        t3 = next(tokens_iter)
        self.assertEqual(t1.kind, TokenKind.KW_SELECT)
        self.assertEqual(t2.kind, TokenKind.INTEGER_LITERAL)
        self.assertEqual(t3.kind, TokenKind.SEMICOLON)
        # 继续消费应该遇到空白（跳过）然后 @ 报 INVALID_CHARACTER
        with self.assertRaises(DbError) as cm:
            next(tokens_iter)
        self.assertEqual(cm.exception.code, INVALID_CHARACTER)

    def test_interleaved_scans_are_independent(self):
        lexer = Lexer()
        first = lexer.scan(SourceText(name="first.sql", text="SELECT first;"))
        second = lexer.scan(SourceText(name="second.sql", text="DELETE second;"))

        self.assertEqual(next(first).kind, TokenKind.KW_SELECT)
        self.assertEqual(next(second).kind, TokenKind.KW_DELETE)
        first_ident = next(first)
        second_ident = next(second)
        self.assertEqual(first_ident.value, "first")
        self.assertEqual(first_ident.span.source_name, "first.sql")
        self.assertEqual(second_ident.value, "second")
        self.assertEqual(second_ident.span.source_name, "second.sql")


class TestInterleavedGenerators(unittest.TestCase):
    """交错消费多个 generator 的隔离性测试。

    工作计划第 15.3 节要求：每份新的 SourceText 使用新的扫描器状态，
    不能把上一提交的前瞻 Token 带入下一提交。
    同一个 Lexer 实例产生的多个 generator 必须互不干扰。
    """

    def test_interleaved_scan_preserves_independent_state(self):
        """同一个 Lexer 的两个 generator 交错消费，状态互不干扰。"""
        lexer = Lexer()
        a = lexer.scan(SourceText(name="a.sql", text="SELECT a;"))
        b = lexer.scan(SourceText(name="b.sql", text="DELETE b;"))

        # 交错消费
        t_a1 = next(a)  # KW_SELECT from a.sql
        self.assertEqual(t_a1.kind, TokenKind.KW_SELECT)
        self.assertEqual(t_a1.span.source_name, "a.sql")

        t_b1 = next(b)  # KW_DELETE from b.sql
        self.assertEqual(t_b1.kind, TokenKind.KW_DELETE)
        self.assertEqual(t_b1.span.source_name, "b.sql")

        # 继续消费 a，应该得到 IDENT('a')，而不是从 b.sql 继续读
        t_a2 = next(a)
        self.assertEqual(t_a2.kind, TokenKind.IDENT)
        self.assertEqual(t_a2.value, "a")
        self.assertEqual(t_a2.span.source_name, "a.sql")

        # 继续消费 b，应该得到 IDENT('b')
        t_b2 = next(b)
        self.assertEqual(t_b2.kind, TokenKind.IDENT)
        self.assertEqual(t_b2.value, "b")
        self.assertEqual(t_b2.span.source_name, "b.sql")

        # 继续消费 a，应该得到 SEMICOLON
        t_a3 = next(a)
        self.assertEqual(t_a3.kind, TokenKind.SEMICOLON)
        self.assertEqual(t_a3.span.source_name, "a.sql")

        # 继续消费 b，应该得到 SEMICOLON
        t_b3 = next(b)
        self.assertEqual(t_b3.kind, TokenKind.SEMICOLON)
        self.assertEqual(t_b3.span.source_name, "b.sql")

        # 各自的 EOF
        t_a4 = next(a)
        self.assertEqual(t_a4.kind, TokenKind.EOF)
        self.assertEqual(t_a4.span.source_name, "a.sql")

        t_b4 = next(b)
        self.assertEqual(t_b4.kind, TokenKind.EOF)
        self.assertEqual(t_b4.span.source_name, "b.sql")

    def test_interleaved_scan_different_lengths(self):
        """两个 SQL 长度不同时，交错消费不会提前 EOF 或越界。"""
        lexer = Lexer()
        short = lexer.scan(SourceText(name="short.sql", text="SELECT 1;"))
        long = lexer.scan(SourceText(name="long.sql", text="CREATE TABLE t(id INT, name VARCHAR, age INT);"))

        # 消费完 short 的全部 Token
        short_tokens = list(short)
        self.assertEqual(short_tokens[-1].kind, TokenKind.EOF)
        self.assertEqual(short_tokens[-1].span.source_name, "short.sql")

        # long 应该仍然完整，不受 short 消费完的影响
        long_tokens = list(long)
        self.assertEqual(long_tokens[0].kind, TokenKind.KW_CREATE)
        self.assertEqual(long_tokens[-1].kind, TokenKind.EOF)
        self.assertEqual(long_tokens[-1].span.source_name, "long.sql")
        # long 应该有更多 Token
        self.assertGreater(len(long_tokens), len(short_tokens))

    def test_three_generators_interleaved(self):
        """三个 generator 交错消费，全部隔离。"""
        lexer = Lexer()
        g1 = lexer.scan(SourceText(name="1.sql", text="INSERT INTO t VALUES (1);"))
        g2 = lexer.scan(SourceText(name="2.sql", text="DELETE FROM t WHERE id = 1;"))
        g3 = lexer.scan(SourceText(name="3.sql", text="SELECT * FROM t;"))

        self.assertEqual(next(g1).kind, TokenKind.KW_INSERT)
        self.assertEqual(next(g2).kind, TokenKind.KW_DELETE)
        self.assertEqual(next(g3).kind, TokenKind.KW_SELECT)

        # 各自的 source_name 正确
        self.assertEqual(next(g1).span.source_name, "1.sql")
        self.assertEqual(next(g2).span.source_name, "2.sql")
        self.assertEqual(next(g3).span.source_name, "3.sql")

    def test_new_lexer_after_exhaustion(self):
        """一个 generator 消费完后，同一个 Lexer 实例创建新 generator 正常工作。"""
        lexer = Lexer()

        # 第一次扫描
        tokens1 = list(lexer.scan(SourceText(name="first.sql", text="SELECT 1;")))
        self.assertEqual(tokens1[0].kind, TokenKind.KW_SELECT)
        self.assertEqual(tokens1[-1].kind, TokenKind.EOF)

        # 第二次扫描，应该完全独立
        tokens2 = list(lexer.scan(SourceText(name="second.sql", text="DELETE FROM t;")))
        self.assertEqual(tokens2[0].kind, TokenKind.KW_DELETE)
        self.assertEqual(tokens2[-1].span.source_name, "second.sql")


class TestFullStatement(unittest.TestCase):
    """完整语句扫描测试。"""

    def test_create_table(self):
        sql = "CREATE TABLE student(id INT, name VARCHAR, age INT);"
        tokens = scan_one(sql)
        kinds = [t.kind for t in tokens]
        expected = [
            TokenKind.KW_CREATE, TokenKind.KW_TABLE, TokenKind.IDENT,
            TokenKind.LPAREN, TokenKind.IDENT, TokenKind.KW_INT, TokenKind.COMMA,
            TokenKind.IDENT, TokenKind.KW_VARCHAR, TokenKind.COMMA,
            TokenKind.IDENT, TokenKind.KW_INT, TokenKind.RPAREN, TokenKind.SEMICOLON,
        ]
        self.assertEqual(kinds, expected)

    def test_insert(self):
        sql = "INSERT INTO student(id,name,age) VALUES (1,'Alice',20);"
        tokens = scan_one(sql)
        kinds = [t.kind for t in tokens]
        expected = [
            TokenKind.KW_INSERT, TokenKind.KW_INTO, TokenKind.IDENT,
            TokenKind.LPAREN, TokenKind.IDENT, TokenKind.COMMA,
            TokenKind.IDENT, TokenKind.COMMA, TokenKind.IDENT, TokenKind.RPAREN,
            TokenKind.KW_VALUES,
            TokenKind.LPAREN, TokenKind.INTEGER_LITERAL, TokenKind.COMMA,
            TokenKind.STRING_LITERAL, TokenKind.COMMA, TokenKind.INTEGER_LITERAL,
            TokenKind.RPAREN, TokenKind.SEMICOLON,
        ]
        self.assertEqual(kinds, expected)

    def test_select_with_where(self):
        sql = "SELECT id,name FROM student WHERE age >= 18;"
        tokens = scan_one(sql)
        kinds = [t.kind for t in tokens]
        expected = [
            TokenKind.KW_SELECT, TokenKind.IDENT, TokenKind.COMMA, TokenKind.IDENT,
            TokenKind.KW_FROM, TokenKind.IDENT,
            TokenKind.KW_WHERE, TokenKind.IDENT, TokenKind.GE, TokenKind.INTEGER_LITERAL,
            TokenKind.SEMICOLON,
        ]
        self.assertEqual(kinds, expected)

    def test_delete(self):
        sql = "DELETE FROM student WHERE id = 2;"
        tokens = scan_one(sql)
        kinds = [t.kind for t in tokens]
        expected = [
            TokenKind.KW_DELETE, TokenKind.KW_FROM, TokenKind.IDENT,
            TokenKind.KW_WHERE, TokenKind.IDENT, TokenKind.EQ, TokenKind.INTEGER_LITERAL,
            TokenKind.SEMICOLON,
        ]
        self.assertEqual(kinds, expected)

    def test_boolean_expressions(self):
        """AND/OR/NOT 表达式。"""
        sql = "WHERE a = 1 AND b = 2 OR NOT c = 3;"
        tokens = scan_one(sql)
        kinds = [t.kind for t in tokens]
        self.assertIn(TokenKind.KW_AND, kinds)
        self.assertIn(TokenKind.KW_OR, kinds)
        self.assertIn(TokenKind.KW_NOT, kinds)


if __name__ == "__main__":
    unittest.main()
