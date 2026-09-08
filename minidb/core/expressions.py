"""张振：Semantic、Optimizer、Executor 共用的表达式操作及类型规则。"""

from enum import Enum

from minidb.core.schema import DataType


class ExprOp(Enum):
    """表达式操作符：EQ/NE 是等于/不等于，LT/LE/GT/GE 是大小比较，其余是逻辑运算。"""
    EQ = "EQ"
    NE = "NE"
    LT = "LT"
    LE = "LE"
    GT = "GT"
    GE = "GE"
    AND = "AND"
    OR = "OR"
    NOT = "NOT"


def resolve_result_type(
    op: ExprOp, operand_types: tuple[DataType, ...]
) -> DataType | None:
    """查询一种运算是否允许；合法返回 BOOL，类型或元数不匹配返回 None。

    原始字符串等不符合接口的参数属于编程错误，按约定报 PLAN 阶段的
    INVALID_ARGUMENT。此处不读取数据、不求值、不附加 SQL 源码位置。
    """
    if not isinstance(op, ExprOp):
        _invalid("op", "ExprOp", op)
    if not isinstance(operand_types, tuple):
        _invalid("operand_types", "tuple[DataType, ...]", operand_types)
    for index, data_type in enumerate(operand_types):
        if not isinstance(data_type, DataType):
            _invalid(f"operand_types[{index}]", "DataType", data_type)

    if op is ExprOp.NOT:
        return DataType.BOOL if operand_types == (DataType.BOOL,) else None

    if len(operand_types) != 2:
        return None
    left, right = operand_types

    if op in (ExprOp.AND, ExprOp.OR):
        valid = left is DataType.BOOL and right is DataType.BOOL
    elif op in (ExprOp.EQ, ExprOp.NE):
        valid = left is right and left in (DataType.INT, DataType.VARCHAR)
    else:  # LT / LE / GT / GE 只接受两个 INT。
        valid = left is DataType.INT and right is DataType.INT
    return DataType.BOOL if valid else None


def _invalid(field: str, expected: str, actual: object) -> None:
    # 依赖赵凯航维护的公共错误契约；类型组合查询本身无需该模块。
    """类型查询接口的参数错误固定报告为 PLAN/INVALID_ARGUMENT。"""
    from minidb.core.errors import INVALID_ARGUMENT, DbError, ErrorStage

    raise DbError(
        stage=ErrorStage.PLAN,
        code=INVALID_ARGUMENT,
        message=f"resolve_result_type 的 {field} 参数不合法",
        span=None,
        context={
            "operation": "resolve_result_type",
            "field": field,
            "expected": expected,
            "actual": repr(actual),
        },
    )
