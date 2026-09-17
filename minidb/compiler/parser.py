"""MiniDB 的递归下降 SQL Parser。

Parser 只把 Token 组织为 ``compiler.ast`` 中的不可变节点，不查询目录、
不判断表列是否存在，也不执行数据库操作。支持 v2 工作计划第 3 节的
CREATE TABLE、INSERT、SELECT、DELETE、UPDATE、CREATE INDEX、DESCRIBE
和 EXPLAIN 文法。
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from decimal import Decimal

from minidb.compiler.ast import (
    Assignment,
    BinaryExpr,
    ColumnDecl,
    ConstraintDecl,
    CreateIndexStmt,
    CreateTableStmt,
    DeleteStmt,
    DescribeStmt,
    ExplainStmt,
    IdentifierExpr,
    InsertStmt,
    IsNullExpr,
    LiteralExpr,
    NameRef,
    SelectStmt,
    Statement,
    TypeDecl,
    UnaryExpr,
    UpdateStmt,
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
from minidb.core.schema import DataType, TypeSpec
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

_STATEMENT_STARTS = frozenset({
    TokenKind.KW_CREATE,
    TokenKind.KW_INSERT,
    TokenKind.KW_SELECT,
    TokenKind.KW_DELETE,
    TokenKind.KW_UPDATE,
    TokenKind.KW_DESCRIBE,
    TokenKind.KW_EXPLAIN,
})

_EXPLAINABLE_STARTS = frozenset({
    TokenKind.KW_INSERT,
    TokenKind.KW_SELECT,
    TokenKind.KW_DELETE,
    TokenKind.KW_UPDATE,
})

_LITERAL_STARTS = frozenset({
    TokenKind.MINUS,
    TokenKind.INTEGER_LITERAL,
    TokenKind.DECIMAL_LITERAL,
    TokenKind.STRING_LITERAL,
    TokenKind.KW_TRUE,
    TokenKind.KW_FALSE,
    TokenKind.KW_NULL,
    TokenKind.KW_DATE,
})

# 这些词属于明确延期的 SQL 能力；遇到时报告“不支持”，而不是把它们
# 当成名称或一般拼写错误。v2 已支持的关键字不放进此集合。
_UNSUPPORTED_KEYWORDS = frozenset({
    TokenKind.KW_JOIN,
    TokenKind.KW_ORDER,
    TokenKind.KW_BY,
    TokenKind.KW_GROUP,
    TokenKind.KW_DISTINCT,
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
    """无状态 Parser；每次公开调用都创建独立的 Token 前瞻流。"""

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
                try:
                    self._synchronize(stream)
                except DbError as sync_error:
                    if sync_error.stage is ErrorStage.LEXICAL:
                        errors.append(sync_error)
                        stopped_on_lexical_error = True
                        break
                    raise
            except StopIteration:
                errors.append(self._unexpected_eof_from_stream(stream))
                break

        return SyntaxCheckResult(valid_count, tuple(errors), stopped_on_lexical_error)

    def _parse_statement(self, stream: _TokenStream) -> Statement:
        kind = stream.peek().kind
        if kind is TokenKind.KW_CREATE:
            return self._parse_create(stream)
        if kind is TokenKind.KW_INSERT:
            return self._parse_insert(stream)
        if kind is TokenKind.KW_SELECT:
            return self._parse_select(stream)
        if kind is TokenKind.KW_DELETE:
            return self._parse_delete(stream)
        if kind is TokenKind.KW_UPDATE:
            return self._parse_update(stream)
        if kind is TokenKind.KW_DESCRIBE:
            return self._parse_describe(stream)
        if kind is TokenKind.KW_EXPLAIN:
            return self._parse_explain(stream)
        self._unexpected(stream, set(_STATEMENT_STARTS))

    def _parse_create(self, stream: _TokenStream) -> CreateTableStmt | CreateIndexStmt:
        start = self._expect(stream, TokenKind.KW_CREATE)
        if stream.accept(TokenKind.KW_TABLE) is not None:
            return self._parse_create_table_after_prefix(stream, start)

        # 处理索引创建
        unique = stream.accept(TokenKind.KW_UNIQUE) is not None
        if unique or stream.peek().kind is TokenKind.KW_INDEX:
            self._expect(stream, TokenKind.KW_INDEX)
            return self._parse_create_index_after_prefix(stream, start, unique)

        self._unexpected(stream, {TokenKind.KW_TABLE, TokenKind.KW_UNIQUE, TokenKind.KW_INDEX})

    def _parse_create_table_after_prefix(
        self, stream: _TokenStream, start: Token
    ) -> CreateTableStmt:
        table_name = self._parse_name(stream)
        self._expect(stream, TokenKind.LPAREN)
        columns = [self._parse_column_decl(stream)]

        # 如果有逗号，就继续取
        while stream.accept(TokenKind.COMMA) is not None:
            columns.append(self._parse_column_decl(stream))
        self._expect(stream, TokenKind.RPAREN)
        end = self._expect(stream, TokenKind.SEMICOLON)
        return CreateTableStmt(table_name, tuple(columns), _join_span(start.span, end.span))

    def _parse_create_index_after_prefix(
        self, stream: _TokenStream, start: Token, unique: bool
    ) -> CreateIndexStmt:
        name = self._parse_name(stream)
        self._expect(stream, TokenKind.KW_ON)
        table = self._parse_name(stream)
        self._expect(stream, TokenKind.LPAREN)
        column = self._parse_name(stream)
        self._expect(stream, TokenKind.RPAREN)
        end = self._expect(stream, TokenKind.SEMICOLON)
        return CreateIndexStmt(name, table, column, unique, _join_span(start.span, end.span))

    def _parse_column_decl(self, stream: _TokenStream) -> ColumnDecl:
        name = self._parse_name(stream)
        type_decl = self._parse_type_decl(stream)
        constraints: list[ConstraintDecl] = []
        while stream.peek().kind in {
            TokenKind.KW_PRIMARY,
            TokenKind.KW_UNIQUE,
            TokenKind.KW_NOT,
            TokenKind.KW_NULL,
            TokenKind.KW_DEFAULT,
        }:
            constraints.append(self._parse_constraint(stream))
        end_span = constraints[-1].span if constraints else type_decl.span
        return ColumnDecl(name, type_decl, tuple(constraints), _join_span(name.span, end_span))

    def _parse_type_decl(self, stream: _TokenStream) -> TypeDecl:
        token = stream.peek()
        if token.kind in (TokenKind.KW_INT, TokenKind.KW_BOOL, TokenKind.KW_DATE):
            stream.take()
            kind = {
                TokenKind.KW_INT: DataType.INT,
                TokenKind.KW_BOOL: DataType.BOOL,
                TokenKind.KW_DATE: DataType.DATE,
            }[token.kind]
            return TypeDecl(kind, None, None, None, token.span)

        if token.kind is TokenKind.KW_VARCHAR:
            stream.take()
            length = None
            end = token
            if stream.accept(TokenKind.LPAREN) is not None:
                length_token, length = self._parse_uint_parameter(stream)
                end = self._expect(stream, TokenKind.RPAREN)
            return TypeDecl(
                DataType.VARCHAR,
                length,
                None,
                None,
                _join_span(token.span, end.span),
            )

        if token.kind is TokenKind.KW_DECIMAL:
            stream.take()
            self._expect(stream, TokenKind.LPAREN)
            _, precision = self._parse_uint_parameter(stream)
            self._expect(stream, TokenKind.COMMA)
            _, scale = self._parse_uint_parameter(stream)
            end = self._expect(stream, TokenKind.RPAREN)
            return TypeDecl(
                DataType.DECIMAL,
                None,
                precision,
                scale,
                _join_span(token.span, end.span),
            )

        self._unexpected(
            stream,
            {
                TokenKind.KW_INT,
                TokenKind.KW_VARCHAR,
                TokenKind.KW_BOOL,
                TokenKind.KW_DATE,
                TokenKind.KW_DECIMAL,
            },
        )

    def _parse_constraint(self, stream: _TokenStream) -> ConstraintDecl:
        start = stream.peek()
        if start.kind is TokenKind.KW_PRIMARY:
            stream.take()
            end = self._expect(stream, TokenKind.KW_KEY)
            return ConstraintDecl("PRIMARY_KEY", None, _join_span(start.span, end.span))
        if start.kind is TokenKind.KW_UNIQUE:
            stream.take()
            return ConstraintDecl("UNIQUE", None, start.span)
        if start.kind is TokenKind.KW_NOT:
            stream.take()
            end = self._expect(stream, TokenKind.KW_NULL)
            return ConstraintDecl("NOT_NULL", None, _join_span(start.span, end.span))
        if start.kind is TokenKind.KW_NULL:
            stream.take()
            return ConstraintDecl("NULL", None, start.span)
        if start.kind is TokenKind.KW_DEFAULT:
            stream.take()
            value = self._parse_literal(stream)
            return ConstraintDecl("DEFAULT", value, _join_span(start.span, value.span))
        self._unexpected(
            stream,
            {
                TokenKind.KW_PRIMARY,
                TokenKind.KW_UNIQUE,
                TokenKind.KW_NOT,
                TokenKind.KW_NULL,
                TokenKind.KW_DEFAULT,
            },
        )

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

    def _parse_update(self, stream: _TokenStream) -> UpdateStmt:
        start = self._expect(stream, TokenKind.KW_UPDATE)
        table = self._parse_name(stream)
        self._expect(stream, TokenKind.KW_SET)
        assignments = [self._parse_assignment(stream)]
        while stream.accept(TokenKind.COMMA) is not None:
            assignments.append(self._parse_assignment(stream))
        predicate = None
        if stream.accept(TokenKind.KW_WHERE) is not None:
            predicate = self._parse_expression(stream)
        end = self._expect(stream, TokenKind.SEMICOLON)
        return UpdateStmt(table, tuple(assignments), predicate, _join_span(start.span, end.span))

    def _parse_assignment(self, stream: _TokenStream) -> Assignment:
        target = self._parse_name(stream)
        equals = self._expect(stream, TokenKind.EQ)
        if equals.lexeme != "=":
            raise DbError(
                ErrorStage.SYNTAX,
                UNEXPECTED_TOKEN,
                "UPDATE SET 赋值只允许单个等号 =",
                equals.span,
                {"expected": ["EQ(=)"], "actual": "EQ(==)"},
            )
        token = stream.peek()
        if token.kind is TokenKind.IDENT:
            stream.take()
            value = IdentifierExpr(token.value if token.value is not None else token.lexeme, token.span)
        elif token.kind in _LITERAL_STARTS:
            value = self._parse_literal(stream)
        elif token.kind is TokenKind.KW_DEFAULT:
            self._unsupported(stream, token)
        else:
            self._unexpected(stream, {TokenKind.IDENT, *_LITERAL_STARTS})
        if stream.peek().kind in {
            TokenKind.PLUS,
            TokenKind.MINUS,
            TokenKind.STAR,
            TokenKind.SLASH,
        }:
            # v2 的 SET 右侧只允许一个列引用或常量，不支持算术赋值。
            self._unsupported(stream, stream.peek())
        return Assignment(target, value, _join_span(target.span, value.span))

    def _parse_describe(self, stream: _TokenStream) -> DescribeStmt:
        start = self._expect(stream, TokenKind.KW_DESCRIBE)
        table = self._parse_name(stream)
        end = self._expect(stream, TokenKind.SEMICOLON)
        return DescribeStmt(table, _join_span(start.span, end.span))

    def _parse_explain(self, stream: _TokenStream) -> ExplainStmt:
        start = self._expect(stream, TokenKind.KW_EXPLAIN)
        kind = stream.peek().kind
        if kind is TokenKind.KW_INSERT:
            statement = self._parse_insert(stream)
        elif kind is TokenKind.KW_SELECT:
            statement = self._parse_select(stream)
        elif kind is TokenKind.KW_DELETE:
            statement = self._parse_delete(stream)
        elif kind is TokenKind.KW_UPDATE:
            statement = self._parse_update(stream)
        elif kind is TokenKind.KW_CREATE:
            self._unsupported(stream, stream.peek())
        else:
            self._unexpected(stream, set(_EXPLAINABLE_STARTS))
        return ExplainStmt(statement, _join_span(start.span, statement.span))

    def _parse_name_list(self, stream: _TokenStream) -> list[NameRef]:
        names = [self._parse_name(stream)]
        while stream.accept(TokenKind.COMMA) is not None:
            names.append(self._parse_name(stream))
        return names

    def _parse_name(self, stream: _TokenStream) -> NameRef:
        token = stream.peek()
        if token.kind is not TokenKind.IDENT:
            self._unexpected(stream, {TokenKind.IDENT})
        stream.take()
        return NameRef(token.value if token.value is not None else token.lexeme, token.span)

    def _parse_uint_parameter(self, stream: _TokenStream) -> tuple[Token, int]:
        token = self._expect(stream, TokenKind.INTEGER_LITERAL)
        raw = token.value if token.value is not None else token.lexeme
        # 所有合法类型参数都不超过 1024。超长文本使用一个明确越界的
        # 哨兵交给 Semantic 报 INVALID_TYPE_PARAMETER，避免 Python 对超长
        # 十进制字符串的转换限制改变错误阶段。
        digits = raw.lstrip("0") or "0"
        value = 1025 if len(digits) > 4 else int(digits)
        return token, value

    def _parse_literal(self, stream: _TokenStream) -> LiteralExpr:
        minus = stream.accept(TokenKind.MINUS)
        token = stream.peek()
        span = _join_span(minus.span, token.span) if minus is not None else token.span

        if token.kind is TokenKind.INTEGER_LITERAL:
            stream.take()
            raw = token.value if token.value is not None else token.lexeme
            value = _parse_int64(raw, negative=minus is not None, span=span)
            return LiteralExpr(value, TypeSpec(DataType.INT), span)

        if token.kind is TokenKind.DECIMAL_LITERAL:
            stream.take()
            raw = token.value if token.value is not None else token.lexeme
            value = Decimal(("-" if minus is not None else "") + raw)
            whole, fraction = raw.split(".", 1)
            precision = min(18, max(1, len(whole) + len(fraction)))
            scale = min(18, len(fraction))
            return LiteralExpr(
                value,
                TypeSpec(DataType.DECIMAL, precision=precision, scale=scale),
                span,
            )

        if minus is not None:
            self._unexpected(stream, {TokenKind.INTEGER_LITERAL, TokenKind.DECIMAL_LITERAL})

        if token.kind is TokenKind.STRING_LITERAL:
            stream.take()
            return LiteralExpr(
                token.value if token.value is not None else "",
                TypeSpec(DataType.VARCHAR),
                token.span,
            )
        if token.kind in (TokenKind.KW_TRUE, TokenKind.KW_FALSE):
            stream.take()
            return LiteralExpr(token.kind is TokenKind.KW_TRUE, TypeSpec(DataType.BOOL), token.span)
        if token.kind is TokenKind.KW_NULL:
            stream.take()
            return LiteralExpr(None, None, token.span)
        if token.kind is TokenKind.KW_DATE:
            start = stream.take()
            string = self._expect(stream, TokenKind.STRING_LITERAL)
            return LiteralExpr(
                string.value if string.value is not None else "",
                TypeSpec(DataType.DATE),
                _join_span(start.span, string.span),
            )

        self._unexpected(stream, set(_LITERAL_STARTS))

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
            return self._parse_predicate(stream)
        operand = self._parse_not(stream)
        return UnaryExpr(ExprOp.NOT, operand, operator.span, _join_span(operator.span, operand.span))

    def _parse_predicate(self, stream: _TokenStream):
        left = self._parse_primary(stream)
        if stream.peek().kind is TokenKind.KW_IS:
            start = stream.take()
            negated = stream.accept(TokenKind.KW_NOT) is not None
            end = self._expect(stream, TokenKind.KW_NULL)
            op_span = _join_span(start.span, end.span)
            return IsNullExpr(left, negated, op_span, _join_span(left.span, end.span))

        token = stream.peek()
        op = _COMPARISON_OPS.get(token.kind)
        if op is None:
            if token.kind in (TokenKind.PLUS, TokenKind.MINUS, TokenKind.STAR, TokenKind.SLASH):
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
        if token.kind in _LITERAL_STARTS:
            return self._parse_literal(stream)
        if token.kind is TokenKind.LPAREN:
            stream.take()
            expression = self._parse_expression(stream)
            self._expect(stream, TokenKind.RPAREN)
            return expression
        self._unexpected(
            stream,
            {TokenKind.IDENT, TokenKind.LPAREN, *_LITERAL_STARTS},
        )

    def _expect(self, stream: _TokenStream, kind: TokenKind) -> Token:
        token = stream.peek()
        if token.kind is not kind:
            self._unexpected(stream, {kind})
        return stream.take()

    def _unexpected(self, stream: _TokenStream, expected: set[TokenKind]) -> None:
        token = stream.peek()
        if token.kind in _UNSUPPORTED_KEYWORDS:
            self._unsupported(stream, token)
        code = UNEXPECTED_EOF if token.kind is TokenKind.EOF else UNEXPECTED_TOKEN
        context = {"expected": sorted(kind.name for kind in expected)}
        if token.kind is not TokenKind.EOF:
            context["actual"] = token.kind.name
        raise DbError(
            ErrorStage.SYNTAX,
            code,
            "输入在此处不符合 SQL 语法",
            token.span,
            context,
        )

    @staticmethod
    def _unsupported(stream: _TokenStream, token: Token) -> None:
        del stream
        raise DbError(
            ErrorStage.SYNTAX,
            UNSUPPORTED_FEATURE,
            f"暂不支持语法：{token.lexeme}",
            token.span,
            {"feature": token.lexeme},
        )

    @staticmethod
    def _synchronize(stream: _TokenStream) -> None:
        """找到下一个分号并消费，用作语法检查的恢复点。"""

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
        token = stream._current
        if token is None:
            raise RuntimeError("Token 流在 EOF 前结束且没有位置")
        return DbError(
            ErrorStage.SYNTAX,
            UNEXPECTED_EOF,
            "Token 流在语句结束前结束",
            token.span,
            {"expected": [TokenKind.SEMICOLON.name]},
        )


def _join_span(start: SourceSpan, end: SourceSpan) -> SourceSpan:
    """组合同一输入中的两个范围，保留首尾位置和来源名称。"""

    return SourceSpan(start=start.start, end=end.end, source_name=start.source_name)


def _parse_int64(raw: str, *, negative: bool, span: SourceSpan) -> int:
    """按字符串检查 INT64，避免超长数字触发解释器转换限制。"""

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
