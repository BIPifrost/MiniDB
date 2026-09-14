"""索引选择的纯规则，供赵凯航的Optimizer调用；不替他修改Optimizer或执行器。"""
from minidb.compiler.bound import BoundBinary, BoundColumn, BoundLiteral, BoundIsNull
from minidb.compiler.plan import IndexScanPlan
from minidb.compiler.bound_validation import validate_predicate
from minidb.compiler._checks import Check
from minidb.core.expressions import ExprOp
from minidb.core.value_rules import normalize_value, assignment_allowed
from minidb.core.errors import DbError
from minidb.core.schema import IndexDef


def choose_index_scan(table, predicate, indexes, span):
    """返回确定性的IndexScanPlan或None。调用方必须保留原完整Filter作为residual。

    这里只建议访问路径，不改写原计划。UPDATE的输入仍按8.5节固定为SeqScan/Filter。
    不能无损归一化的跨类型边界继续顺序扫描，避免舍入边界改变比较结果。
    """
    check = Check("choose_index_scan", plan=True)
    check.table(table)
    check.span(span)
    check.require(type(indexes) is tuple and all(isinstance(index, IndexDef) for index in indexes),
                  "indexes", "tuple[IndexDef, ...]", type(indexes).__name__)
    validate_predicate(predicate, table, check, span)
    if predicate is None:
        return None
    atoms, stack = [], [predicate]
    while stack:
        node = stack.pop()
        if isinstance(node, BoundBinary) and node.op is ExprOp.AND:
            stack.extend((node.right, node.left))
        elif isinstance(node, BoundIsNull) and not node.negated and isinstance(node.operand, BoundColumn):
            atoms.append((node.operand.index, "NULL", None))
        elif (isinstance(node, BoundBinary) and node.op in
              (ExprOp.EQ, ExprOp.LT, ExprOp.LE, ExprOp.GT, ExprOp.GE)):
            left, right, op = node.left, node.right, node.op
            if isinstance(left, BoundLiteral) and isinstance(right, BoundColumn):
                left, right = right, left
                op = {ExprOp.EQ: ExprOp.EQ, ExprOp.LT: ExprOp.GT, ExprOp.LE: ExprOp.GE,
                      ExprOp.GT: ExprOp.LT, ExprOp.GE: ExprOp.LE}[op]
            if not isinstance(left, BoundColumn) or not isinstance(right, BoundLiteral) or right.value is None:
                return None
            atoms.append((left.index, op, right))
        else:
            # 不在本轮支持的连续区间内，保持原SeqScan。
            return None
    candidates = []
    for index in indexes:
        if index.table_id != table.ref.table_id or index.column_index >= len(table.schema.columns):
            continue
        relevant = [(op, value) for position, op, value in atoms if position == index.column_index]
        if not relevant:
            continue
        spec = table.schema.columns[index.column_index].type_spec
        null_only = any(op == "NULL" for op, _ in relevant)
        lower = upper = None
        lower_inc = upper_inc = False
        usable = True
        if not null_only:
            for op, literal in relevant:
                if not assignment_allowed(literal.type_spec, spec):
                    usable = False
                    break
                try:
                    value = normalize_value(literal.value, spec, nullable=False)
                except (DbError, NotImplementedError):
                    # 优化不是强制转换；不可表示的常量仍由原谓词比较。
                    usable = False
                    break
                bound = BoundLiteral(value, spec, literal.span)
                if op in (ExprOp.EQ, ExprOp.GT, ExprOp.GE):
                    inclusive = op is not ExprOp.GT
                    if lower is None or value > lower.value:
                        lower, lower_inc = bound, inclusive
                    elif value == lower.value:
                        lower_inc = lower_inc and inclusive
                if op in (ExprOp.EQ, ExprOp.LT, ExprOp.LE):
                    inclusive = op is not ExprOp.LT
                    if upper is None or value < upper.value:
                        upper, upper_inc = bound, inclusive
                    elif value == upper.value:
                        upper_inc = upper_inc and inclusive
        if not usable:
            continue
        equality = null_only or (lower is not None and upper is not None
                                and lower.value == upper.value and lower_inc and upper_inc)
        count = 1 if null_only else int(lower is not None) + int(upper is not None)
        plan = IndexScanPlan(table, index, lower is not None, lower, lower_inc,
                             upper is not None, upper, upper_inc, null_only, span)
        candidates.append(((not equality, -count, not index.unique, index.index_id), plan))
    return min(candidates, key=lambda pair: pair[0])[1] if candidates else None
