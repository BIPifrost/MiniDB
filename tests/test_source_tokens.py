"""source.py 和 tokens.py 的单元测试。

由赵凯航维护。验证工作计划第 6.1 节、第 15.1 节、第 15.3 节、
第 15.13 节对公共数据结构的全部要求。

运行方式：
    python -m unittest tests.test_source_tokens -v
"""

import unittest

from minidb.core.source import SourcePos, SourceSpan, SourceText
from minidb.core.tokens import KEYWORDS, Token, TokenKind, token_category


# ---------------------------------------------------------------------------
# source.py 测试
# ---------------------------------------------------------------------------
class TestSourcePos(unittest.TestCase):
    """验证 SourcePos 的工作计划要求（第 6.1、15.1、15.3 节）。"""

    def test_normal_construction(self):
        """正常构造：行、列从1开始，offset从0开始。"""
        pos = SourcePos(line=1, column=1, offset=0)
        self.assertEqual(pos.line, 1)
        self.assertEqual(pos.column, 1)
        self.assertEqual(pos.offset, 0)

    def test_line_must_be_at_least_1(self):
        """line 必须 >= 1（工作计划 15.3 节）。"""
        with self.assertRaises(ValueError):
            SourcePos(line=0, column=1, offset=0)

    def test_column_must_be_at_least_1(self):
        """column 必须 >= 1（工作计划 15.3 节）。"""
        with self.assertRaises(ValueError):
            SourcePos(line=1, column=0, offset=0)

    def test_offset_must_be_non_negative(self):
        """offset 必须 >= 0（工作计划 15.3 节）。"""
        with self.assertRaises(ValueError):
            SourcePos(line=1, column=1, offset=-1)

    def test_bool_is_not_accepted_as_int(self):
        """True/False 不能当作 int（工作计划 15.1 节第 8 条）。"""
        with self.assertRaises(TypeError):
            SourcePos(line=True, column=1, offset=0)
        with self.assertRaises(TypeError):
            SourcePos(line=1, column=False, offset=0)

    def test_immutable(self):
        """不可变性（工作计划 15.1 节第 7 条）。"""
        pos = SourcePos(line=1, column=1, offset=0)
        with self.assertRaises(Exception):
            pos.line = 2  # type: ignore[misc]


class TestSourceSpan(unittest.TestCase):
    """验证 SourceSpan 的工作计划要求（第 6.1、15.3 节）。"""

    def _make_pos(self, offset: int) -> SourcePos:
        return SourcePos(line=1, column=offset + 1, offset=offset)

    def test_normal_construction(self):
        """正常构造：起点包含、终点不包含。"""
        span = SourceSpan(
            start=self._make_pos(0),
            end=self._make_pos(6),
            source_name="<test>",
        )
        self.assertEqual(span.start.offset, 0)
        self.assertEqual(span.end.offset, 6)
        self.assertEqual(span.source_name, "<test>")

    def test_start_offset_cannot_exceed_end(self):
        """start.offset 不能大于 end.offset（工作计划 6.1 节）。"""
        with self.assertRaises(ValueError):
            SourceSpan(
                start=self._make_pos(5),
                end=self._make_pos(3),
                source_name="<test>",
            )

    def test_zero_length_span_is_allowed(self):
        """起点等于终点的零长度 Span 是允许的（如 EOF）。"""
        span = SourceSpan(
            start=self._make_pos(3),
            end=self._make_pos(3),
            source_name="<test>",
        )
        self.assertEqual(span.start.offset, span.end.offset)

    def test_source_name_must_be_str(self):
        """source_name 必须是 str。"""
        with self.assertRaises(TypeError):
            SourceSpan(
                start=self._make_pos(0),
                end=self._make_pos(1),
                source_name=123,
            )


