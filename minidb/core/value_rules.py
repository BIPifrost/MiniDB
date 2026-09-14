"""v2 唯一的值归一化与赋值/比较规则，不执行表达式、不实现 RowCodec。"""
from datetime import date
from decimal import Decimal

from minidb.core._v2_contract import fail
from minidb.core.schema import DataType, TypeSpec


def normalize_value(value, target: TypeSpec, *, nullable: bool):
    """精确检查并返回标准值，不依赖或修改进程的 Decimal context。"""
    if not isinstance(target, TypeSpec) or type(nullable) is not bool:
        fail("INVALID_ARGUMENT", "normalize_value 需要 TypeSpec 与 bool")
    if value is None:
        if not nullable:
            fail("NOT_NULL_VIOLATION", "非空列不能保存 NULL", stage="STORAGE")
        return None
    kind = target.kind
    if kind is DataType.DECIMAL and type(value) is int:
        value = Decimal(value)
    expected = {DataType.INT: int, DataType.VARCHAR: str, DataType.BOOL: bool,
                DataType.DATE: date, DataType.DECIMAL: Decimal}[kind]
    if type(value) is not expected:
        fail("TYPE_MISMATCH", "值与目标类型不匹配", expected=kind.name, actual=type(value).__name__)
    if kind is DataType.INT and not -(1 << 63) <= value < (1 << 63):
        fail("NUMERIC_OUT_OF_RANGE", "整数超出INT64", value=str(value), p=19, s=0)
    if kind is DataType.VARCHAR:
        if len(value) > target.length:
            fail("VALUE_TOO_LONG", "VARCHAR超过字符上限", actual=len(value), limit=target.length)
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            fail("ROW_ENCODING_ERROR", "字符串无法严格编码为UTF-8", stage="STORAGE")
    if kind is DataType.DECIMAL:
        if not value.is_finite():
            fail("NUMERIC_OUT_OF_RANGE", "DECIMAL必须是有限精确数值", value=str(value),
                 p=target.precision, s=target.scale)
        sign, digits, exponent = value.as_tuple()
        digits = list(digits)
        while digits and digits[0] == 0:
            digits.pop(0)
        if not digits:
            return Decimal((0, (0,), -target.scale))
        # 先删可无损舍弃的尾零，再判断小数位，避免 quantize 随全局context舍入。
        while digits[-1] == 0:
            digits.pop()
            exponent += 1
        shift = exponent + target.scale
        if shift < 0:
            fail("NUMERIC_SCALE_MISMATCH", "DECIMAL不能静默舍入", s=target.scale, value=str(value))
        if len(digits) + shift > target.precision:
            fail("NUMERIC_OUT_OF_RANGE", "DECIMAL超出精度", p=target.precision,
                 s=target.scale, value=str(value))
        return Decimal((sign, tuple(digits + [0] * shift), -target.scale))
    return value


def assignment_allowed(source: TypeSpec | None, target: TypeSpec) -> bool:
    if not isinstance(target, TypeSpec) or (source is not None and not isinstance(source, TypeSpec)):
        fail("INVALID_ARGUMENT", "赋值类型参数必须是 TypeSpec")
    return source is None or source.kind is target.kind or (
        source.kind is DataType.INT and target.kind is DataType.DECIMAL)


def comparison_allowed(op, left: TypeSpec | None, right: TypeSpec | None) -> bool:
    from minidb.core.expressions import ExprOp, resolve_result_type
    if not isinstance(op, ExprOp) or any(v is not None and not isinstance(v, TypeSpec) for v in (left, right)):
        fail("INVALID_ARGUMENT", "比较接口参数类型不合法")
    if op not in (ExprOp.EQ, ExprOp.NE, ExprOp.LT, ExprOp.LE, ExprOp.GT, ExprOp.GE):
        return False
    # 比较矩阵只在expressions保存一份，避免语义检查与其他调用方逐渐分歧。
    return resolve_result_type(op, (left, right)) is DataType.BOOL


def default_text(value, target):
    """只序列化目录中的 DEFAULT 文本，不取代队友的通用trace/RowCodec。"""
    if value is None:
        return ""
    if target.kind is DataType.BOOL:
        return "TRUE" if value else "FALSE"
    if target.kind is DataType.DATE:
        return value.isoformat()
    if target.kind is DataType.DECIMAL:
        # 值已按列的scale归一化；f保留小数位，避免极小数和零被str写成指数。
        return format(value, "f")
    return str(value)


def parse_default(kind, text, target, *, nullable):
    """严格还原规范目录文本；拒绝非规范编码而非自动修复损坏目录。"""
    if kind == "NONE":
        from minidb.core.schema import NO_DEFAULT
        if text != "":
            raise ValueError("NONE default must be empty")
        return NO_DEFAULT
    from minidb.core.schema import DefaultSpec
    if kind == "NULL":
        if text != "":
            raise ValueError("NULL default must be empty")
        value = None
    elif kind != target.kind.name:
        raise ValueError("default kind does not match column")
    elif kind == "VARCHAR":
        value = text
    elif kind == "BOOL":
        if text not in ("TRUE", "FALSE"):
            raise ValueError("invalid boolean default")
        value = text == "TRUE"
    elif kind == "DATE":
        value = date.fromisoformat(text)
    elif kind == "DECIMAL":
        value = Decimal(text)
    else:
        value = int(text)
    normalized = normalize_value(value, target, nullable=nullable)
    if default_text(normalized, target) != text:
        raise ValueError("noncanonical default")
    return DefaultSpec(True, normalized)
