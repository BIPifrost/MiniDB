"""MiniDB 的逻辑计划优化器。

Optimizer 位于 Semantic/Planner 之后、Executor 之前，只查看不可变的 Bound 和
Plan 结构，不读取数据页、不访问目录，也不修改传入的原计划。第一版实现工作
计划第 10.4 节规定的三类安全规则：常量比较折叠、布尔化简和恒真 Filter 消除。
成本优化路径（第 10.4 节扩展）：调用方可选传入 ``TableStats`` 只读快照，
按成本决定是否采用 ``choose_index_scan`` 建议的索引扫描；不传统计时保留
原有的"有可用索引即采用"行为。
"""

from __future__ import annotations

from minidb.compiler.bound import (
    BoundBinary,
    BoundColumn,
    BoundExpr,
    BoundLiteral,
    BoundUnary,
    BoundIsNull,
)
from minidb.compiler.plan import (
    CreateTablePlan,
    DeletePlan,
    FilterPlan,
    InsertPlan,
    Plan,
    ProjectPlan,
    SeqScanPlan,
    IndexScanPlan,
    UpdatePlan,
    CreateIndexPlan,
    DescribePlan,
    ExplainPlan,
    validate_plan,
)
from minidb.compiler.index_selection import choose_index_scan
from minidb.compiler.statistics import TableStats, decide_scan_path
from minidb.core.expressions import ExprOp, resolve_result_type
from minidb.core.schema import DataType


class Optimizer:
    """对已通过校验的计划做保持结果等价的结构简化。"""

    def __init__(self) -> None:
        pass

    def optimize(self, plan: Plan, indexes: tuple = (), stats: TableStats | None = None) -> Plan:
        """返回新计划；原计划及其嵌套节点保持不变。

        ``indexes`` 是调用方从当前 Catalog 读取的只读索引快照。优化器不
        访问目录；未提供索引时保留原有的顺序扫描优化行为。``stats`` 是
        调用方读取的只读统计快照；提供时按成本决定是否采用索引扫描，
        ``stats=None`` 时保持原有的"有可用索引即采用"行为。
        """
        if type(indexes) is not tuple:
            raise TypeError("indexes must be a tuple")
        if stats is not None and type(stats) is not TableStats:
            raise TypeError("stats must be a TableStats or None")
        validate_plan(plan)
        optimized = self._optimize_plan(plan, indexes, stats)
        # 优化可能删除 Filter，但不能产生 Executor 不接受的计划结构。
        validate_plan(optimized)
        return optimized

    def _optimize_plan(self, plan: Plan, indexes: tuple, stats: TableStats | None) -> Plan:
        # 新Plan接口适配：这些节点尚无优化规则，校验后原样交给后续阶段。
        # 不在此处代写UPDATE、索引或EXPLAIN的优化/执行实现。
        if isinstance(plan, (IndexScanPlan, UpdatePlan, CreateIndexPlan, DescribePlan)):
            return plan
        if isinstance(plan, ExplainPlan):
            return ExplainPlan(self._optimize_plan(plan.child, indexes, stats), plan.span)
        if isinstance(plan, CreateTablePlan):
            return CreateTablePlan(plan.table_name, plan.schema, plan.span)
        if isinstance(plan, InsertPlan):
            return InsertPlan(plan.table, plan.row, plan.span)
        if isinstance(plan, SeqScanPlan):
            return SeqScanPlan(plan.table, plan.span)
        if isinstance(plan, FilterPlan):
            child = self._optimize_plan(plan.child, indexes, stats)
            predicate = self._optimize_expr(plan.predicate)
            # 恒真的过滤条件不会筛掉任何行，可以安全移除。
            if _bool_literal(predicate, True):
                return child
            if isinstance(child, SeqScanPlan) and indexes:
                chosen = choose_index_scan(child.table, predicate, indexes, plan.span)
                if chosen is not None:
                    if stats is not None:
                        decided = decide_scan_path(chosen, stats)
                        if decided is not None:
                            return FilterPlan(decided, predicate, plan.span)
                        return FilterPlan(child, predicate, plan.span)
                    return FilterPlan(chosen, predicate, plan.span)
            return FilterPlan(child, predicate, plan.span)
        if isinstance(plan, ProjectPlan):
            child = self._optimize_plan(plan.child, indexes, stats)
            return ProjectPlan(child, plan.column_indexes, plan.output_columns, plan.span)
        if isinstance(plan, DeletePlan):
            child = self._optimize_plan(plan.child, indexes, stats)
            return DeletePlan(plan.table, child, plan.span)
        raise TypeError(f"unsupported plan type: {type(plan).__name__}")

    def _optimize_expr(self, expr: BoundExpr) -> BoundExpr:
        if isinstance(expr, (BoundColumn, BoundLiteral, BoundIsNull)):
            # BoundIsNull是新提供的节点；保持原式，不猜测其运行时结果。
            return expr

        if isinstance(expr, BoundUnary):
            operand = self._optimize_expr(expr.operand)
            if _bool_literal(operand):
                # NOT True/False 仍保留当前表达式的整体源码位置。
                return BoundLiteral(not operand.value, DataType.BOOL, expr.span)
            if operand is expr.operand:
                return expr
            return BoundUnary(expr.op, operand, expr.type_spec, expr.op_span, expr.span,
                              operand.nullable)

        if isinstance(expr, BoundBinary):
            left = self._optimize_expr(expr.left)
            right = self._optimize_expr(expr.right)

            simplified = _simplify_boolean(expr.op, left, right)
            if simplified is not None:
                return simplified

            if isinstance(left, BoundLiteral) and isinstance(right, BoundLiteral):
                folded = _fold_literals(expr, left, right)
                if folded is not None:
                    return folded

            if left is expr.left and right is expr.right:
                return expr
            # 重建节点必须随新操作数传播nullable，不能退回旧接口的默认False。
            return BoundBinary(expr.op, left, right, expr.type_spec, expr.op_span, expr.span,
                               left.nullable or right.nullable)

        raise TypeError(f"unsupported bound expression type: {type(expr).__name__}")


