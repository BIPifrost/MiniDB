"""SQL 词法分析器。

由赵凯航维护，张振复核。将 SQL 文本惰性切分为 Token 流。

接口（工作计划第 7.1 节）：
    Lexer.scan(source: SourceText) -> Iterator[Token]

设计要点：
- Lexer 是无状态工厂，每次 scan() 创建独立的 _Scanner 实例。
- _Scanner 持有全部扫描状态（_source/_text/_offset/_line/_column）。
- 多个 generator 交错消费时互不干扰，符合工作计划第 15.3 节
  "每份新的 SourceText 使用新的扫描器和解析器状态"的要求。

规则依据：
- 第 2.2 节：标识符 [A-Za-z_][A-Za-z0-9_]*，最多 64 字符；关键字大小写不敏感；
  字符串单引号包围，双单引号转义；-- 行注释和 /* */ 块注释（不嵌套）
- 第 15.3 节：位置规则（行/列从 1 开始，offset 从 0 开始按 Unicode 字符计数，
  CRLF 算一次换行但 offset 占 2 字符）；数字词法 [0-9]+ 或 [0-9]+\\.[0-9]+；
  非法数字片段整体报 INVALID_NUMBER；EOF 只产生一次
- 第 1.3 节第 7 条：3.14 在 Lexer 中识别为 DECIMAL_LITERAL，Parser 再拒绝
- 第 15.12 节：错误码 INVALID_CHARACTER / UNTERMINATED_STRING /
  UNTERMINATED_COMMENT / INVALID_NUMBER / IDENTIFIER_TOO_LONG
"""

from typing import Iterator

from minidb.core.errors import (
    IDENTIFIER_TOO_LONG,
    INVALID_CHARACTER,
    INVALID_NUMBER,
    UNTERMINATED_COMMENT,
    UNTERMINATED_STRING,
    DbError,
    ErrorStage,
)
from minidb.core.source import SourcePos, SourceSpan, SourceText
from minidb.core.tokens import KEYWORDS, Token, TokenKind


class Lexer:
    """SQL 词法分析器（无状态工厂）。

    每次 scan() 调用创建独立的 _Scanner 实例，返回其 generator。
    同一个 Lexer 实例可以安全地产生多个互不干扰的 generator。
    """

    def __init__(self) -> None:
        # 无状态：所有扫描状态由 _Scanner 实例持有
        pass

    def scan(self, source: SourceText) -> Iterator[Token]:
        """惰性扫描 SQL 文本，依次 yield Token，最后 yield EOF。

        每次调用创建独立的 _Scanner，多个 generator 交错消费互不干扰。
        遇到词法错误时抛出 DbError（带位置）。调用方应在迭代过程中捕获。
        """
        return _Scanner(source).scan()


