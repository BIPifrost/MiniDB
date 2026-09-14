"""只消费赵凯航的 AST；未提供的新类不在此处定义替身。
待提供：TypeDecl、ConstraintDecl、Assignment、UpdateStmt、CreateIndexStmt、
DescribeStmt、ExplainStmt、IsNullExpr，字段遵循优化计划第7.1节。
"""
from minidb.compiler import ast
from minidb.compiler._checks import Check
from minidb.core.schema import TypeSpec, DataType
from minidb.core.expressions import ExprOp


def is_ast(node, name):
    """只接受正式模块公开的类；测试替身必须由测试显式注入该模块。"""
    return isinstance(node, getattr(ast, name, ()))


def literal_type(node):
    # 只读过渡旧LiteralExpr，待前端升级后可删除data_type分支。
    value = node.type_spec if hasattr(node, "type_spec") else node.data_type
    return TypeSpec(value) if isinstance(value, DataType) else value


def validate_ast(stmt):
    check = Check("Semantic.analyze")
    names = ("CreateTableStmt", "InsertStmt", "SelectStmt", "DeleteStmt",
             "UpdateStmt", "CreateIndexStmt", "DescribeStmt", "ExplainStmt")
    check.require(any(is_ast(stmt, name) for name in names), "stmt", "正式Statement", type(stmt).__name__)
    check.span(stmt.span)
    if is_ast(stmt, "ExplainStmt"):
        check.require(any(is_ast(stmt.statement, kind) for kind in
                          ("InsertStmt", "SelectStmt", "DeleteStmt", "UpdateStmt")),
                      "statement", "可EXPLAIN语句", type(stmt.statement).__name__)
        check.span(stmt.statement.span, parent=stmt.span)
        validate_ast(stmt.statement)
        return
    name = stmt.table if any(is_ast(stmt, kind) for kind in
                            ("UpdateStmt", "CreateIndexStmt", "DescribeStmt")) else stmt.table_name
    _name(name, stmt.span, check)
    if is_ast(stmt, "CreateTableStmt"):
        check.require(type(stmt.columns) is tuple, "columns", "tuple", type(stmt.columns).__name__)
        for column in stmt.columns:
            check.require(is_ast(column, "ColumnDecl"), "column", "ColumnDecl", type(column).__name__)
            check.span(column.span, parent=stmt.span)
            _name(column.name, column.span, check)
            if hasattr(column, "type_decl"):
                check.require(is_ast(column.type_decl, "TypeDecl"), "type_decl", "TypeDecl", type(column.type_decl).__name__)
                check.span(column.type_decl.span, parent=column.span)
                check.require(isinstance(column.type_decl.kind, DataType), "kind", "DataType", column.type_decl.kind)
                check.require(type(column.constraints) is tuple, "constraints", "tuple", type(column.constraints).__name__)
                for constraint in column.constraints:
                    check.require(is_ast(constraint, "ConstraintDecl"), "constraint", "ConstraintDecl", type(constraint).__name__)
                    check.span(constraint.span, parent=column.span)
                    if constraint.value is not None:
                        check.require(is_ast(constraint.value, "LiteralExpr"), "default", "LiteralExpr", type(constraint.value).__name__)
                        _expression(constraint.value, constraint.span, check)
            else:
                check.require(isinstance(column.data_type, DataType), "data_type", "DataType", column.data_type)
                check.span(column.type_span, parent=column.span)
    elif is_ast(stmt, "InsertStmt"):
        _names(stmt.columns, stmt.span, check)
        check.require(type(stmt.values) is tuple and bool(stmt.values), "values", "非空tuple", type(stmt.values).__name__)
        for value in stmt.values:
            check.require(is_ast(value, "LiteralExpr"), "value", "LiteralExpr", type(value).__name__)
            _expression(value, stmt.span, check)
    elif is_ast(stmt, "SelectStmt"):
        check.require(type(stmt.select_all) is bool and type(stmt.columns) is tuple,
                      "select_all/columns", "bool、tuple", type(stmt.columns).__name__)
        if stmt.select_all:
            check.require(not stmt.columns, "columns", "星号时为空", len(stmt.columns))
        else:
            _names(stmt.columns, stmt.span, check)
    elif is_ast(stmt, "UpdateStmt"):
        check.require(type(stmt.assignments) is tuple and bool(stmt.assignments),
                      "assignments", "非空tuple", type(stmt.assignments).__name__)
        for assignment in stmt.assignments:
            check.require(is_ast(assignment, "Assignment"), "assignment", "Assignment", type(assignment).__name__)
            check.span(assignment.span, parent=stmt.span)
            _name(assignment.target, assignment.span, check)
            check.require(is_ast(assignment.value, "IdentifierExpr") or is_ast(assignment.value, "LiteralExpr"),
                          "value", "列引用或常量", type(assignment.value).__name__)
            _expression(assignment.value, assignment.span, check)
    elif is_ast(stmt, "CreateIndexStmt"):
        _name(stmt.name, stmt.span, check)
        _name(stmt.column, stmt.span, check)
        check.require(type(stmt.unique) is bool, "unique", "bool", stmt.unique)
    predicate = stmt.predicate if is_ast(stmt, "UpdateStmt") else getattr(stmt, "where", None)
    if predicate is not None:
        _expression(predicate, stmt.span, check)


def _name(name, parent, check):
    check.require(is_ast(name, "NameRef"), "name", "NameRef", type(name).__name__)
    check.require(type(name.text) is str, "name.text", "str", type(name.text).__name__)
    check.span(name.span, parent=parent)


def _names(names, parent, check):
    check.require(type(names) is tuple and bool(names), "columns", "非空tuple", type(names).__name__)
    for name in names:
        _name(name, parent, check)


def _expression(expr, parent, check):
    pending, active, done = [(expr, parent, False)], set(), set()
    while pending:
        node, enclosing, leaving = pending.pop()
        if leaving:
            active.remove(id(node))
            done.add(id(node))
            continue
        check.require(any(is_ast(node, kind) for kind in
                          ("IdentifierExpr", "LiteralExpr", "UnaryExpr", "BinaryExpr", "IsNullExpr")),
                      "expression", "正式Expr", type(node).__name__)
        check.require(id(node) not in active, "expression", "无环AST", type(node).__name__)
        check.span(node.span, parent=enclosing)
        if id(node) in done:
            continue
        if is_ast(node, "IdentifierExpr"):
            check.require(type(node.name) is str, "name", "str", type(node.name).__name__)
        elif is_ast(node, "LiteralExpr"):
            typ = literal_type(node)
            check.require(isinstance(typ, TypeSpec) or typ is None and node.value is None,
                          "type_spec", "TypeSpec或未定型NULL", typ)
        else:
            check.span(node.op_span, parent=node.span)
            if is_ast(node, "IsNullExpr"):
                check.require(type(node.negated) is bool, "negated", "bool", node.negated)
            else:
                valid = node.op is ExprOp.NOT if is_ast(node, "UnaryExpr") else (
                    isinstance(node.op, ExprOp) and node.op is not ExprOp.NOT)
                check.require(valid, "op", "节点对应的ExprOp", node.op)
            children = (node.left, node.right) if is_ast(node, "BinaryExpr") else (node.operand,)
            active.add(id(node))
            pending.append((node, enclosing, True))
            pending.extend((child, node.span, False) for child in reversed(children))
            continue
        done.add(id(node))
