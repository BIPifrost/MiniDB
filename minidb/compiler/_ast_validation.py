"""张振：检查 Semantic 收到的 AST 结构，防止手工构造的坏节点进入绑定。

这里只消费赵凯航的 compiler/ast.py，不定义 AST，也不解析 SQL。
字段名依据工作计划 15.4 节；依赖的源码位置由 core/source.py 提供。
"""

from minidb.compiler._checks import Check
from minidb.core.expressions import ExprOp
from minidb.core.schema import DataType


def validate_ast(stmt) -> None:
    """先检查完整结构，再由 Semantic 检查名字和类型的业务含义。"""
    # 延迟导入：缺少 AST 时其他张振模块仍可导入，但不能假装能分析 SQL。
    from minidb.compiler import ast

    check = Check("Semantic.analyze")
    statement_types = (ast.CreateTableStmt, ast.InsertStmt, ast.SelectStmt, ast.DeleteStmt)
    check.require(isinstance(stmt, statement_types), "stmt", "四类正式 Statement", type(stmt).__name__)
    check.span(stmt.span)
    _name_ref(stmt.table_name, stmt.span, check)

    if isinstance(stmt, ast.CreateTableStmt):
        check.require(isinstance(stmt.columns, tuple), "columns", "tuple[ColumnDecl, ...]", type(stmt.columns).__name__)
        for column in stmt.columns:
            check.require(isinstance(column, ast.ColumnDecl), "columns", "ColumnDecl", type(column).__name__)
            check.span(column.span, "column.span", stmt.span)
            _name_ref(column.name, column.span, check)
            check.span(column.type_span, "type_span", column.span)
            check.require(isinstance(column.data_type, DataType), "data_type", "DataType", repr(column.data_type))
    elif isinstance(stmt, ast.InsertStmt):
        _names(stmt.columns, stmt.span, check)
        check.require(isinstance(stmt.values, tuple) and bool(stmt.values), "values", "非空 tuple[LiteralExpr, ...]", type(stmt.values).__name__)
        for value in stmt.values:
            check.require(isinstance(value, ast.LiteralExpr), "values", "LiteralExpr", type(value).__name__)
            _expression(value, stmt.span, check)
    elif isinstance(stmt, ast.SelectStmt):
        check.require(type(stmt.select_all) is bool, "select_all", "bool", stmt.select_all)
        check.require(isinstance(stmt.columns, tuple), "columns", "tuple[NameRef, ...]", type(stmt.columns).__name__)
        if stmt.select_all:
            check.require(not stmt.columns, "columns", "SELECT * 的列列表为空", repr(stmt.columns))
        else:
            _names(stmt.columns, stmt.span, check)

    if isinstance(stmt, (ast.SelectStmt, ast.DeleteStmt)) and stmt.where is not None:
        _expression(stmt.where, stmt.span, check)


def _name_ref(name, parent, check: Check) -> None:
    """NameRef 保存原始文字和位置；合法字符、关键字稍后再检查。"""
    from minidb.compiler.ast import NameRef

    check.require(isinstance(name, NameRef), "name", "NameRef", type(name).__name__)
    check.require(isinstance(name.text, str), "name.text", "str", type(name.text).__name__)
    check.span(name.span, "name.span", parent)


def _names(names, parent, check: Check) -> None:
    """INSERT 和显式 SELECT 列表必须是非空的不可变序列。"""
    check.require(isinstance(names, tuple) and bool(names), "columns", "非空 tuple[NameRef, ...]", type(names).__name__)
    for name in names:
        _name_ref(name, parent, check)


def _expression(expr, parent, check: Check) -> None:
    """用栈检查表达式和子节点位置；active 用来识别循环引用。"""
    from minidb.compiler import ast

    # leaving=False 表示第一次进入节点，True 表示其子节点已检查完毕。
    pending = [(expr, parent, False)]
    active: set[int] = set()
    expression_types = (ast.IdentifierExpr, ast.LiteralExpr, ast.UnaryExpr, ast.BinaryExpr)
    while pending:
        node, enclosing, leaving = pending.pop()
        if leaving:
            active.remove(id(node))
            continue
        check.require(isinstance(node, expression_types), "expression", "正式 Expr", type(node).__name__)
        check.require(id(node) not in active, "expression", "无环 AST", type(node).__name__)
        check.span(node.span, "expression.span", enclosing)
        if isinstance(node, ast.IdentifierExpr):
            check.require(isinstance(node.name, str), "name", "str", type(node.name).__name__)
        elif isinstance(node, ast.LiteralExpr):
            # 用户 AST 不允许 BOOL，也不允许把 bool 当作 INT。
            check.value(node.value, node.data_type)
        else:
            unary = isinstance(node, ast.UnaryExpr)
            valid_op = node.op is ExprOp.NOT if unary else isinstance(node.op, ExprOp) and node.op is not ExprOp.NOT
            check.require(valid_op, "op", "节点对应的 ExprOp", repr(node.op))
            check.span(node.op_span, "op_span", node.span)
            active.add(id(node))
            pending.append((node, enclosing, True))
            children = (node.operand,) if unary else (node.left, node.right)
            # 栈后进先出，所以先放右边，才能按左、右的顺序检查。
            pending.extend((child, node.span, False) for child in reversed(children))