class TestSourceText(unittest.TestCase):
    """验证 SourceText 的工作计划要求（第 6.1 节）。"""

    def test_normal_construction(self):
        """正常构造。"""
        src = SourceText(name="<test>", text="SELECT id FROM t;")
        self.assertEqual(src.name, "<test>")
        self.assertEqual(src.text, "SELECT id FROM t;")

    def test_len_returns_character_count(self):
        """len() 返回字符数（按 Unicode 字符，不是字节）。"""
        src = SourceText(name="<test>", text="SELECT")
        self.assertEqual(len(src), 6)

    def test_unicode_characters_counted_as_one(self):
        """中文字符按一个 Unicode 字符计数（工作计划 15.3 节）。"""
        src = SourceText(name="<test>", text="中文")
        self.assertEqual(len(src), 2)

    def test_name_must_be_str(self):
        with self.assertRaises(TypeError):
            SourceText(name=123, text="x")  # type: ignore[arg-type]

    def test_text_must_be_str(self):
        with self.assertRaises(TypeError):
            SourceText(name="<test>", text=123)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# tokens.py 测试
# ---------------------------------------------------------------------------
class TestTokenKind(unittest.TestCase):
    """验证 TokenKind 枚举的工作计划要求（第 15.3 节）。"""

    def test_total_count_is_43(self):
        """TokenKind 总数为 43（工作计划 15.3 节固定列表）。"""
        self.assertEqual(len(list(TokenKind)), 43)

    def test_keyword_count_is_24(self):
        """关键字 KW_* 共 24 个。"""
        kw = [k for k in TokenKind if k.name.startswith("KW_")]
        self.assertEqual(len(kw), 24)

    def test_supported_keywords_exist(self):
        """本期支持的 14 个关键字都存在。"""
        expected = [
            "KW_CREATE", "KW_TABLE", "KW_INSERT", "KW_INTO", "KW_VALUES",
            "KW_SELECT", "KW_FROM", "KW_WHERE", "KW_DELETE",
            "KW_AND", "KW_OR", "KW_NOT", "KW_INT", "KW_VARCHAR",
        ]
        for name in expected:
            self.assertIn(name, TokenKind.__members__, f"缺少 {name}")

    def test_extended_keywords_exist(self):
        """扩展关键字（10 个）都存在，供 Parser 报 UNSUPPORTED_FEATURE。"""
        expected = [
            "KW_UPDATE", "KW_JOIN", "KW_ORDER", "KW_BY", "KW_GROUP",
            "KW_DISTINCT", "KW_NULL", "KW_TRUE", "KW_FALSE", "KW_EXPLAIN",
        ]
        for name in expected:
            self.assertIn(name, TokenKind.__members__, f"缺少 {name}")

    def test_literals_and_ident_exist(self):
        """标识符和 3 种字面量都存在。"""
        for name in ["IDENT", "INTEGER_LITERAL", "DECIMAL_LITERAL", "STRING_LITERAL"]:
            self.assertIn(name, TokenKind.__members__)

    def test_operators_exist(self):
        """10 个运算符都存在。"""
        expected = ["EQ", "NE", "LT", "LE", "GT", "GE", "PLUS", "MINUS", "STAR", "SLASH"]
        for name in expected:
            self.assertIn(name, TokenKind.__members__)

    def test_delimiters_exist(self):
        """4 个分隔符都存在。"""
        for name in ["LPAREN", "RPAREN", "COMMA", "SEMICOLON"]:
            self.assertIn(name, TokenKind.__members__)

    def test_eof_exists(self):
        self.assertIn("EOF", TokenKind.__members__)


