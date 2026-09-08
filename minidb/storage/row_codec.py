"""按固定磁盘格式编码和解码一行数据。

本模块只负责 ``Row + Schema <-> bytes``，不决定记录放在哪一页，也不读写
文件。StorageEngine、DataPage 和 CatalogManager 因此可以共用同一种行字节格式。
"""

from __future__ import annotations

from minidb.core.errors import (
    INT_OUT_OF_RANGE,
    INVALID_ARGUMENT,
    ROW_CORRUPTED,
    ROW_ENCODING_ERROR,
    ROW_TOO_LARGE,
    ROW_TYPE_MISMATCH,
    ROW_VALUE_COUNT_MISMATCH,
    DbError,
    ErrorStage,
)
from minidb.core.records import Row
from minidb.core.schema import DataType, Schema


INT_BYTE_SIZE = 8
VARCHAR_LENGTH_SIZE = 4
MAX_ROW_SIZE = 4056  # 4096 字节页 - 32 字节页头 - 8 字节记录槽。
INT64_MIN = -(1 << 63)
INT64_MAX = (1 << 63) - 1
UINT32_MAX = (1 << 32) - 1


class RowCodec:
    """实现 MiniDB v1 的无类型标签行编码格式。"""

    def encoded_size(self, row: Row, schema: Schema) -> int:
        """返回完整编码所需字节数，并执行与 ``encode`` 相同的写入前校验。"""
        values = _validate_row(row, schema, "encoded_size")
        size = 0
        for index, (value, column) in enumerate(zip(values, schema.columns)):
            if column.data_type is DataType.INT:
                _validate_int(value, index, "encoded_size")
                size += INT_BYTE_SIZE
            else:
                size += VARCHAR_LENGTH_SIZE + len(_encode_string(value, index, "encoded_size"))
        _validate_size(size, "encoded_size")
        return size

    def encode(self, row: Row, schema: Schema) -> bytes:
        """把 Schema 顺序的 Row 编码为连续字节，不截断超长记录。"""
        values = _validate_row(row, schema, "encode")
        encoded = bytearray()
        for index, (value, column) in enumerate(zip(values, schema.columns)):
            if column.data_type is DataType.INT:
                _validate_int(value, index, "encode")
                encoded.extend(value.to_bytes(INT_BYTE_SIZE, byteorder="little", signed=True))
            else:
                text = _encode_string(value, index, "encode")
                encoded.extend(len(text).to_bytes(VARCHAR_LENGTH_SIZE, byteorder="little"))
                encoded.extend(text)
        _validate_size(len(encoded), "encode")
        return bytes(encoded)

    def decode(self, data: bytes, schema: Schema) -> Row:
        """将完整行字节严格解码；短数据、脏 UTF-8 和尾随数据均拒绝。"""
        _validate_schema(schema, "decode")
        if type(data) is not bytes:
            _invalid_argument("decode", "data", "bytes", type(data).__name__)
        # 编码格式规定单条记录最多占用一个数据页中可放置的空间。
        # 即使字段本身看起来能读完，超过该上限也说明页上的记录边界
        # 已经损坏，不能让 decode 单独放行与 encode 不一致的数据。
        if len(data) > MAX_ROW_SIZE:
            _corrupted(0, MAX_ROW_SIZE, "行编码超过单条记录允许的最大长度")

        offset = 0
        values: list[int | str] = []
        for index, column in enumerate(schema.columns):
            if column.data_type is DataType.INT:
                _require_remaining(data, offset, INT_BYTE_SIZE, index, "INT 字段不足 8 字节")
                values.append(int.from_bytes(data[offset:offset + INT_BYTE_SIZE], "little", signed=True))
                offset += INT_BYTE_SIZE
                continue

            _require_remaining(data, offset, VARCHAR_LENGTH_SIZE, index, "VARCHAR 长度头不足 4 字节")
            byte_length = int.from_bytes(data[offset:offset + VARCHAR_LENGTH_SIZE], "little")
            offset += VARCHAR_LENGTH_SIZE
            _require_remaining(data, offset, byte_length, index, "VARCHAR 内容长度超过剩余字节")
            raw_text = data[offset:offset + byte_length]
            try:
                values.append(raw_text.decode("utf-8", errors="strict"))
            except UnicodeDecodeError as error:
                _corrupted(index, offset + error.start, "VARCHAR 不是合法 UTF-8")
            offset += byte_length

        if offset != len(data):
            _corrupted(len(schema.columns), offset, "行编码含有未消费的尾随字节")
        return tuple(values)


