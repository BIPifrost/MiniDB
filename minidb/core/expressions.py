"""统一表达式操作和类型规则；三值逻辑的实际求值由执行器负责。"""
from enum import Enum
from minidb.core.schema import DataType, TypeSpec
from minidb.core._v2_contract import fail


class ExprOp(Enum):
    EQ = "EQ"
    NE = "NE"
    LT = "LT"
    LE = "LE"
    GT = "GT"
    GE = "GE"
    AND = "AND"
    OR = "OR"
    NOT = "NOT"


def resolve_result_type(op, operand_types):
    """保留公开返回 DataType 的接口；输入接受完整TypeSpec和未定型NULL。"""
    if not isinstance(op, ExprOp):
        _argument_error("op", "ExprOp", op)
    if type(operand_types) is not tuple:
        _argument_error("operand_types", "tuple", operand_types)
    for index, value in enumerate(operand_types):
        if value is not None and not isinstance(value, (DataType, TypeSpec)):
            _argument_error(f"operand_types[{index}]", "TypeSpec/DataType/None", value)
    types = tuple(value.kind if isinstance(value, TypeSpec) else value for value in operand_types)
    if op is ExprOp.NOT:
        return DataType.BOOL if len(types) == 1 and types[0] in (None, DataType.BOOL) else None
    if len(types) != 2:
        return None
    if op in (ExprOp.AND, ExprOp.OR):
        valid = all(value in (None, DataType.BOOL) for value in types)
    else:
        left, right = types
        # NULL接受另一侧的上下文；两侧NULL比较仍返回可空BOOL。
        left, right = left or right, right or left
        if left is None:
            valid = True
        elif left in (DataType.INT, DataType.DECIMAL) and right in (DataType.INT, DataType.DECIMAL):
            valid = True
        else:
            valid = left is right and (left in (DataType.VARCHAR, DataType.DATE)
                                      or left is DataType.BOOL and op in (ExprOp.EQ, ExprOp.NE))
    return DataType.BOOL if valid else None


def _argument_error(field, expected, actual):
    fail("INVALID_ARGUMENT", "类型规则调用参数不合法", stage="PLAN",
         operation="resolve_result_type", field=field, expected=expected, actual=repr(actual))