def _bool_literal(expr: BoundExpr, value: bool | None = None) -> bool:
    """判断表达式是否为 Optimizer 产生的 BOOL 常量。"""
    if not isinstance(expr, BoundLiteral) or expr.data_type is not DataType.BOOL:
        return False
    if type(expr.value) is not bool:
        return False
    return value is None or expr.value is value


def _simplify_boolean(op: ExprOp, left: BoundExpr, right: BoundExpr) -> BoundExpr | None:
    """实现 True AND x、False OR x 等不改变结果的布尔恒等式。"""
    if op is ExprOp.AND:
        if _bool_literal(left, False) or _bool_literal(right, False):
            return _make_bool(False, left, right)
        if _bool_literal(left, True):
            return right
        if _bool_literal(right, True):
            return left
    elif op is ExprOp.OR:
        if _bool_literal(left, True) or _bool_literal(right, True):
            return _make_bool(True, left, right)
        if _bool_literal(left, False):
            return right
        if _bool_literal(right, False):
            return left
    return None


def _fold_literals(node: BoundBinary, left: BoundLiteral, right: BoundLiteral) -> BoundLiteral | None:
    """只折叠已通过统一类型规则的常量运算。"""
    if left.value is None or right.value is None:
        # v2新增NULL后，Python的None比较/真值转换不能代表SQL的UNKNOWN。
        # 当前没有NULL常量折叠规则，保留原表达式，由正式求值模块处理。
        return None
    if resolve_result_type(node.op, (left.data_type, right.data_type)) is not DataType.BOOL:
        return None
    if node.op is ExprOp.EQ:
        value = left.value == right.value
    elif node.op is ExprOp.NE:
        value = left.value != right.value
    elif node.op is ExprOp.LT:
        value = left.value < right.value
    elif node.op is ExprOp.LE:
        value = left.value <= right.value
    elif node.op is ExprOp.GT:
        value = left.value > right.value
    elif node.op is ExprOp.GE:
        value = left.value >= right.value
    elif node.op is ExprOp.AND:
        value = left.value and right.value
    elif node.op is ExprOp.OR:
        value = left.value or right.value
    else:
        return None
    return BoundLiteral(bool(value), DataType.BOOL, node.span)


def _make_bool(value: bool, left: BoundExpr, right: BoundExpr) -> BoundLiteral:
    """为被消除的二元条件保留一个稳定的范围，便于 trace 和调试。"""
    # 该常量只在两个操作数均为 BOOL 且已通过 validate_plan 后创建。
    return BoundLiteral(value, DataType.BOOL, _span_for_expr(left, right))


def _span_for_expr(left: BoundExpr, right: BoundExpr):
    """取二元简化结果的整体范围；两个节点来自同一条原 SQL。"""
    from minidb.core.source import SourceSpan

    return SourceSpan(left.span.start, right.span.end, left.span.source_name)


__all__ = ["Optimizer"]