def _validate_schema(schema: object, operation: str) -> Schema:
    """RowCodec 只接受核心模块提供的正式 Schema，避免猜测列类型。"""
    if not isinstance(schema, Schema):
        _invalid_argument(operation, "schema", "Schema", type(schema).__name__)
    return schema


def _validate_row(row: object, schema: object, operation: str) -> Row:
    """先检查行形状和类型，再开始计算字节数或生成字节。"""
    formal_schema = _validate_schema(schema, operation)
    if type(row) is not tuple:
        _invalid_argument(operation, "row", "tuple[int | str, ...]", type(row).__name__)
    if len(row) != len(formal_schema.columns):
        _error(
            ROW_VALUE_COUNT_MISMATCH,
            "行值数量与 Schema 列数不一致",
            operation,
            expected=len(formal_schema.columns),
            actual=len(row),
        )
    for index, (value, column) in enumerate(zip(row, formal_schema.columns)):
        expected_type = int if column.data_type is DataType.INT else str
        if type(value) is not expected_type:
            _error(
                ROW_TYPE_MISMATCH,
                "行值类型与 Schema 列类型不一致",
                operation,
                column_index=index,
                expected=column.data_type.name,
                actual=type(value).__name__,
            )
    return row


def _validate_int(value: int, index: int, operation: str) -> None:
    """直接调用 Codec 时也复核 INT64 边界，避免 struct/to_bytes 泄漏异常。"""
    if not INT64_MIN <= value <= INT64_MAX:
        # INT_OUT_OF_RANGE 是公共错误契约中固定为 SYNTAX 的错误码；
        # Codec 复核直接 API 调用时仍保留该固定 code/stage 组合。
        _error(
            INT_OUT_OF_RANGE,
            "INT 值超出 64 位有符号整数范围",
            operation,
            stage=ErrorStage.SYNTAX,
            column_index=index,
            value_repr=repr(value),
            min_value=INT64_MIN,
            max_value=INT64_MAX,
        )


def _encode_string(value: str, index: int, operation: str) -> bytes:
    """严格编码 UTF-8，并把编码异常转换成统一的结构化错误。"""
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as error:
        _error(
            ROW_ENCODING_ERROR,
            "VARCHAR 无法编码为 UTF-8",
            operation,
            column_index=index,
            reason=str(error),
        )
    if len(encoded) > UINT32_MAX:
        _error(
            ROW_ENCODING_ERROR,
            "VARCHAR 的 UTF-8 字节长度超过格式上限",
            operation,
            column_index=index,
            reason="UTF-8 字节长度超过 32 位无符号整数范围",
        )
    return encoded


def _validate_size(size: int, operation: str) -> None:
    if size > MAX_ROW_SIZE:
        _error(
            ROW_TOO_LARGE,
            "行编码超过单个数据页可容纳的最大长度",
            operation,
            encoded_size=size,
            max_size=MAX_ROW_SIZE,
        )


def _require_remaining(data: bytes, offset: int, count: int, index: int, reason: str) -> None:
    if count > len(data) - offset:
        _corrupted(index, offset, reason)


def _corrupted(index: int, offset: int, reason: str) -> None:
    _error(
        ROW_CORRUPTED,
        "行编码损坏或不完整",
        "decode",
        column_index=index,
        byte_offset=offset,
        reason=reason,
    )


def _invalid_argument(operation: str, field: str, expected: str, actual: str) -> None:
    _error(
        INVALID_ARGUMENT,
        f"{operation} 的 {field} 参数不合法",
        operation,
        field=field,
        expected=expected,
        actual=actual,
    )


def _error(code: str, message: str, operation: str, *, stage: ErrorStage = ErrorStage.STORAGE, **context: object) -> None:
    """唯一错误出口：保留公共错误码、阶段和 JSON 上下文字段。"""
    raise DbError(stage, code, message, None, {"operation": operation, **context})


__all__ = [
    "RowCodec",
    "INT_BYTE_SIZE",
    "VARCHAR_LENGTH_SIZE",
    "MAX_ROW_SIZE",
    "INT64_MIN",
    "INT64_MAX",
]
