"""MiniDB 的递归下降 SQL Parser。

Parser 只把 Token 组织成 ``compiler.ast`` 中的不可变节点，不查询 Catalog、
不检查列和表是否存在，也不执行数据库操作。Semantic 会在下一阶段复核
手工 AST 的结构和名称含义。

公开接口遵循工作计划第 7.1、15.14 节：
``Parser().iter_statements(tokens)`` 按语句惰性地产生 AST；
``Parser().check_syntax(tokens)`` 只做语法检查并在分号处恢复。
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator

from minidb.compiler.ast import (
    BinaryExpr,
    ColumnDecl,
    CreateTableStmt,
    DeleteStmt,
    IdentifierExpr,
    InsertStmt,
    LiteralExpr,
    NameRef,
    SelectStmt,
    Statement,
    UnaryExpr,
)
from minidb.core.diagnostics import SyntaxCheckResult
from minidb.core.errors import (
    DbError,
    ErrorStage,
    INT_OUT_OF_RANGE,
    UNEXPECTED_EOF,
    UNEXPECTED_TOKEN,
    UNSUPPORTED_FEATURE,
)
from minidb.core.expressions import ExprOp
from minidb.core.schema import DataType
from minidb.core.source import SourceSpan
from minidb.core.tokens import Token, TokenKind


_COMPARISON_OPS: dict[TokenKind, ExprOp] = {
    TokenKind.EQ: ExprOp.EQ,
    TokenKind.NE: ExprOp.NE,
    TokenKind.LT: ExprOp.LT,
    TokenKind.LE: ExprOp.LE,
    TokenKind.GT: ExprOp.GT,
    TokenKind.GE: ExprOp.GE,
}

_SUPPORTED_STARTS = frozenset({
    TokenKind.KW_CREATE,
    TokenKind.KW_INSERT,
    TokenKind.KW_SELECT,
    TokenKind.KW_DELETE,
})

_EXTENDED_KEYWORDS = frozenset({
    TokenKind.KW_UPDATE,
    TokenKind.KW_JOIN,
    TokenKind.KW_ORDER,
    TokenKind.KW_BY,
    TokenKind.KW_GROUP,
    TokenKind.KW_DISTINCT,
    TokenKind.KW_NULL,
    TokenKind.KW_TRUE,
    TokenKind.KW_FALSE,
    TokenKind.KW_EXPLAIN,
})


class _TokenStream:
    """带一个前瞻 Token 的惰性流，不预读后续语句。"""

    def __init__(self, tokens: Iterable[Token]) -> None:
        self._tokens = iter(tokens)
        self._current: Token | None = None

    def peek(self) -> Token:
        if self._current is None:
            self._current = next(self._tokens)
        return self._current

    def take(self) -> Token:
        token = self.peek()
        self._current = None
        return token

    def accept(self, kind: TokenKind) -> Token | None:
        if self.peek().kind is kind:
            return self.take()
        return None


class Parser:
    """无状态 Parser；每次调用都会创建独立的前瞻流。"""

    def __init__(self) -> None:
        pass

    def iter_statements(self, tokens: Iterable[Token]) -> Iterator[Statement]:
        """逐条解析完整语句；空分号被跳过，错误原样向调用方传播。"""
        stream = _TokenStream(tokens)
        while True:
            current = stream.peek()
            if current.kind is TokenKind.EOF:
                return
            if stream.accept(TokenKind.SEMICOLON) is not None:
                continue
            yield self._parse_statement(stream)

    def check_syntax(self, tokens: Iterable[Token]) -> SyntaxCheckResult:
        """只检查语法；语法错误跳到分号，词法错误终止剩余检查。"""
        stream = _TokenStream(tokens)
        errors: list[DbError] = []
        valid_count = 0
        stopped_on_lexical_error = False

        while True:
            try:
                current = stream.peek()
                if current.kind is TokenKind.EOF:
                    break
                if stream.accept(TokenKind.SEMICOLON) is not None:
                    continue
                self._parse_statement(stream)
                valid_count += 1
            except DbError as error:
                errors.append(error)
                if error.stage is ErrorStage.LEXICAL:
                    stopped_on_lexical_error = True
                    break
                # 恢复过程中仍可能需要向前读取 Token；如果此时 Lexer
                # 发现非法字符，必须把这个词法错误也记录下来，而不是
                # 让 check_syntax() 直接把异常抛给调用方。
                try:
                    self._synchronize(stream)
                except DbError as sync_error:
                    if sync_error.stage is ErrorStage.LEXICAL:
                        errors.append(sync_error)
                        stopped_on_lexical_error = True
                        break
                    raise
            except StopIteration:
                # 正式 Lexer 总会产生 EOF；把不完整的替代 Token 流报告为语法错误。
                errors.append(self._unexpected_eof_from_stream(stream))
                break

        return SyntaxCheckResult(valid_count, tuple(errors), stopped_on_lexical_error)

    def _parse_statement(self, stream: _TokenStream) -> Statement:
        start = stream.peek()
        if start.kind is TokenKind.KW_CREATE:
            return self._parse_create(stream)
        if start.kind is TokenKind.KW_INSERT:
            return self._parse_insert(stream)
        if start.kind is TokenKind.KW_SELECT:
            return self._parse_select(stream)
        if start.kind is TokenKind.KW_DELETE:
            return self._parse_delete(stream)
        if start.kind in _EXTENDED_KEYWORDS:
            self._unsupported(stream, start)
        self._unexpected(stream, {TokenKind.KW_CREATE, TokenKind.KW_INSERT,
                                  TokenKind.KW_SELECT, TokenKind.KW_DELETE})

    def _parse_create(self, stream: _TokenStream) -> CreateTableStmt:
        start = self._expect(stream, TokenKind.KW_CREATE)
        self._expect(stream, TokenKind.KW_TABLE)
        table_name = self._parse_name(stream)
        self._expect(stream, TokenKind.LPAREN)

        columns = [self._parse_column_decl(stream)]
        while stream.accept(TokenKind.COMMA) is not None:
            columns.append(self._parse_column_decl(stream))
        self._expect(stream, TokenKind.RPAREN)
        end = self._expect(stream, TokenKind.SEMICOLON)
        return CreateTableStmt(table_name, tuple(columns), _join_span(start.span, end.span))

    def _parse_column_decl(self, stream: _TokenStream) -> ColumnDecl:
        name = self._parse_name(stream)
        type_token = stream.peek()
        if type_token.kind is TokenKind.KW_INT:
            stream.take()
            data_type = DataType.INT
        elif type_token.kind is TokenKind.KW_VARCHAR:
            stream.take()
            data_type = DataType.VARCHAR
        else:
            self._unexpected(stream, {TokenKind.KW_INT, TokenKind.KW_VARCHAR})
        return ColumnDecl(name, data_type, type_token.span, _join_span(name.span, type_token.span))

    def _parse_insert(self, stream: _TokenStream) -> InsertStmt:
        start = self._expect(stream, TokenKind.KW_INSERT)
        self._expect(stream, TokenKind.KW_INTO)
        table_name = self._parse_name(stream)
        self._expect(stream, TokenKind.LPAREN)
        columns = self._parse_name_list(stream)
        self._expect(stream, TokenKind.RPAREN)
        self._expect(stream, TokenKind.KW_VALUES)
        self._expect(stream, TokenKind.LPAREN)
        values = [self._parse_literal(stream)]
        while stream.accept(TokenKind.COMMA) is not None:
            values.append(self._parse_literal(stream))
        self._expect(stream, TokenKind.RPAREN)
        end = self._expect(stream, TokenKind.SEMICOLON)
        return InsertStmt(table_name, tuple(columns), tuple(values), _join_span(start.span, end.span))

    def _parse_select(self, stream: _TokenStream) -> SelectStmt:
        start = self._expect(stream, TokenKind.KW_SELECT)
        if stream.accept(TokenKind.STAR) is not None:
            select_all = True
            columns: tuple[NameRef, ...] = ()
        else:
            select_all = False
            columns = tuple(self._parse_name_list(stream))
        self._expect(stream, TokenKind.KW_FROM)
        table_name = self._parse_name(stream)
        where = None
        if stream.accept(TokenKind.KW_WHERE) is not None:
            where = self._parse_expression(stream)
        end = self._expect(stream, TokenKind.SEMICOLON)
        return SelectStmt(table_name, select_all, columns, where, _join_span(start.span, end.span))

    def _parse_delete(self, stream: _TokenStream) -> DeleteStmt:
        start = self._expect(stream, TokenKind.KW_DELETE)
        self._expect(stream, TokenKind.KW_FROM)
        table_name = self._parse_name(stream)
        where = None
        if stream.accept(TokenKind.KW_WHERE) is not None:
            where = self._parse_expression(stream)
        end = self._expect(stream, TokenKind.SEMICOLON)
        return DeleteStmt(table_name, where, _join_span(start.span, end.span))

    def _parse_name_list(self, stream: _TokenStream) -> list[NameRef]:
        names = [self._parse_name(stream)]
        while stream.accept(TokenKind.COMMA) is not None:
            names.append(self._parse_name(stream))
        return names

    def _parse_name(self, stream: _TokenStream) -> NameRef:
        token = stream.peek()
        if token.kind is not TokenKind.IDENT:
            if token.kind is TokenKind.DECIMAL_LITERAL or token.kind in _EXTENDED_KEYWORDS:
                self._unsupported(stream, token)
            self._unexpected(stream, {TokenKind.IDENT})
        stream.take()
        # Lexer 对 IDENT 保证 value 为原始名字；lexeme 是防御性回退。
        return NameRef(token.value if token.value is not None else token.lexeme, token.span)

    def _parse_literal(self, stream: _TokenStream) -> LiteralExpr:
        minus = stream.accept(TokenKind.MINUS)
        token = stream.peek()
        if token.kind is TokenKind.INTEGER_LITERAL:
            stream.take()
            raw = token.value if token.value is not None else token.lexeme
            value = _parse_int64(raw, negative=minus is not None, span=_join_span(minus.span, token.span) if minus else token.span)
            span = _join_span(minus.span, token.span) if minus else token.span
            return LiteralExpr(value, DataType.INT, span)
        if token.kind is TokenKind.DECIMAL_LITERAL:
            self._unsupported(stream, token)
        if minus is not None:
            self._unexpected(stream, {TokenKind.INTEGER_LITERAL})
        if token.kind is TokenKind.STRING_LITERAL:
            stream.take()
            return LiteralExpr(token.value if token.value is not None else "", DataType.VARCHAR, token.span)
        self._unexpected(stream, {TokenKind.INTEGER_LITERAL, TokenKind.STRING_LITERAL})

    def _parse_expression(self, stream: _TokenStream):
        return self._parse_or(stream)

    def _parse_or(self, stream: _TokenStream):
        left = self._parse_and(stream)
        while True:
            operator = stream.accept(TokenKind.KW_OR)
            if operator is None:
                return left
            right = self._parse_and(stream)
            left = BinaryExpr(ExprOp.OR, left, right, operator.span, _join_span(left.span, right.span))

    def _parse_and(self, stream: _TokenStream):
        left = self._parse_not(stream)
        while True:
            operator = stream.accept(TokenKind.KW_AND)
            if operator is None:
                return left
            right = self._parse_not(stream)
            left = BinaryExpr(ExprOp.AND, left, right, operator.span, _join_span(left.span, right.span))

    def _parse_not(self, stream: _TokenStream):
        operator = stream.accept(TokenKind.KW_NOT)
        if operator is None:
            return self._parse_comparison(stream)
        operand = self._parse_not(stream)
        return UnaryExpr(ExprOp.NOT, operand, operator.span, _join_span(operator.span, operand.span))

    def _parse_comparison(self, stream: _TokenStream):
        left = self._parse_primary(stream)
        token = stream.peek()
        op = _COMPARISON_OPS.get(token.kind)
        if op is None:
            if token.kind in (TokenKind.PLUS, TokenKind.MINUS,
                              TokenKind.STAR, TokenKind.SLASH):
                self._unsupported(stream, token)
            return left
        operator = stream.take()
        right = self._parse_primary(stream)
        return BinaryExpr(op, left, right, operator.span, _join_span(left.span, right.span))

    def _parse_primary(self, stream: _TokenStream):
        token = stream.peek()
        if token.kind is TokenKind.IDENT:
            stream.take()
            return IdentifierExpr(token.value if token.value is not None else token.lexeme, token.span)
        if token.kind in (TokenKind.INTEGER_LITERAL, TokenKind.STRING_LITERAL,
                          TokenKind.MINUS, TokenKind.DECIMAL_LITERAL):
            return self._parse_literal(stream)
        if token.kind is TokenKind.LPAREN:
            stream.take()
            expression = self._parse_expression(stream)
            self._expect(stream, TokenKind.RPAREN)
            return expression
        if token.kind in _EXTENDED_KEYWORDS:
            self._unsupported(stream, token)
        self._unexpected(stream, {TokenKind.IDENT, TokenKind.INTEGER_LITERAL,
                                  TokenKind.STRING_LITERAL, TokenKind.LPAREN})

    def _expect(self, stream: _TokenStream, kind: TokenKind) -> Token:
        token = stream.peek()
        if token.kind is not kind:
            self._unexpected(stream, {kind})
        return stream.take()

    def _unexpected(self, stream: _TokenStream, expected: set[TokenKind]) -> None:
        token = stream.peek()
        # 工作计划 15.15：小数和已识别但暂未实现的扩展关键字，
        # 无论出现在普通 expect 位置还是表达式位置，都优先报告
        # UNSUPPORTED_FEATURE，而不是笼统的 UNEXPECTED_TOKEN。
        if token.kind is TokenKind.DECIMAL_LITERAL or token.kind in _EXTENDED_KEYWORDS:
            self._unsupported(stream, token)
        code = UNEXPECTED_EOF if token.kind is TokenKind.EOF else UNEXPECTED_TOKEN
        context = {"expected": sorted(kind.name for kind in expected)}
        if token.kind is not TokenKind.EOF:
            context["actual"] = token.kind.name
        raise DbError(ErrorStage.SYNTAX, code,
                      "输入在此处不符合 SQL 语法", token.span, context)

    def _unsupported(self, stream: _TokenStream, token: Token) -> None:
        raise DbError(ErrorStage.SYNTAX, UNSUPPORTED_FEATURE,
                      f"暂不支持语法：{token.lexeme}", token.span,
                      {"feature": token.lexeme})

    @staticmethod
    def _synchronize(stream: _TokenStream) -> None:
        """保留当前分号作为同步点，找到后消费它再解析下一条。"""
        while True:
            token = stream.peek()
            if token.kind is TokenKind.EOF:
                return
            if token.kind is TokenKind.SEMICOLON:
                stream.take()
                return
            stream.take()

    @staticmethod
    def _unexpected_eof_from_stream(stream: _TokenStream) -> DbError:
        # 正式 Token 流不会走到这里；该分支仍保持统一的 SYNTAX 错误形态。
        token = stream._current
        if token is None:
            raise RuntimeError("Token 流在 EOF 前结束且没有位置")
        return DbError(ErrorStage.SYNTAX, UNEXPECTED_EOF,
                       "Token 流在语句结束前结束", token.span,
                       {"expected": [TokenKind.SEMICOLON.name]})


def _join_span(start: SourceSpan, end: SourceSpan) -> SourceSpan:
    """组合同一输入中的两个范围，保留首尾位置和来源名称。"""
    return SourceSpan(start=start.start, end=end.end, source_name=start.source_name)


def _parse_int64(raw: str, *, negative: bool, span: SourceSpan) -> int:
    """在转换为 Python int 前按字符串检查 INT64，避免超长数字触发解释器限制。"""
    digits = raw.lstrip("0") or "0"
    limit = "9223372036854775808" if negative else "9223372036854775807"
    if len(digits) > len(limit) or (len(digits) == len(limit) and digits > limit):
        raise DbError(
            ErrorStage.SYNTAX,
            INT_OUT_OF_RANGE,
            "整数超出 INT64 范围",
            span,
            {
                "value_repr": ("-" if negative else "") + raw,
                "min_value": -9223372036854775808,
                "max_value": 9223372036854775807,
            },
        )
    value = int(digits)
    return -value if negative and value else value


__all__ = ["Parser"]
