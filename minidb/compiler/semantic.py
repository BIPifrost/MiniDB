"""张振：把语法树变成已检查、已绑定的语句，不创建表或读写数据页。

入口：Semantic().analyze(stmt: Statement, catalog: CatalogRead) -> BoundStatement。
外部依赖（仅引用，未替其他成员实现）：
* 赵凯航：compiler.ast 的四类语句、四类表达式、NameRef、ColumnDecl；
  字段遵循计划 15.4 节。core.source 提供 SourceSpan/SourcePos。
* 赵凯航：core.tokens.TokenKind 的 KW_* 枚举，以及
  core.errors.DbError(stage, code, message, span, context)、ErrorStage 和错误码。
* 周升荣：core.result.ResultColumn(name: str, data_type: DataType)。

入口直接使用这些正式类型，统一检查结构、名字和表达式类型。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NoReturn

from minidb.compiler._ast_validation import validate_ast
from minidb.compiler._checks import Check
from minidb.compiler.bound import (
    BoundBinary, BoundColumn, BoundCreate, BoundDelete, BoundExpr,
    BoundInsert, BoundLiteral, BoundSelect, BoundStatement, BoundUnary,
)
from minidb.compiler.bound_validation import validate_bound
from minidb.core.catalog_protocol import CatalogRead
from minidb.core.expressions import ExprOp, resolve_result_type
from minidb.core.schema import ColumnDef, DataType, Schema, TableDef, _IDENTIFIER

if TYPE_CHECKING:
    from minidb.compiler.ast import Statement
    from minidb.core.source import SourceSpan


class Semantic:
    """无状态的语义分析器；每次从调用者传入的目录读取最新表定义。"""

    def analyze(self, stmt: Statement, catalog: CatalogRead) -> BoundStatement:
        """检查顺序：AST 结构 → 表名/表存在性 → 语句内容 → 输出契约。"""
        from minidb.compiler import ast

        validate_ast(stmt)
        Check("Semantic.analyze").require(
            isinstance(catalog, CatalogRead), "catalog", "CatalogRead", type(catalog).__name__,
        )
        name = _name(stmt.table_name.text, stmt.table_name.span, table=True)
        table = catalog.find_table(name)

        if isinstance(stmt, ast.CreateTableStmt):
            if table is not None:
                _error("TABLE_EXISTS", "表已经存在", stmt.table_name.span, table_name=name)
            bound = self._create(stmt, name)
        else:
            if table is None:
                _error("TABLE_NOT_FOUND", "找不到这张表", stmt.table_name.span, table_name=name)
            Check("Semantic.analyze").table(table)
            if isinstance(stmt, ast.InsertStmt):
                bound = self._insert(stmt, table)
            elif isinstance(stmt, ast.SelectStmt):
                bound = self._select(stmt, table)
            else:
                bound = BoundDelete(table, self._where(stmt.where, table), stmt.span)

        # 对外只返回满足正式约定的 Bound，不让后续 Planner 猜测缺失字段。
        validate_bound(bound)
        return bound

    def _create(self, stmt, name: str) -> BoundCreate:
        """按声明顺序生成 Schema；这里尚不知道 table_id 和根页号。"""
        count = len(stmt.columns)
        if not 1 <= count <= 64:
            _error("UNSUPPORTED_FEATURE", "建表列数必须为 1 至 64", stmt.span,
                   actual=count, min_value=1, max_value=64, reason="表列数量超出项目范围")
        columns = []
        seen = set()
        for declaration in stmt.columns:
            column_name = _name(declaration.name.text, declaration.name.span)
            if column_name in seen:
                _error("DUPLICATE_COLUMN", "列名重复", declaration.name.span, column_name=column_name)
            seen.add(column_name)
            if declaration.data_type not in (DataType.INT, DataType.VARCHAR):
                _error("UNSUPPORTED_FEATURE", "表字段只支持 INT 和 VARCHAR", declaration.type_span,
                       expected=["INT", "VARCHAR"], actual=declaration.data_type.name)
            columns.append(ColumnDef(column_name, declaration.data_type))
        return BoundCreate(name, Schema(tuple(columns)), stmt.span)

    def _insert(self, stmt, table: TableDef) -> BoundInsert:
        """先核对列集合，再检查值；最后只重排一次，得到 Schema 顺序的 Row。"""
        if len(stmt.columns) != len(stmt.values):
            _error("VALUE_COUNT_MISMATCH", "列数与值数不一致", stmt.span,
                   expected=len(stmt.columns), actual=len(stmt.values))

        indexes = []
        seen = set()
        for reference in stmt.columns:
            name = _name(reference.text, reference.span)
            if name in seen:
                _error("DUPLICATE_INSERT_COLUMN", "INSERT 列名重复", reference.span, column_name=name)
            seen.add(name)
            index, _ = _column(table, name, reference.span)
            indexes.append(index)

        missing = sorted(column.name for column in table.schema.columns if column.name not in seen)
        if missing:
            _error("INSERT_COLUMN_SET_MISMATCH", "INSERT 必须覆盖表中全部列", stmt.span,
                   missing_columns=missing, extra_columns=[], duplicate_columns=[])

        # 例如 SQL 写 (name, age, id)，indexes 就是 [1, 2, 0]。
        # 将 ('Alice', 20, 1) 按这些位置放入，得到 (1, 'Alice', 20)。
        row = [None] * len(table.schema.columns)
        for index, literal in zip(indexes, stmt.values):
            column = table.schema.columns[index]
            if literal.data_type is not column.data_type:
                _error("TYPE_MISMATCH", "插入值与列类型不匹配", literal.span,
                       table_name=table.ref.name, column_name=column.name,
                       expected=column.data_type.name, actual=literal.data_type.name)
            # 原样保存字符串，包括大小写、中文、空格和换行。
            row[index] = literal.value
        return BoundInsert(table, tuple(row), stmt.span)

    def _select(self, stmt, table: TableDef) -> BoundSelect:
        """把星号或列名列表展开成索引；SELECT id,id 的重复列按顺序保留。"""
        from minidb.core.result import ResultColumn

        if stmt.select_all:
            indexes = tuple(range(len(table.schema.columns)))
        else:
            indexes = tuple(
                _column(table, _name(ref.text, ref.span), ref.span)[0]
                for ref in stmt.columns
            )
        output = tuple(
            ResultColumn(table.schema.columns[index].name, table.schema.columns[index].data_type)
            for index in indexes
        )
        # WHERE 仍绑定原表全部列，不能只在 SELECT 选出的列中找 age 等条件列。
        predicate = self._where(stmt.where, table)
        return BoundSelect(table, indexes, output, predicate, stmt.span)

    def _where(self, expr, table: TableDef) -> BoundExpr | None:
        """没有 WHERE 就返回 None；有条件时，其最终结果必须是 BOOL。"""
        if expr is None:
            return None
        bound = self._expression(expr, table)
        if bound.data_type is not DataType.BOOL:
            _error("CONDITION_NOT_BOOL", "WHERE 条件必须产生布尔值", expr.span,
                   actual_type=bound.data_type.name)
        return bound

    def _expression(self, expr, table: TableDef) -> BoundExpr:
        """按左、右、操作符顺序绑定整棵树；语义检查不进行短路求值。"""
        from minidb.compiler import ast

        pending = [(expr, False)]
        completed = {}
        while pending:
            node, leaving = pending.pop()
            # AST 已通过完整结构检查，共享节点可以直接使用本次绑定的结果。
            if id(node) in completed:
                continue
            if isinstance(node, ast.IdentifierExpr):
                name = _name(node.name, node.span)
                index, column = _column(table, name, node.span)
                completed[id(node)] = BoundColumn(index, column.data_type, node.span)
            elif isinstance(node, ast.LiteralExpr):
                completed[id(node)] = BoundLiteral(node.value, node.data_type, node.span)
            elif not leaving:
                # 此时只排定访问顺序；等两个子树都绑定后才能检查当前操作。
                pending.append((node, True))
                children = (node.operand,) if isinstance(node, ast.UnaryExpr) else (node.left, node.right)
                pending.extend((child, False) for child in reversed(children))
            else:
                unary = isinstance(node, ast.UnaryExpr)
                operands = (completed[id(node.operand)],) if unary else (completed[id(node.left)], completed[id(node.right)])
                result_type = _operation_type(node.op, operands, node.op_span)
                if unary:
                    bound = BoundUnary(node.op, operands[0], result_type, node.op_span, node.span)
                else:
                    bound = BoundBinary(node.op, *operands, result_type, node.op_span, node.span)
                completed[id(node)] = bound
        return completed[id(expr)]


def _name(text: str, span: SourceSpan, *, table: bool = False) -> str:
    """只归一化标识符，不处理字符串值；关键字来自前端唯一 TokenKind。"""
    if len(text) > 64:
        _error("IDENTIFIER_TOO_LONG", "标识符超过 64 个字符", span, lexeme=text, max_length=64)
    if _IDENTIFIER.fullmatch(text) is None:
        _error("INVALID_ARGUMENT", "AST 中的名称不是合法标识符", span,
               field="name", expected="ASCII 标识符", actual=text)
    normalized = text.lower()
    if table and normalized.startswith("_sys_"):
        _error("RESERVED_NAME", "用户语句不能访问系统表名", span, table_name=normalized)

    if _is_keyword(text):
        _error("RESERVED_NAME", "关键字不能直接用作名称", span,
               **({"table_name": normalized} if table else {"column_name": normalized}))
    return normalized


def _is_keyword(text: str) -> bool:
    """查询前端的 KW_* 枚举，关键字集合只有 tokens.py 中的一份。"""
    from minidb.core.tokens import TokenKind

    return f"KW_{text.upper()}" in TokenKind.__members__


def _column(table: TableDef, name: str, span: SourceSpan):
    """把列名解析为 (列索引, ColumnDef)，缺列错误准确指向该名字。"""
    found = table.schema.find_column(name)
    if found is None:
        _error("COLUMN_NOT_FOUND", "表中没有这列", span,
               table_name=table.ref.name, column_name=name)
    return found


def _operation_type(op: ExprOp, operands: tuple[BoundExpr, ...], span: SourceSpan) -> DataType:
    """复用公共类型规则，只补充 Semantic 的错误码和操作符位置。"""
    types = tuple(operand.data_type for operand in operands)
    result = resolve_result_type(op, types)
    if result is not None:
        return result
    context = {"operator": op.name}
    if len(types) == 1:
        context.update(expected="BOOL", actual=types[0].name)
    else:
        context.update(left_type=types[0].name, right_type=types[1].name)
        # 这里只描述报错原因；哪些组合合法仍统一由 resolve_result_type 决定。
        if op in (ExprOp.LT, ExprOp.LE, ExprOp.GT, ExprOp.GE):
            expected = ["INT", "INT"]
        elif op in (ExprOp.AND, ExprOp.OR):
            expected = ["BOOL", "BOOL"]
        else:
            expected = "同类型的 INT 或 VARCHAR"
        context.update(
            expected=expected,
            actual=[data_type.name for data_type in types],
        )
    code = "UNSUPPORTED_COMPARISON" if op in (ExprOp.LT, ExprOp.LE, ExprOp.GT, ExprOp.GE) else "TYPE_MISMATCH"
    _error(code, "操作符不接受这些类型", span, **context)


def _error(code: str, message: str, span: SourceSpan, **context) -> NoReturn:
    """唯一的语义报错出口；只引用公共错误码，不定义另一套异常。"""
    from minidb.core import errors

    context.setdefault("operation", "Semantic.analyze")
    raise errors.DbError(errors.ErrorStage.SEMANTIC, getattr(errors, code), message, span, context)
