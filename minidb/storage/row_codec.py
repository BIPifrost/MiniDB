"""按固定磁盘格式编码和解码一行数据（v2）。

本模块只负责 ``Row + Schema <-> bytes``，不决定记录放在哪一页，也不读写
文件。StorageEngine、DataPage 和 CatalogManager 因此可以共用同一种行字节格式。

v2 行格式（工作计划第 8.2 节）::

    offset0: uint8  row_version = 2
    offset1: uint8  flags = 0
    offset2: uint16 column_count
    offset4: ceil(column_count / 8) 字节 null_bitmap
    随后:   按列顺序连接所有非 NULL 字段负载

第 i 列使用 ``null_bitmap[i // 8]`` 的 bit ``i % 8``，1 表示 NULL；未使用的
高位必须为 0。行前缀与空值位图都计入单条记录的 4056 字节上限，所以单
VARCHAR 行的固定开销是 4 + 1 + 4 = 9 字节。

各类型负载（工作计划第 6.2 节）：INT 为 int64 有符号小端，VARCHAR 为
uint32 字节长度加 UTF-8，BOOL 为 1 字节 01/00，DATE 为 int32 距
1970-01-01 的天数，DECIMAL 为 int64 缩放整数。

取值规则只有一处实现（``core.value_rules.normalize_value``）。Codec 只复核
已归一化的值，并把失败转换成行编码错误码，避免出现第二套类型规则。
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from minidb.core.errors import (
    INVALID_ARGUMENT,
    NOT_NULL_VIOLATION,
    NUMERIC_OUT_OF_RANGE,
    NUMERIC_SCALE_MISMATCH,
    ROW_CORRUPTED,
    ROW_ENCODING_ERROR,
    ROW_TOO_LARGE,
    ROW_TYPE_MISMATCH,
    ROW_VALUE_COUNT_MISMATCH,
    TYPE_MISMATCH,
    VALUE_TOO_LONG,
    DbError,
    ErrorStage,
)
from minidb.core.records import Row
from minidb.core.schema import DataType, Schema
from minidb.core.value_rules import normalize_value


ROW_VERSION_V2 = 2
ROW_FLAGS_V2 = 0
ROW_PREFIX_SIZE = 4  # row_version(1) + flags(1) + column_count(2)
COLUMN_COUNT_SIZE = 2
INT_BYTE_SIZE = 8
VARCHAR_LENGTH_SIZE = 4
BOOL_BYTE_SIZE = 1
DATE_BYTE_SIZE = 4
DECIMAL_BYTE_SIZE = 8
MAX_ROW_SIZE = 4056  # 4096 字节页 - 32 字节页头 - 8 字节记录槽。
INT64_MIN = -(1 << 63)
INT64_MAX = (1 << 63) - 1
INT32_MIN = -(1 << 31)
INT32_MAX = (1 << 31) - 1
DATE_EPOCH = date(1970, 1, 1)

# 非 VARCHAR 的字段负载长度固定；VARCHAR 由字节长度头加内容决定。
_FIXED_PAYLOAD_SIZE = {
    DataType.INT: INT_BYTE_SIZE,
    DataType.BOOL: BOOL_BYTE_SIZE,
    DataType.DATE: DATE_BYTE_SIZE,
    DataType.DECIMAL: DECIMAL_BYTE_SIZE,
}

# 复核阶段保留原错误码的值错误；其余错误按 Codec 自己的错误码重新报告。
_VALUE_ERROR_CODES = frozenset({
    NOT_NULL_VIOLATION,
    VALUE_TOO_LONG,
    NUMERIC_OUT_OF_RANGE,
    NUMERIC_SCALE_MISMATCH,
    ROW_ENCODING_ERROR,
})


class RowCodec:
    """实现 MiniDB v2 的行编码格式，三个方法的长度/类型/空值规则完全一致。"""

    def encoded_size(self, row: Row, schema: Schema) -> int:
        """返回完整编码所需字节数，并执行与 ``encode`` 相同的写入前校验。"""
        values, columns = _normalized_row(row, schema, "encoded_size")
        size = ROW_PREFIX_SIZE + _bitmap_size(len(columns))
        for index, (value, column) in enumerate(zip(values, columns)):
            size += _payload_size(value, column, index, "encoded_size")
        _validate_size(size, "encoded_size")
        return size

    def encode(self, row: Row, schema: Schema) -> bytes:
        """把 Schema 顺序的 Row 编码为连续字节，不截断超长记录。"""
        values, columns = _normalized_row(row, schema, "encode")
        bitmap_size = _bitmap_size(len(columns))
        size = ROW_PREFIX_SIZE + bitmap_size
        for index, (value, column) in enumerate(zip(values, columns)):
            size += _payload_size(value, column, index, "encode")
        # 先验证总长度再拼装负载，避免为最终必然拒绝的行分配大块字节。
        _validate_size(size, "encode")

        encoded = bytearray(size)
        encoded[0] = ROW_VERSION_V2
        encoded[1] = ROW_FLAGS_V2
        encoded[2:ROW_PREFIX_SIZE] = len(columns).to_bytes(COLUMN_COUNT_SIZE, "little")
        offset = ROW_PREFIX_SIZE + bitmap_size
        for index, (value, column) in enumerate(zip(values, columns)):
            if value is None:
                encoded[ROW_PREFIX_SIZE + (index >> 3)] |= 1 << (index & 7)
                continue
            payload = _payload(value, column, index)
            encoded[offset:offset + len(payload)] = payload
            offset += len(payload)
        return bytes(encoded)

    def decode(self, data: bytes, schema: Schema) -> Row:
        """严格解码完整行字节；前缀、位图、负载和尾随字节都会复核。"""
        columns = _validate_schema(schema, "decode").columns
        if type(data) is not bytes:
            _invalid_argument("decode", "data", "bytes", type(data).__name__)
        if len(data) > MAX_ROW_SIZE:
            _corrupted(0, 0, "行编码超过单条记录允许的最大长度")
        if len(data) < ROW_PREFIX_SIZE:
            _corrupted(0, 0, "行前缀不足4字节")
        if data[0] != ROW_VERSION_V2:
            _corrupted(0, 0, f"row_version 必须为{ROW_VERSION_V2}")
        if data[1] != ROW_FLAGS_V2:
            _corrupted(1, 1, "flags 必须为0")
        column_count = int.from_bytes(data[2:ROW_PREFIX_SIZE], "little")
        if column_count != len(columns):
            _corrupted(2, 2, "column_count 与 Schema 列数不一致")

        bitmap_size = _bitmap_size(column_count)
        bitmap_end = ROW_PREFIX_SIZE + bitmap_size
        if len(data) < bitmap_end:
            _corrupted(ROW_PREFIX_SIZE, ROW_PREFIX_SIZE, "空值位图不完整")
        bitmap = data[ROW_PREFIX_SIZE:bitmap_end]
        for bit in range(column_count, bitmap_size * 8):
            if bitmap[bit >> 3] >> (bit & 7) & 1:
                _corrupted(bit, ROW_PREFIX_SIZE + (bit >> 3),
                           "空值位图未使用的高位必须为0")

        offset = bitmap_end
        values: list[object] = []
        for index, column in enumerate(columns):
            if bitmap[index >> 3] >> (index & 7) & 1:
                if not column.nullable:
                    _corrupted(index, ROW_PREFIX_SIZE + (index >> 3), "非空列被标记为 NULL")
                values.append(None)
                continue
            field_offset = offset
            value, offset = _decode_column(data, offset, index, column)
            _require_canonical(value, column, index, field_offset)
            values.append(value)

        if offset != len(data):
            _corrupted(len(columns), offset, "行编码含有未消费的尾随字节")
        return tuple(values)


def _bitmap_size(column_count: int) -> int:
    """返回空值位图占用的字节数：每 8 列 1 字节，向上取整。

    例：1~8 列 → 1 字节，9~16 列 → 2 字节。
    """
    return (column_count + 7) // 8


def _validate_schema(schema: object, operation: str) -> Schema:
    """RowCodec 只接受核心模块提供的正式 Schema，避免猜测列类型。"""
    if not isinstance(schema, Schema):
        _invalid_argument(operation, "schema", "Schema", type(schema).__name__)
    return schema


def _normalized_row(row: object, schema: object, operation: str) -> tuple[tuple, tuple]:
    """检查行形状并复核每个值，返回规范化后的值与正式列定义。"""
    formal_schema = _validate_schema(schema, operation)
    columns = formal_schema.columns
    if type(row) is not tuple:
        _invalid_argument(
            operation,
            "row",
            "tuple[int | str | bool | date | Decimal | None, ...]",
            type(row).__name__,
        )
    if len(row) != len(columns):
        _error(
            ROW_VALUE_COUNT_MISMATCH,
            "行值数量与 Schema 列数不一致",
            operation,
            expected=len(columns),
            actual=len(row),
        )
    values = tuple(
        _recheck(value, column, index, operation)
        for index, (value, column) in enumerate(zip(row, columns))
    )
    return values, columns


def _recheck(value: object, column, index: int, operation: str) -> object:
    """调用唯一的值规则复核，并把结果转换成 RowCodec 的错误码。"""
    try:
        return normalize_value(value, column.type_spec, nullable=column.nullable)
    except DbError as error:
        context = dict(error.context)
        context["operation"] = operation
        context["column_index"] = index
        context["column"] = column.name
        if error.code == TYPE_MISMATCH:
            raise DbError(
                ErrorStage.STORAGE,
                ROW_TYPE_MISMATCH,
                error.message,
                error.span,
                {**context, "expected": column.type_spec.kind.name,
                 "actual": type(value).__name__},
            )
        if error.code in _VALUE_ERROR_CODES:
            raise DbError(ErrorStage.STORAGE, error.code, error.message, error.span, context)
        raise


def _payload_size(value: object, column, index: int, operation: str) -> int:
    """返回单个字段负载占用的字节数，供 encoded_size 与 encode 预估整行长度。

    NULL 记 0 字节；INT/BOOL/DATE/DECIMAL 为定长；VARCHAR 为 4 字节长度头
    加 UTF-8 内容。此处只测长度，不产生任何字节。
    """
    if value is None:
        return 0
    fixed = _FIXED_PAYLOAD_SIZE.get(column.type_spec.kind)
    if fixed is not None:
        return fixed
    return VARCHAR_LENGTH_SIZE + len(_utf8(value, index, operation))


def _payload(value: object, column, index: int) -> bytes:
    """按列类型生成负载；值已经过 ``_recheck``，类型与范围都已确定。"""
    kind = column.type_spec.kind
    if kind is DataType.INT:
        return value.to_bytes(INT_BYTE_SIZE, "little", signed=True)
    if kind is DataType.BOOL:
        return b"\x01" if value else b"\x00"
    if kind is DataType.DATE:
        days = (value - DATE_EPOCH).days
        return days.to_bytes(DATE_BYTE_SIZE, "little", signed=True)
    if kind is DataType.DECIMAL:
        scaled = _decimal_scaled(value, column.type_spec.scale, index)
        return scaled.to_bytes(DECIMAL_BYTE_SIZE, "little", signed=True)
    text = _utf8(value, index, "encode")
    return len(text).to_bytes(VARCHAR_LENGTH_SIZE, "little") + text


def _utf8(value: str, index: int, operation: str) -> bytes:
    """严格编码 UTF-8，并把编码异常转换成统一的结构化错误。"""
    try:
        return value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as error:
        _error(
            ROW_ENCODING_ERROR,
            "VARCHAR 无法编码为 UTF-8",
            operation,
            column_index=index,
            reason=str(error),
        )


def _decimal_scaled(value: Decimal, scale: int, index: int) -> int:
    """把规范化后的 Decimal 转成 int64 缩放整数。"""
    sign, digits, exponent = value.as_tuple()
    digits = list(digits)
    while digits and digits[0] == 0:
        digits.pop(0)
    if not digits:
        return 0
    while digits[-1] == 0:
        digits.pop()
        exponent += 1
    shift = exponent + scale
    magnitude = int("".join(str(digit) for digit in digits)) * (10 ** shift)
    scaled = -magnitude if sign else magnitude
    if not INT64_MIN <= scaled <= INT64_MAX:
        _error(
            NUMERIC_OUT_OF_RANGE,
            "DECIMAL 的缩放整数超出 int64 范围",
            "encode",
            column_index=index,
            value=str(value),
            scale=scale,
        )
    return scaled


def _decode_column(data: bytes, offset: int, index: int, column) -> tuple[object, int]:
    """读取单个非 NULL 字段负载，返回值与新的偏移。"""
    kind = column.type_spec.kind
    if kind is DataType.INT:
        _require_remaining(data, offset, INT_BYTE_SIZE, index, "INT 字段不足 8 字节")
        value = int.from_bytes(data[offset:offset + INT_BYTE_SIZE], "little", signed=True)
        return value, offset + INT_BYTE_SIZE
    if kind is DataType.BOOL:
        _require_remaining(data, offset, BOOL_BYTE_SIZE, index, "BOOL 字段不足 1 字节")
        raw = data[offset]
        if raw not in (0, 1):
            _corrupted(index, offset, "BOOL 负载必须是 00 或 01")
        return raw == 1, offset + BOOL_BYTE_SIZE
    if kind is DataType.DATE:
        _require_remaining(data, offset, DATE_BYTE_SIZE, index, "DATE 字段不足 4 字节")
        days = int.from_bytes(data[offset:offset + DATE_BYTE_SIZE], "little", signed=True)
        try:
            value = DATE_EPOCH + timedelta(days=days)
        except OverflowError:
            _corrupted(index, offset, "DATE 天数超出可表示日期范围")
        return value, offset + DATE_BYTE_SIZE
    if kind is DataType.DECIMAL:
        _require_remaining(data, offset, DECIMAL_BYTE_SIZE, index, "DECIMAL 字段不足 8 字节")
        scaled = int.from_bytes(data[offset:offset + DECIMAL_BYTE_SIZE], "little", signed=True)
        return _decimal_from_scaled(scaled, column.type_spec.scale), offset + DECIMAL_BYTE_SIZE

    _require_remaining(data, offset, VARCHAR_LENGTH_SIZE, index, "VARCHAR 长度头不足 4 字节")
    byte_length = int.from_bytes(data[offset:offset + VARCHAR_LENGTH_SIZE], "little")
    offset += VARCHAR_LENGTH_SIZE
    _require_remaining(data, offset, byte_length, index, "VARCHAR 内容长度超过剩余字节")
    raw_text = data[offset:offset + byte_length]
    try:
        value = raw_text.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        _corrupted(index, offset + error.start, "VARCHAR 不是合法 UTF-8")
    return value, offset + byte_length


def _decimal_from_scaled(scaled: int, scale: int) -> Decimal:
    """不用进程 Decimal context 还原缩放整数，保证等价 scale 只有一个编码。"""
    if scaled == 0:
        return Decimal((0, (0,), -scale))
    digits = tuple(int(digit) for digit in str(abs(scaled)))
    return Decimal((1 if scaled < 0 else 0, digits, -scale))


def _require_canonical(value: object, column, index: int, offset: int) -> None:
    """拒绝页上不符合 Schema 的行内编码，避免越界值被当成正常数据读出。"""
    try:
        normalize_value(value, column.type_spec, nullable=column.nullable)
    except DbError as error:
        _corrupted(index, offset, f"字段值不符合 Schema：{error.code}")


def _validate_size(size: int, operation: str) -> None:
    """检查整行编码是否超过单条记录上限（MAX_ROW_SIZE），超出即报 ROW_TOO_LARGE。

    在真正分配/拼装字节之前调用，避免为最终必然被拒绝的行分配大块内存。
    """
    if size > MAX_ROW_SIZE:
        _error(
            ROW_TOO_LARGE,
            "行编码超过单个数据页可容纳的最大长度",
            operation,
            encoded_size=size,
            max_size=MAX_ROW_SIZE,
        )


def _require_remaining(data: bytes, offset: int, count: int, index: int, reason: str) -> None:
    """确认 data 从 offset 起至少还有 count 字节可读，不足即按行损坏报错。

    解码时用来防止切片越界：宁可报 ROW_CORRUPTED，也不返回半截数据。
    """
    if count > len(data) - offset:
        _corrupted(index, offset, reason)


def _corrupted(index: int, offset: int, reason: str) -> None:
    """统一报告“行编码损坏”：附上列号与字节偏移，方便定位坏在哪一段。

    解码路径上所有完整性/一致性检查失败都经由此函数抛出 ROW_CORRUPTED。
    """
    _error(
        ROW_CORRUPTED,
        "行编码损坏或不完整",
        "decode",
        column_index=index,
        byte_offset=offset,
        reason=reason,
    )


def _invalid_argument(operation: str, field: str, expected: str, actual: str) -> None:
    """报告调用参数不合法（类型或形状不对），抛出 INVALID_ARGUMENT。

    属于“调用方用错了”的编程错误，与“页上数据坏了”的 ROW_CORRUPTED 区分开。
    """
    _error(
        INVALID_ARGUMENT,
        f"{operation} 的 {field} 参数不合法",
        operation,
        field=field,
        expected=expected,
        actual=actual,
    )


def _error(code: str, message: str, operation: str, *,
           stage: ErrorStage = ErrorStage.STORAGE, **context: object) -> None:
    """唯一错误出口：保留公共错误码、阶段和 JSON 上下文字段。"""
    raise DbError(stage, code, message, None, {"operation": operation, **context})


__all__ = [
    "RowCodec",
    "ROW_VERSION_V2",
    "ROW_FLAGS_V2",
    "ROW_PREFIX_SIZE",
    "INT_BYTE_SIZE",
    "VARCHAR_LENGTH_SIZE",
    "BOOL_BYTE_SIZE",
    "DATE_BYTE_SIZE",
    "DECIMAL_BYTE_SIZE",
    "MAX_ROW_SIZE",
    "INT64_MIN",
    "INT64_MAX",
    "INT32_MIN",
    "INT32_MAX",
]