class TestToken(unittest.TestCase):
    """验证 Token 数据类的工作计划要求（第 6.1、15.3 节）。"""

    def _make_span(self, start_offset: int = 0, end_offset: int = 6) -> SourceSpan:
        return SourceSpan(
            start=SourcePos(line=1, column=start_offset + 1, offset=start_offset),
            end=SourcePos(line=1, column=end_offset + 1, offset=end_offset),
            source_name="<test>",
        )

    def test_normal_construction(self):
        """正常构造关键字 Token。"""
        token = Token(
            kind=TokenKind.KW_SELECT,
            lexeme="SELECT",
            value=None,
            span=self._make_span(),
        )
        self.assertEqual(token.kind, TokenKind.KW_SELECT)
        self.assertEqual(token.lexeme, "SELECT")
        self.assertIsNone(token.value)

    def test_identifier_token_preserves_original_case(self):
        """标识符 lexeme 保留原始大小写（工作计划 6.1 节）。"""
        token = Token(
            kind=TokenKind.IDENT,
            lexeme="StudentName",
            value="StudentName",
            span=self._make_span(),
        )
        self.assertEqual(token.lexeme, "StudentName")
        self.assertEqual(token.value, "StudentName")

    def test_string_token_value_is_unescaped(self):
        """字符串 Token 的 value 是处理双单引号后的内容（工作计划 6.1 节）。"""
        token = Token(
            kind=TokenKind.STRING_LITERAL,
            lexeme="'Tom''s book'",
            value="Tom's book",
            span=self._make_span(),
        )
        self.assertEqual(token.value, "Tom's book")

    def test_integer_token_value_is_string(self):
        """整数 Token 的 value 保留原始数字串（str 类型），不转 int（工作计划 15.3 节）。"""
        token = Token(
            kind=TokenKind.INTEGER_LITERAL,
            lexeme="18",
            value="18",
            span=self._make_span(),
        )
        self.assertIsInstance(token.value, str)
        self.assertEqual(token.value, "18")

    def test_decimal_token_value_is_string(self):
        """小数 Token 的 value 保留原始数字串，不调用 float（工作计划 1.3 节第 7 条）。"""
        token = Token(
            kind=TokenKind.DECIMAL_LITERAL,
            lexeme="3.14",
            value="3.14",
            span=self._make_span(),
        )
        self.assertIsInstance(token.value, str)
        self.assertEqual(token.value, "3.14")

    def test_eq_and_double_eq_both_produce_eq(self):
        """= 和 == 都产生 EQ，lexeme 保留各自原文（工作计划 15.3 节）。"""
        t1 = Token(kind=TokenKind.EQ, lexeme="=", value=None, span=self._make_span())
        t2 = Token(kind=TokenKind.EQ, lexeme="==", value=None, span=self._make_span())
        self.assertIs(t1.kind, TokenKind.EQ)
        self.assertIs(t2.kind, TokenKind.EQ)
        self.assertEqual(t1.lexeme, "=")
        self.assertEqual(t2.lexeme, "==")

    def test_ne_and_angle_bracket_both_produce_ne(self):
        """!= 和 <> 都产生 NE，lexeme 保留各自原文（工作计划 15.3 节）。"""
        t1 = Token(kind=TokenKind.NE, lexeme="!=", value=None, span=self._make_span())
        t2 = Token(kind=TokenKind.NE, lexeme="<>", value=None, span=self._make_span())
        self.assertIs(t1.kind, TokenKind.NE)
        self.assertIs(t2.kind, TokenKind.NE)
        self.assertEqual(t1.lexeme, "!=")
        self.assertEqual(t2.lexeme, "<>")

    def test_operator_value_is_none(self):
        """所有运算符 Token 的 value 为 None（工作计划 15.3 节）。"""
        for op_kind in [
            TokenKind.EQ, TokenKind.NE, TokenKind.LT, TokenKind.LE,
            TokenKind.GT, TokenKind.GE, TokenKind.PLUS, TokenKind.MINUS,
            TokenKind.STAR, TokenKind.SLASH,
        ]:
            token = Token(kind=op_kind, lexeme="+", value=None, span=self._make_span())
            self.assertIsNone(token.value, f"{op_kind.name} 的 value 应为 None")

    def test_eof_lexeme_is_empty_string(self):
        """EOF 的 lexeme 为空字符串（工作计划 15.3 节）。"""
        eof_span = SourceSpan(
            start=SourcePos(line=1, column=40, offset=39),
            end=SourcePos(line=1, column=40, offset=39),
            source_name="<test>",
        )
        token = Token(kind=TokenKind.EOF, lexeme="", value=None, span=eof_span)
        self.assertEqual(token.lexeme, "")

    def test_immutable(self):
        """不可变性（工作计划 15.1 节第 7 条）。"""
        token = Token(
            kind=TokenKind.KW_SELECT, lexeme="SELECT", value=None, span=self._make_span()
        )
        with self.assertRaises(Exception):
            token.lexeme = "UPDATE"  # type: ignore[misc]

    def test_kind_must_be_token_kind(self):
        with self.assertRaises(TypeError):
            Token(kind="KW_SELECT", lexeme="x", value=None, span=self._make_span())  # type: ignore[arg-type]

    def test_lexeme_must_be_str(self):
        with self.assertRaises(TypeError):
            Token(kind=TokenKind.IDENT, lexeme=123, value=None, span=self._make_span())  # type: ignore[arg-type]

    def test_value_must_be_str_or_none(self):
        with self.assertRaises(TypeError):
            Token(kind=TokenKind.IDENT, lexeme="x", value=123, span=self._make_span())  # type: ignore[arg-type]

    def test_span_must_be_source_span(self):
        with self.assertRaises(TypeError):
            Token(kind=TokenKind.IDENT, lexeme="x", value=None, span=None)  # type: ignore[arg-type]