class _Scanner:
    """内部扫描器，每个实例持有独立的扫描状态。

    不直接对外暴露；由 Lexer.scan() 创建并调用 scan()。
    """

    def __init__(self, source: SourceText) -> None:
        self._source: SourceText = source
        self._text: str = source.text
        self._offset: int = 0
        self._line: int = 1
        self._column: int = 1

    # ------------------------------------------------------------------
    # 公共接口
    # ------------------------------------------------------------------
    def scan(self) -> Iterator[Token]:
        """惰性扫描，yield Token 流，最后 yield EOF。"""
        while self._offset < len(self._text):
            c = self._peek()

            # 空白：跳过（位置已在 _advance 中更新）
            if c == " " or c == "\t" or c == "\r" or c == "\n":
                self._advance()
                continue

            # 标识符或关键字
            if self._is_identifier_start(c):
                yield self._scan_identifier_or_keyword()
                continue

            # 数字（以数字开头）
            if self._is_ascii_digit(c):
                yield self._scan_number()
                continue

            # .5 形式：点后紧接数字，属于非法数字
            if c == "." and self._is_ascii_digit(self._peek_at(1)):
                yield self._scan_number()
                continue

            # 字符串
            if c == "'":
                yield self._scan_string()
                continue

            # 运算符、分隔符、注释（注释在此处被跳过）
            token = self._scan_operator_or_delimiter()
            if token is not None:
                yield token
            # token 为 None 表示是注释，已跳过，继续下一个 Token

        # EOF：只产生一次，Span 起止都等于输入末尾（工作计划第 15.3 节）
        eof_pos = SourcePos(self._line, self._column, self._offset)
        eof_span = SourceSpan(start=eof_pos, end=eof_pos, source_name=self._source.name)
        yield Token(kind=TokenKind.EOF, lexeme="", value=None, span=eof_span)

    # ------------------------------------------------------------------
    # 位置与字符操作
    # ------------------------------------------------------------------
    def _peek(self) -> str | None:
        """查看当前字符，不前进。越界返回 None。"""
        if self._offset < len(self._text):
            return self._text[self._offset]
        return None

    def _peek_at(self, relative: int) -> str | None:
        """查看相对当前位置的字符，不前进。越界返回 None。"""
        pos = self._offset + relative
        if 0 <= pos < len(self._text):
            return self._text[pos]
        return None

    def _advance(self) -> str:
        """前进一个字符单元，更新行/列/offset。

        CRLF（\\r\\n）当作一次换行处理：offset 增加 2，line 增加 1，
        column 重置为 1（工作计划第 6.1 节、第 15.3 节）。
        单独的 \\r 或 \\n 也算一次换行。
        """
        c = self._text[self._offset]

        if c == "\r":
            # 检查是否是 \r\n
            if self._offset + 1 < len(self._text) and self._text[self._offset + 1] == "\n":
                self._offset += 2  # \r\n 在 offset 中占两个字符
            else:
                self._offset += 1  # 单独的 \r
            self._line += 1
            self._column = 1
            return c
        elif c == "\n":
            self._offset += 1
            self._line += 1
            self._column = 1
            return c
        else:
            self._offset += 1
            self._column += 1
            return c

    def _make_span(self, start_line: int, start_col: int, start_offset: int) -> SourceSpan:
        """从起始位置到当前位置创建 SourceSpan。"""
        return SourceSpan(
            start=SourcePos(start_line, start_col, start_offset),
            end=SourcePos(self._line, self._column, self._offset),
            source_name=self._source.name,
        )

    def _make_token(
        self,
        kind: TokenKind,
        lexeme: str,
        value: str | None,
        start_line: int,
        start_col: int,
        start_offset: int,
    ) -> Token:
        """创建带位置的 Token。"""
        return Token(
            kind=kind,
            lexeme=lexeme,
            value=value,
            span=self._make_span(start_line, start_col, start_offset),
        )

    def _lexical_error(
        self,
        code: str,
        message: str,
        start_line: int,
        start_col: int,
        start_offset: int,
        context: dict | None = None,
    ) -> DbError:
        """构造带位置的词法错误。"""
        if context is None:
            context = {}
        return DbError(
            stage=ErrorStage.LEXICAL,
            code=code,
            message=message,
            span=self._make_span(start_line, start_col, start_offset),
            context=context,
        )

    # ------------------------------------------------------------------
    # 字符分类
    # ------------------------------------------------------------------
    @staticmethod
    def _is_ascii_digit(c: str | None) -> bool:
        return c is not None and "0" <= c <= "9"

    @staticmethod
    def _is_ascii_alpha(c: str | None) -> bool:
        return c is not None and (("a" <= c <= "z") or ("A" <= c <= "Z"))

    @classmethod
    def _is_identifier_start(cls, c: str | None) -> bool:
        return cls._is_ascii_alpha(c) or c == "_"

    @classmethod
    def _is_identifier_char(cls, c: str | None) -> bool:
        return cls._is_ascii_alpha(c) or cls._is_ascii_digit(c) or c == "_"

    # ------------------------------------------------------------------
    # 标识符与关键字
    # ------------------------------------------------------------------
    def _scan_identifier_or_keyword(self) -> Token:
        """扫描标识符或关键字。

        标识符规则：[A-Za-z_][A-Za-z0-9_]*，最多 64 字符（工作计划第 2.2 节）。
        关键字大小写不敏感，通过 KEYWORDS 映射表判断（工作计划第 2.2 节）。
        标识符 lexeme 保留原始大小写，value 也是原始名字（工作计划第 6.1 节）。
        """
        start_line = self._line
        start_col = self._column
        start_offset = self._offset

        while self._is_identifier_char(self._peek()):
            self._advance()

        lexeme = self._text[start_offset : self._offset]

        # 标识符长度检查：最多 64 字符（工作计划第 2.2 节、第 15.12 节）
        if len(lexeme) > 64:
            raise self._lexical_error(
                IDENTIFIER_TOO_LONG,
                f"标识符长度超过 64 个字符：{lexeme[:20]}...",
                start_line,
                start_col,
                start_offset,
                context={"lexeme": lexeme, "max_length": 64},
            )

        # 关键字判断：转小写后查映射表（工作计划第 2.2 节）
        lower = lexeme.lower()
        if lower in KEYWORDS:
            kind = KEYWORDS[lower]
            return self._make_token(kind, lexeme, None, start_line, start_col, start_offset)

        # 普通标识符：value 为原始名字（工作计划第 6.1 节）
        return self._make_token(
            TokenKind.IDENT, lexeme, lexeme, start_line, start_col, start_offset
        )

    # ------------------------------------------------------------------
    # 数字
    # ------------------------------------------------------------------
    def _scan_number(self) -> Token:
        """扫描整数字面量或小数字面量。

        合法形式（工作计划第 15.3 节）：
        - [0-9]+ → INTEGER_LITERAL
        - [0-9]+\\.[0-9]+ → DECIMAL_LITERAL

        非法形式（整体报 INVALID_NUMBER，工作计划第 15.3 节）：
        - 12abc、1e3、1_234（数字后接字母/下划线）
        - 1.2.3（多个小数点）
        - .5（点前无数字）
        - 1.（点后无数字）

        整数和小数的 value 都保留原始数字串（str 类型），不转 int/float
        （工作计划第 15.3 节）。
        """
        start_line = self._line
        start_col = self._column
        start_offset = self._offset

        c = self._peek()

        # 情况 A：以数字开头
        if self._is_ascii_digit(c):
            # 扫描第一段连续数字
            while self._is_ascii_digit(self._peek()):
                self._advance()

            # 检查是否是小数形式：数字.数字
            if self._peek() == "." and self._is_ascii_digit(self._peek_at(1)):
                self._advance()  # 消费 .
                while self._is_ascii_digit(self._peek()):
                    self._advance()

                # 小数后如果还有字母、下划线或另一个点 → 非法数字
                if self._is_number_trailing_char(self._peek()):
                    self._consume_number_trailing()
                    lexeme = self._text[start_offset : self._offset]
                    raise self._lexical_error(
                        INVALID_NUMBER,
                        f"非法数字：{lexeme!r}",
                        start_line,
                        start_col,
                        start_offset,
                        context={"lexeme": lexeme},
                    )

                # 合法小数
                lexeme = self._text[start_offset : self._offset]
                return self._make_token(
                    TokenKind.DECIMAL_LITERAL, lexeme, lexeme, start_line, start_col, start_offset
                )

            # 不是小数：检查整数后是否有非法尾随字符
            if self._is_number_trailing_char(self._peek()):
                self._consume_number_trailing()
                lexeme = self._text[start_offset : self._offset]
                raise self._lexical_error(
                    INVALID_NUMBER,
                    f"非法数字：{lexeme!r}",
                    start_line,
                    start_col,
                    start_offset,
                    context={"lexeme": lexeme},
                )

            # 合法整数
            lexeme = self._text[start_offset : self._offset]
            return self._make_token(
                TokenKind.INTEGER_LITERAL, lexeme, lexeme, start_line, start_col, start_offset
            )

        # 情况 B：以 . 开头且后面是数字（.5 形式）→ 非法数字
        if c == "." and self._is_ascii_digit(self._peek_at(1)):
            self._consume_number_trailing()
            lexeme = self._text[start_offset : self._offset]
            raise self._lexical_error(
                INVALID_NUMBER,
                f"非法数字：{lexeme!r}",
                start_line,
                start_col,
                start_offset,
                context={"lexeme": lexeme},
            )

        # 不应该到达这里
        raise self._lexical_error(
            INVALID_CHARACTER,
            f"遇到非法字符：{c!r}",
            start_line,
            start_col,
            start_offset,
            context={"character": c},
        )

    @classmethod
    def _is_number_trailing_char(cls, c: str | None) -> bool:
        """数字后面的非法尾随字符：ASCII 字母、下划线、点。"""
        return cls._is_ascii_alpha(c) or c == "_" or c == "."

    def _consume_number_trailing(self) -> None:
        """消费数字后的非法尾随片段 [A-Za-z0-9_.]+。"""
        while True:
            c = self._peek()
            if self._is_ascii_alpha(c) or self._is_ascii_digit(c) or c == "_" or c == ".":
                self._advance()
            else:
                break

    # ------------------------------------------------------------------
    # 字符串
    # ------------------------------------------------------------------
    def _scan_string(self) -> Token:
        """扫描字符串字面量。

        规则（工作计划第 2.2 节）：
        - 单引号包围
        - 两个连续单引号 '' 表示一个单引号（转义）
        - 内容不转小写，不解释反斜杠转义
        - 到 EOF 未闭合 → UNTERMINATED_STRING
        - lexeme 保存原始写法（含外层单引号和双单引号）
        - value 保存处理双单引号后的内容
        - 字符串内部的换行内容保持原样（工作计划第 2.1 节）
        """
        start_line = self._line
        start_col = self._column
        start_offset = self._offset

        self._advance()  # 消费开头的 '

        value_chars: list[str] = []
        while True:
            c = self._peek()
            if c is None:
                # 到 EOF 仍未闭合（工作计划第 15.12 节）
                raise self._lexical_error(
                    UNTERMINATED_STRING,
                    "字符串未闭合",
                    start_line,
                    start_col,
                    start_offset,
                )
            if c == "'":
                self._advance()  # 消费 '
                if self._peek() == "'":
                    # 双单引号转义：'' → '
                    self._advance()
                    value_chars.append("'")
                else:
                    # 字符串结束
                    break
            else:
                # _advance() consumes CRLF as one logical newline and advances
                # over both source characters; preserve both in string value.
                if c == "\r" and self._peek_at(1) == "\n":
                    value_chars.append("\r\n")
                else:
                    value_chars.append(c)
                self._advance()

        lexeme = self._text[start_offset : self._offset]
        value = "".join(value_chars)
        return self._make_token(
            TokenKind.STRING_LITERAL, lexeme, value, start_line, start_col, start_offset
        )

    # ------------------------------------------------------------------
    # 运算符、分隔符与注释
    # ------------------------------------------------------------------
    def _scan_operator_or_delimiter(self) -> Token | None:
        """扫描运算符、分隔符，或跳过注释。

        返回 Token；如果是注释则返回 None（表示已跳过，继续下一个 Token）。

        运算符最长匹配（工作计划第 5.1 节任务 2）：
        - = / == → EQ
        - != → NE，<> → NE
        - < / <= / <>，> / >=
        - + - * /
        注释：
        - -- → 行注释（到行末）
        - /* → 块注释（到 */，不嵌套）
        """
        start_line = self._line
        start_col = self._column
        start_offset = self._offset
        c = self._peek()

        # = 和 ==
        if c == "=":
            self._advance()
            if self._peek() == "=":
                self._advance()
                return self._make_token(TokenKind.EQ, "==", None, start_line, start_col, start_offset)
            return self._make_token(TokenKind.EQ, "=", None, start_line, start_col, start_offset)

        # ! 和 !=
        if c == "!":
            self._advance()
            if self._peek() == "=":
                self._advance()
                return self._make_token(TokenKind.NE, "!=", None, start_line, start_col, start_offset)
            # 单独的 ! 是非法字符
            raise self._lexical_error(
                INVALID_CHARACTER,
                "遇到非法字符：'!'",
                start_line,
                start_col,
                start_offset,
                context={"character": "!"},
            )

        # <, <=, <>
        if c == "<":
            self._advance()
            if self._peek() == "=":
                self._advance()
                return self._make_token(TokenKind.LE, "<=", None, start_line, start_col, start_offset)
            if self._peek() == ">":
                self._advance()
                return self._make_token(TokenKind.NE, "<>", None, start_line, start_col, start_offset)
            return self._make_token(TokenKind.LT, "<", None, start_line, start_col, start_offset)

        # >, >=
        if c == ">":
            self._advance()
            if self._peek() == "=":
                self._advance()
                return self._make_token(TokenKind.GE, ">=", None, start_line, start_col, start_offset)
            return self._make_token(TokenKind.GT, ">", None, start_line, start_col, start_offset)

        # +
        if c == "+":
            self._advance()
            return self._make_token(TokenKind.PLUS, "+", None, start_line, start_col, start_offset)

        # - 和 --（行注释）
        if c == "-":
            self._advance()
            if self._peek() == "-":
                # -- 行注释：跳到行末（工作计划第 2.2 节）
                self._skip_line_comment()
                return None
            return self._make_token(TokenKind.MINUS, "-", None, start_line, start_col, start_offset)

        # *
        if c == "*":
            self._advance()
            return self._make_token(TokenKind.STAR, "*", None, start_line, start_col, start_offset)

        # / 和 /*（块注释）
        if c == "/":
            self._advance()
            if self._peek() == "*":
                # /* 块注释：跳到 */，不嵌套（工作计划第 2.2 节）
                self._skip_block_comment(start_line, start_col, start_offset)
                return None
            return self._make_token(TokenKind.SLASH, "/", None, start_line, start_col, start_offset)

        # 分隔符
        if c == "(":
            self._advance()
            return self._make_token(TokenKind.LPAREN, "(", None, start_line, start_col, start_offset)
        if c == ")":
            self._advance()
            return self._make_token(TokenKind.RPAREN, ")", None, start_line, start_col, start_offset)
        if c == ",":
            self._advance()
            return self._make_token(TokenKind.COMMA, ",", None, start_line, start_col, start_offset)
        if c == ";":
            self._advance()
            return self._make_token(TokenKind.SEMICOLON, ";", None, start_line, start_col, start_offset)

        # 其他字符都是非法字符
        raise self._lexical_error(
            INVALID_CHARACTER,
            f"遇到非法字符：{c!r}",
            start_line,
            start_col,
            start_offset,
            context={"character": c},
        )

    def _skip_line_comment(self) -> None:
        """跳过 -- 行注释：到行末（\\n 或 EOF），不消费换行符。"""
        while True:
            c = self._peek()
            if c is None or c == "\n" or c == "\r":
                # 停在换行符前，让主循环的空白处理来消费换行
                return
            self._advance()

    def _skip_block_comment(self, start_line: int, start_col: int, start_offset: int) -> None:
        """跳过 /* 块注释：到 */，不嵌套。

        到 EOF 未闭合 → UNTERMINATED_COMMENT（工作计划第 15.12 节）。
        调用时已消费 /*。
        """
        while True:
            c = self._peek()
            if c is None:
                raise self._lexical_error(
                    UNTERMINATED_COMMENT,
                    "块注释未闭合",
                    start_line,
                    start_col,
                    start_offset,
                )
            if c == "*" and self._peek_at(1) == "/":
                self._advance()  # *
                self._advance()  # /
                return
            self._advance()
