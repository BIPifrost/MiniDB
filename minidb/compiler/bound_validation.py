"""复核绑定树的类型、列号、NULL传播和位置；无存储副作用。"""
from minidb.compiler.bound import (
    BoundColumn, BoundLiteral, BoundUnary, BoundBinary, BoundIsNull, BoundAssignment,
    BoundCreate, BoundInsert, BoundSelect, BoundDelete, BoundUpdate, BoundCreateIndex,
    BoundDescribe, BoundExplain,
)
from minidb.compiler._checks import Check
from minidb.core.schema import DataType, TypeSpec
from minidb.core.expressions import ExprOp, resolve_result_type
from minidb.core.value_rules import assignment_allowed


def validate_expression(expr, table, check, parent=None):
    pending = [(expr, parent, False)]
    active, done = set(), set()
    while pending:
        node, enclosing, leaving = pending.pop()
        if leaving:
            active.remove(id(node))
            if isinstance(node, BoundIsNull):
                check.require(type(node.negated) is bool, "negated", "bool", node.negated)
                check.require(node.operand.type_spec is not None, "operand.type_spec", "已定型操作数", None)
            else:
                children = (node.operand,) if isinstance(node, BoundUnary) else (node.left, node.right)
                if any(child.type_spec is None for child in children):
                    check.require(isinstance(node, BoundBinary) and node.op in
                                  (ExprOp.EQ, ExprOp.NE, ExprOp.LT, ExprOp.LE, ExprOp.GT, ExprOp.GE)
                                  and all(isinstance(child, BoundLiteral) and child.value is None
                                          and child.type_spec is None for child in children),
                                  "type_spec", "仅NULL与NULL比较保留未定型", node)
                result = resolve_result_type(node.op, tuple(child.type_spec for child in children))
                check.require(result is DataType.BOOL and node.type_spec == TypeSpec(DataType.BOOL),
                              "data_type", "合法BOOL运算", node.type_spec)
                check.require(type(node.nullable) is bool and node.nullable == any(child.nullable for child in children),
                              "nullable", "与操作数NULL传播一致", node.nullable)
            done.add(id(node))
            continue
        check.require(isinstance(node, (BoundColumn, BoundLiteral, BoundUnary, BoundBinary, BoundIsNull)),
                      "expression", "BoundExpr", type(node).__name__)
        check.require(id(node) not in active, "expression", "无环", type(node).__name__)
        check.span(node.span, parent=enclosing)
        if id(node) in done:
            continue
        if isinstance(node, BoundColumn):
            check.require(type(node.index) is int and 0 <= node.index < len(table.schema.columns),
                          "index", "表列号", node.index)
            column = table.schema.columns[node.index]
            check.require(node.type_spec == column.type_spec and type(node.nullable) is bool
                          and node.nullable == column.nullable, "column", "表列类型及nullable", node)
        elif isinstance(node, BoundLiteral):
            check.value(node.value, node.type_spec)
        else:
            check.span(node.op_span, "op_span", node.span)
            if isinstance(node, BoundUnary):
                check.require(node.op is ExprOp.NOT, "op", "NOT", node.op)
            elif isinstance(node, BoundBinary):
                check.require(isinstance(node.op, ExprOp) and node.op is not ExprOp.NOT,
                              "op", "二元ExprOp", node.op)
            active.add(id(node))
            pending.append((node, enclosing, True))
            children = (node.left, node.right) if isinstance(node, BoundBinary) else (node.operand,)
            pending.extend((child, node.span, False) for child in reversed(children))
            continue
        done.add(id(node))


def validate_predicate(predicate, table, check, parent=None):
    if predicate is None:
        return
    validate_expression(predicate, table, check, parent)
    check.require(predicate.type_spec == TypeSpec(DataType.BOOL),
                  "predicate", "已按上下文绑定的BOOL或NULL", predicate.type_spec)


def validate_assignments(assignments, table, check, parent):
    check.require(type(assignments) is tuple and bool(assignments), "assignments", "非空tuple", assignments)
    seen = set()
    for assignment in assignments:
        check.require(isinstance(assignment, BoundAssignment), "assignment", "BoundAssignment", type(assignment).__name__)
        index = assignment.column_index
        check.require(type(index) is int and 0 <= index < len(table.schema.columns)
                      and index not in seen, "column_index", "不重复的有效列号", index)
        seen.add(index)
        check.span(assignment.span, parent=parent)
        check.require(isinstance(assignment.value, (BoundColumn, BoundLiteral)),
                      "assignment.value", "列引用或常量", type(assignment.value).__name__)
        validate_expression(assignment.value, table, check, assignment.span)
        target = table.schema.columns[index]
        check.require(assignment_allowed(assignment.value.type_spec, target.type_spec),
                      "assignment.type_spec", "可赋值类型", assignment.value.type_spec)
        if isinstance(assignment.value, BoundLiteral):
            check.value(assignment.value.value, target.type_spec, nullable=target.nullable)


def validate_index_target(name, table, column_index, unique, check):
    check.name(name)
    check.table(table)
    check.require(type(column_index) is int and 0 <= column_index < len(table.schema.columns),
                  "column_index", "有效列号", column_index)
    check.require(type(unique) is bool, "unique", "bool", unique)


def validate_bound(bound, *, plan=False):
    check = Check("validate_bound", plan=plan)
    kinds = (BoundCreate, BoundInsert, BoundSelect, BoundDelete, BoundUpdate,
             BoundCreateIndex, BoundDescribe, BoundExplain)
    check.require(isinstance(bound, kinds), "bound", "完整BoundStatement", type(bound).__name__)
    check.span(bound.span)
    if isinstance(bound, BoundExplain):
        check.require(isinstance(bound.statement, (BoundInsert, BoundSelect, BoundDelete, BoundUpdate)),
                      "statement", "可EXPLAIN的非DDL语句", type(bound.statement).__name__)
        check.span(bound.statement.span, parent=bound.span)
        validate_bound(bound.statement, plan=plan)
    elif isinstance(bound, BoundCreate):
        check.name(bound.table_name)
        check.schema(bound.schema)
    else:
        check.table(bound.table)
        if isinstance(bound, BoundInsert):
            check.row(bound.row, bound.table.schema)
        elif isinstance(bound, BoundCreateIndex):
            validate_index_target(bound.name, bound.table, bound.column_index, bound.unique, check)
        elif isinstance(bound, (BoundSelect, BoundDelete, BoundUpdate)):
            if isinstance(bound, BoundSelect):
                check.projection(bound.table, bound.projection, bound.output_columns)
            if isinstance(bound, BoundUpdate):
                validate_assignments(bound.assignments, bound.table, check, bound.span)
            validate_predicate(bound.predicate, bound.table, check, bound.span)