class TestKeywordsMapping(unittest.TestCase):
    """验证 KEYWORDS 映射表的工作计划要求（第 15.3 节）。"""

    def test_total_count_is_24(self):
        """KEYWORDS 映射表共 24 条（全部关键字，含扩展关键字）。"""
        self.assertEqual(len(KEYWORDS), 24)

    def test_all_keyword_kinds_are_mapped(self):
        """每个 KW_* 枚举值都能在 KEYWORDS 中找到。"""
        for kind in TokenKind:
            if kind.name.startswith("KW_"):
                kw_str = kind.name[3:].lower()
                self.assertIn(kw_str, KEYWORDS, f"关键字 {kw_str} 不在映射表中")
                self.assertIs(KEYWORDS[kw_str], kind)

    def test_case_insensitive_lookup(self):
        """关键字大小写不敏感（映射表用小写键，Lexer 应转小写后查表）。"""
        self.assertIs(KEYWORDS["select"], TokenKind.KW_SELECT)
        self.assertIs(KEYWORDS["SELECT".lower()], TokenKind.KW_SELECT)
        self.assertIs(KEYWORDS["Select".lower()], TokenKind.KW_SELECT)

    def test_extended_keywords_are_mapped(self):
        """扩展关键字也在映射表中（Lexer 识别，Parser 拒绝）。"""
        self.assertIs(KEYWORDS["update"], TokenKind.KW_UPDATE)
        self.assertIs(KEYWORDS["join"], TokenKind.KW_JOIN)
        self.assertIs(KEYWORDS["order"], TokenKind.KW_ORDER)
        self.assertIs(KEYWORDS["null"], TokenKind.KW_NULL)

    def test_identifiers_are_not_in_keywords(self):
        """普通标识符不在关键字映射表中。"""
        self.assertNotIn("student", KEYWORDS)
        self.assertNotIn("id", KEYWORDS)
        self.assertNotIn("name", KEYWORDS)


class TestTokenCategory(unittest.TestCase):
    """验证 token_category 粗类别映射（工作计划第 15.13 节）。"""

    def test_keywords_category(self):
        """所有 KW_* 归为 KEYWORD，包括布尔连接关键字 AND/OR/NOT。"""
        for kind in TokenKind:
            if kind.name.startswith("KW_"):
                self.assertEqual(token_category(kind), "KEYWORD", f"{kind.name} 应为 KEYWORD")

    def test_identifier_category(self):
        self.assertEqual(token_category(TokenKind.IDENT), "IDENTIFIER")

    def test_literals_category(self):
        """三种 *_LITERAL 归为 CONST。"""
        self.assertEqual(token_category(TokenKind.INTEGER_LITERAL), "CONST")
        self.assertEqual(token_category(TokenKind.DECIMAL_LITERAL), "CONST")
        self.assertEqual(token_category(TokenKind.STRING_LITERAL), "CONST")

    def test_operators_category(self):
        for kind in [
            TokenKind.EQ, TokenKind.NE, TokenKind.LT, TokenKind.LE,
            TokenKind.GT, TokenKind.GE, TokenKind.PLUS, TokenKind.MINUS,
            TokenKind.STAR, TokenKind.SLASH,
        ]:
            self.assertEqual(token_category(kind), "OPERATOR", f"{kind.name} 应为 OPERATOR")

    def test_delimiters_category(self):
        for kind in [TokenKind.LPAREN, TokenKind.RPAREN, TokenKind.COMMA, TokenKind.SEMICOLON]:
            self.assertEqual(token_category(kind), "DELIMITER", f"{kind.name} 应为 DELIMITER")

    def test_eof_category(self):
        self.assertEqual(token_category(TokenKind.EOF), "EOF")

    def test_invalid_argument_raises_type_error(self):
        with self.assertRaises(TypeError):
            token_category("not_a_kind")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 集成测试：source + tokens 一起使用
