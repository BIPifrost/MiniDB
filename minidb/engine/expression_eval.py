"""对已经绑定和校验过的表达式求值。

本模块不解析 SQL，也不按列名查询 Schema。编译阶段已经把列名换成列序号，
并把操作符换成正式 ``ExprOp``；执行阶段只需从当前 Row 取值并完成运算。

类型是否合法仍以 ``resolve_result_type`` 为唯一规则来源。本模块只实现运行
时的取值、比较和 AND/OR 短路，不复制一张独立的类型规则表。
"""

from __future__ import annotations

from minidb.compiler.bound import (
    BoundBinary,
    BoundColumn,
    BoundExpr,
    BoundLiteral,
    BoundUnary,
)
from minidb.core.errors import INVALID_ARGUMENT, INVALID_PLAN, DbError, ErrorStage
from minidb.core.expressions import ExprOp, resolve_result_type
from minidb.core.records import Row
from minidb.core.schema import DataType


def evaluate(expr: BoundExpr, row: Row) -> int | str | bool:
    """使用当前完整行计算一个绑定表达式。

    正常调用来自已经执行过 ``validate_plan`` 的 Executor。这里仍检查直接
    API 调用的基本形状，避免错误列号变成难懂的 IndexError，或让 Python
    把 bool 当作 int 悄悄参与比较。
    """
    if not isinstance(expr, (BoundColumn, BoundLiteral, BoundUnary, BoundBinary)):
        _invalid_plan(expr, "expr", "BoundExpr", type(expr).__name__)
    if type(row) is not tuple:
        raise DbError(
            ErrorStage.EXECUTION,
            INVALID_ARGUMENT,
            "表达式求值需要完整 Row 元组",
            getattr(expr, "span", None),
            {
                "operation": "expression_eval.evaluate",
                "field": "row",
                "expected": "tuple[int | str, ...]",
                "actual": type(row).__name__,
            },
        )
    return _evaluate(expr, row, set())


def _evaluate(
    expr: BoundExpr,
    row: Row,
    active: set[int],
) -> int | str | bool:
    """递归计算当前节点；active 仅用于防御手工构造的循环表达式。"""
    identity = id(expr)
    if identity in active:
        _invalid_plan(expr, "expr", "无环表达式", type(expr).__name__)

    if isinstance(expr, BoundColumn):
        if type(expr.index) is not int or not 0 <= expr.index < len(row):
            _invalid_plan(expr, "index", f"0..{len(row) - 1}", repr(expr.index))
        value = row[expr.index]
        _require_value_type(expr, value, expr.data_type)
        return value

    if isinstance(expr, BoundLiteral):
        _require_value_type(expr, expr.value, expr.data_type)
        return expr.value

    active.add(identity)
    try:
        if isinstance(expr, BoundUnary):
            operand_expr = _require_child(expr, expr.operand, "operand")
            _require_operation(
                expr,
                allowed=expr.op is ExprOp.NOT,
                operand_types=(operand_expr.data_type,),
            )
            operand = _evaluate(operand_expr, row, active)
            if type(operand) is not bool:
                _invalid_plan(expr, "operand", "BOOL 值", type(operand).__name__)
            return not operand

        if not isinstance(expr, BoundBinary):
            _invalid_plan(expr, "expr", "BoundExpr", type(expr).__name__)
        left_expr = _require_child(expr, expr.left, "left")
        right_expr = _require_child(expr, expr.right, "right")
        _require_operation(
            expr,
            allowed=isinstance(expr.op, ExprOp) and expr.op is not ExprOp.NOT,
            operand_types=(left_expr.data_type, right_expr.data_type),
        )

        # 编译阶段已经检查完整表达式；运行阶段才按 SQL 语义短路，从而不会
        # 求值本行上不需要的右分支。
        left = _evaluate(left_expr, row, active)
        if expr.op is ExprOp.AND:
            _require_bool(expr, left, "left")
            if not left:
                return False
            right = _evaluate(right_expr, row, active)
            _require_bool(expr, right, "right")
            return right
        if expr.op is ExprOp.OR:
            _require_bool(expr, left, "left")
            if left:
                return True
            right = _evaluate(right_expr, row, active)
            _require_bool(expr, right, "right")
            return right

        right = _evaluate(right_expr, row, active)
        operations = {
            ExprOp.EQ: lambda: left == right,
            ExprOp.NE: lambda: left != right,
            ExprOp.LT: lambda: left < right,
            ExprOp.LE: lambda: left <= right,
            ExprOp.GT: lambda: left > right,
            ExprOp.GE: lambda: left >= right,
        }
        return operations[expr.op]()
    finally:
        active.discard(identity)


def _require_operation(
    expr: BoundUnary | BoundBinary,
    *,
    allowed: bool,
    operand_types: tuple[DataType, ...],
) -> None:
    """复用公共类型规则，拒绝手工拼出的矛盾 Bound 节点。"""
    if not allowed:
        _invalid_plan(expr, "op", "与节点元数匹配的 ExprOp", repr(expr.op))
    for index, data_type in enumerate(operand_types):
        if not isinstance(data_type, DataType):
            _invalid_plan(
                expr,
                f"operand_types[{index}]",
                "DataType",
                repr(data_type),
            )
    result = resolve_result_type(expr.op, operand_types)
    if result is not DataType.BOOL or expr.data_type is not result:
        _invalid_plan(
            expr,
            "data_type",
            "与操作数和 ExprOp 一致的 BOOL",
            repr(expr.data_type),
        )


def _require_child(
    parent: BoundUnary | BoundBinary,
    child: object,
    field: str,
) -> BoundExpr:
    if not isinstance(child, (BoundColumn, BoundLiteral, BoundUnary, BoundBinary)):
        _invalid_plan(parent, field, "BoundExpr", type(child).__name__)
    return child


def _require_value_type(
    expr: BoundColumn | BoundLiteral,
    value: object,
    data_type: object,
) -> None:
    expected = {
        DataType.INT: int,
        DataType.VARCHAR: str,
        DataType.BOOL: bool,
    }.get(data_type)
    if expected is None or type(value) is not expected:
        expected_name = data_type.name if isinstance(data_type, DataType) else "DataType"
        _invalid_plan(expr, "value", expected_name, type(value).__name__)


def _require_bool(expr: BoundBinary, value: object, field: str) -> None:
    if type(value) is not bool:
        _invalid_plan(expr, field, "BOOL 值", type(value).__name__)


def _invalid_plan(
    expr: object,
    field: str,
    expected: object,
    actual: object,
) -> None:
    raise DbError(
        ErrorStage.PLAN,
        INVALID_PLAN,
        "绑定表达式与执行期约定不一致",
        getattr(expr, "span", None),
        {
            "operation": "expression_eval.evaluate",
            "field": field,
            "expected": expected,
            "actual": actual,
        },
    )


__all__ = ["evaluate"]
