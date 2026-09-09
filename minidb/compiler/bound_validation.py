"""张振：在消费 Bound 前检查类型、列索引及输出元数据。"""

from minidb.compiler._checks import Check
from minidb.compiler.bound import (
    BoundColumn, BoundCreate, BoundExpr,
    BoundInsert, BoundLiteral, BoundSelect, BoundStatement, BoundUnary,
)
from minidb.core.expressions import ExprOp, resolve_result_type
from minidb.core.schema import DataType


def validate_expression(expr, table, check: Check, parent=None) -> None:
    """使用显式栈遍历，所有分支都检查，不能按 AND/OR 的值短路。"""
    # 每项是 (节点, 父范围, 是否离开节点)。离开时子节点已全部通过检查。
    pending = [(expr, parent, False)]
    active: set[int] = set()
    # 只复用本次校验中已完全通过的节点，不能把某张表的结论带到下一次调用。
    # 记录 id，避免数据类的递归比较/哈希再次遍历子树。
    validated: set[int] = set()
    while pending:
        node, enclosing, leaving = pending.pop()
        check.require(isinstance(node, BoundExpr), "predicate", "BoundExpr", type(node).__name__)
        if leaving:
            # active 只记录当前祖先链，可以拒绝环，同时允许不同分支共享叶节点。
            active.remove(id(node))
            operands = (node.operand,) if isinstance(node, BoundUnary) else (node.left, node.right)
            result = resolve_result_type(node.op, tuple(child.data_type for child in operands))
            check.require(result is DataType.BOOL and node.data_type is result, "predicate.data_type", "合法 BOOL 运算", repr(node.data_type))
            validated.add(id(node))
            continue
        check.require(id(node) not in active, "predicate", "无环表达式", type(node).__name__)
        # 共享节点可能有不同父节点，每条父子关系都必须检查，不能被缓存跳过。
        check.span(node.span, parent=enclosing)
        if id(node) in validated:
            # 共用同一子树的另一分支可复用结论；尚未完成的祖先节点不能走到这里。
            continue
        if isinstance(node, BoundColumn):
            check.require(type(node.index) is int and 0 <= node.index < len(table.schema.columns), "predicate.index", "扫描表中的有效列序号", node.index)
            check.require(node.data_type is table.schema.columns[node.index].data_type, "predicate.data_type", "与扫描表列类型一致", repr(node.data_type))
        elif isinstance(node, BoundLiteral):
            check.value(node.value, node.data_type, allow_bool=True)
        else:
            allowed = node.op is ExprOp.NOT if isinstance(node, BoundUnary) else isinstance(node.op, ExprOp) and node.op is not ExprOp.NOT
            check.require(allowed, "predicate.op", "节点对应的 ExprOp", repr(node.op))
            check.span(node.op_span, "op_span", node.span)
            active.add(id(node))
            pending.append((node, enclosing, True))
            children = (node.operand,) if isinstance(node, BoundUnary) else (node.left, node.right)
            # 栈先弹出最后放入的元素，倒序压栈才能保持从左到右的检查顺序。
            pending.extend((child, node.span, False) for child in reversed(children))
            continue
        # 列、字面量没有子节点，字段检查完成即可登记。
        validated.add(id(node))


def validate_predicate(predicate, table, check: Check, parent=None) -> None:
    """None 表示没有过滤条件；非空条件必须结构合法且最终类型为 BOOL。"""
    if predicate is not None:
        validate_expression(predicate, table, check, parent)
        check.require(predicate.data_type is DataType.BOOL, "predicate", "BOOL", repr(predicate.data_type))


def validate_bound(bound, *, plan: bool = False) -> None:
    """检查整个已绑定语句，包括表、行、投影和条件，供 Semantic 与 Planner 共用。"""
    check = Check("Planner.build" if plan else "Semantic.analyze", plan=plan)
    check.require(isinstance(bound, BoundStatement), "bound", "BoundStatement", type(bound).__name__)
    check.span(bound.span)
    if isinstance(bound, BoundCreate):
        check.name(bound.table_name)
        check.schema(bound.schema)
        return
    check.table(bound.table)
    if isinstance(bound, BoundInsert):
        check.row(bound.row, bound.table.schema)
    else:
        if isinstance(bound, BoundSelect):
            check.projection(bound.table, bound.projection, bound.output_columns)
        # SELECT 与 DELETE 共用同一套条件及位置检查。
        validate_predicate(bound.predicate, bound.table, check, bound.span)