# ---------------------------------------------------------------------------
class TestSourceAndTokensIntegration(unittest.TestCase):
    """集成测试：模拟 Lexer 产出 Token 的完整流程（手工构造，非真实扫描）。"""

    def test_build_token_stream_from_source_text(self):
        """从 SourceText 构造一串 Token，验证位置和字段完整。"""
        source = SourceText(name="<test>", text="SELECT id FROM t;")

        tokens = [
            Token(
                kind=TokenKind.KW_SELECT, lexeme="SELECT", value=None,
                span=SourceSpan(
                    start=SourcePos(1, 1, 0), end=SourcePos(1, 7, 6),
                    source_name=source.name,
                ),
            ),
            Token(
                kind=TokenKind.IDENT, lexeme="id", value="id",
                span=SourceSpan(
                    start=SourcePos(1, 8, 7), end=SourcePos(1, 10, 9),
                    source_name=source.name,
                ),
            ),
            Token(
                kind=TokenKind.KW_FROM, lexeme="FROM", value=None,
                span=SourceSpan(
                    start=SourcePos(1, 11, 10), end=SourcePos(1, 15, 14),
                    source_name=source.name,
                ),
            ),
            Token(
                kind=TokenKind.IDENT, lexeme="t", value="t",
                span=SourceSpan(
                    start=SourcePos(1, 16, 15), end=SourcePos(1, 17, 16),
                    source_name=source.name,
                ),
            ),
            Token(
                kind=TokenKind.SEMICOLON, lexeme=";", value=None,
                span=SourceSpan(
                    start=SourcePos(1, 17, 16), end=SourcePos(1, 18, 17),
                    source_name=source.name,
                ),
            ),
            Token(
                kind=TokenKind.EOF, lexeme="", value=None,
                span=SourceSpan(
                    start=SourcePos(1, 18, 17), end=SourcePos(1, 18, 17),
                    source_name=source.name,
                ),
            ),
        ]

        self.assertEqual(len(tokens), 6)
        self.assertEqual(tokens[0].kind, TokenKind.KW_SELECT)
        self.assertEqual(tokens[0].span.start.offset, 0)
        self.assertEqual(tokens[0].span.end.offset, 6)
        for token in tokens[:-1]:
            extracted = source.text[token.span.start.offset : token.span.end.offset]
            self.assertEqual(
                extracted, token.lexeme,
                f"Token {token.kind.name} 的 span 提取文本 '{extracted}' 与 lexeme '{token.lexeme}' 不一致",
            )

    def test_token_span_source_name_matches_source_text(self):
        """Token.span.source_name 必须等于所属 SourceText.name（工作计划 6.1 节）。"""
        source = SourceText(name="examples/demo.sql", text="SELECT 1;")
        token = Token(
            kind=TokenKind.KW_SELECT, lexeme="SELECT", value=None,
            span=SourceSpan(
                start=SourcePos(1, 1, 0), end=SourcePos(1, 7, 6),
                source_name=source.name,
            ),
        )
        self.assertEqual(token.span.source_name, source.name)


if __name__ == "__main__":
    unittest.main()
