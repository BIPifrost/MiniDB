"""张振编译模块内部的字段检查，不实现其他成员的公共类型。"""

from __future__ import annotations

from minidb.core.schema import DataType, Schema, TableDef, _IDENTIFIER


class Check:
    """张振模块的内部校验工具：统一检查字段，失败时交给公共错误出口。"""
    def __init__(self, operation: str, *, plan: bool = False) -> None:
        """记录正在检查的接口；plan=True 时按计划错误报告，其余按语义参数错误报告。"""
        self.operation = operation
        self.plan = plan

    def require(self, valid: bool, field: str, expected: object, actual: object) -> None:
        """断言一个契约条件；valid 为假立即抛错，后续代码才可放心访问该字段。"""
        if not valid:
            _contract_error(self.operation, field, expected, actual, plan=self.plan)

    def span(self, value, field: str = "span", parent=None) -> None:
        """检查源码位置的类型、行列偏移及父子包含关系，避免错误定位指向其他输入。"""
        # 依赖赵凯航的 SourcePos(line, column, offset) 和
        # SourceSpan(start, end, source_name)，字段与计划 15.3 节一致。
        from minidb.core.source import SourcePos, SourceSpan

        self.require(isinstance(value, SourceSpan), field, "SourceSpan", type(value).__name__)
        self.require(isinstance(value.source_name, str), field, "str source_name", value.source_name)
        for name, pos in (("start", value.start), ("end", value.end)):
            self.require(isinstance(pos, SourcePos), f"{field}.{name}", "SourcePos", type(pos).__name__)
            for key, minimum in (("line", 1), ("column", 1), ("offset", 0)):
                number = getattr(pos, key)
                self.require(type(number) is int and number >= minimum, f"{field}.{name}.{key}", f"int >= {minimum}", number)
        start = (value.start.line, value.start.column, value.start.offset)
        end = (value.end.line, value.end.column, value.end.offset)
        self.require(_positions_in_order(start, end), field, "行列顺序与字符偏移一致", repr(value))
        if parent is not None:
            self.require(isinstance(parent, SourceSpan), field, "SourceSpan 父范围", type(parent).__name__)
            self.require(
                value.source_name == parent.source_name
                and _positions_in_order((parent.start.line, parent.start.column, parent.start.offset), start)
                and _positions_in_order(end, (parent.end.line, parent.end.column, parent.end.offset)),
                field, "来自同一输入且位于父节点范围内", repr(value),
            )

    def schema(self, value) -> None:
        """确认参数是张振定义的正式 Schema，而不是临时字典或列表。"""
        self.require(isinstance(value, Schema), "schema", "Schema", type(value).__name__)

    def table(self, value) -> None:
        """确认参数是完整用户表定义；执行用户 SQL 时不允许使用系统目录表。"""
        self.require(isinstance(value, TableDef), "table", "TableDef", type(value).__name__)
        self.require(value.ref.table_id > 0, "table", "用户表", value.ref.name)

    def name(self, value) -> None:
        """检查绑定结果和计划中的表名：必须合法、已经小写且不占系统前缀。"""
        valid = isinstance(value, str) and _IDENTIFIER.fullmatch(value) is not None
        self.require(valid, "table_name", "合法标识符", value)
        self.require(value == value.lower() and not value.startswith("_sys_"), "table_name", "已归一化的用户表名", value)

    def value(self, value, data_type, field: str = "value", *, allow_bool: bool = False) -> None:
        """检查值的 Python 类型与 DataType 一致，并确认整数没有超出 64 位范围。"""
        allowed = (DataType.INT, DataType.VARCHAR, DataType.BOOL) if allow_bool else (DataType.INT, DataType.VARCHAR)
        self.require(isinstance(data_type, DataType) and data_type in allowed, "data_type", "允许的数据类型", repr(data_type))
        # Python 中 isinstance(True, int) 为真，所以这里用 type(value) 精确比较。
        expected = {DataType.INT: int, DataType.VARCHAR: str, DataType.BOOL: bool}[data_type]
        self.require(type(value) is expected, field, expected.__name__, type(value).__name__)
        if data_type is DataType.INT:
            self.require(-(1 << 63) <= value <= (1 << 63) - 1, field, "有符号 64 位整数", value)

    def row(self, row, schema: Schema) -> None:
        """先比较行长度和列数，再按 Schema 顺序检查每个值的类型。"""
        self.require(isinstance(row, tuple), "row", "tuple", type(row).__name__)
        self.require(len(row) == len(schema.columns), "row", len(schema.columns), len(row))
        for index, (value, column) in enumerate(zip(row, schema.columns)):
            self.value(value, column.data_type, f"row[{index}]")

    def projection(self, table: TableDef, indexes, columns) -> None:
        """检查投影索引有效，且每个索引与对应输出列名称、类型完全匹配。"""
        self.require(isinstance(indexes, tuple) and bool(indexes), "projection", "非空 tuple[int, ...]", repr(indexes))
        self.require(isinstance(columns, tuple), "output_columns", "tuple[ResultColumn, ...]", type(columns).__name__)
        self.require(len(indexes) == len(columns), "output_columns", len(indexes), len(columns))
        from minidb.core.result import ResultColumn

        for index, output in zip(indexes, columns):
            self.require(type(index) is int and 0 <= index < len(table.schema.columns), "projection", "有效列序号", index)
            self.require(isinstance(output, ResultColumn), "output_columns", "ResultColumn", type(output).__name__)
            column = table.schema.columns[index]
            self.require(output.name == column.name and output.data_type is column.data_type, "output_columns", (column.name, column.data_type.name), repr(output))


def _positions_in_order(start: tuple[int, int, int], end: tuple[int, int, int]) -> bool:
    """比较已经通过整数检查的 (行, 列, 字符偏移)，不依赖源码位置类。"""
    line_delta = end[0] - start[0]
    offset_delta = end[2] - start[2]
    # 行、列均从 1 开始：即使前面的行全部为空，也至少需要这些字符。
    if any(offset < line + column - 2 for line, column, offset in (start, end)):
        return False
    # 第一行前面没有换行和其他行，偏移必须正好等于列号减一。
    if any(line == 1 and offset != column - 1 for line, column, offset in (start, end)):
        return False
    if line_delta < 0 or offset_delta < 0:
        return False
    if line_delta == 0:
        # 中文、表情和制表符都按一个字符计数，不能混用 UTF-8 字节长度。
        return end[1] - start[1] == offset_delta
    # 跨行至少经过若干换行及末行已有的字符；CRLF 可以多占一个 offset。
    # 这里只检查必要条件，精确的原文对应关系仍由 SourceText/Lexer 提供。
    return offset_delta >= line_delta + end[1] - 1


def _contract_error(operation, field, expected, actual, *, plan=False):
    """统一抛出结构契约错误；plan 模式使用 PLAN/INVALID_PLAN。"""
    from minidb.core.errors import INVALID_ARGUMENT, INVALID_PLAN, DbError, ErrorStage

    raise DbError(
        ErrorStage.PLAN if plan else ErrorStage.SEMANTIC,
        INVALID_PLAN if plan else INVALID_ARGUMENT,
        f"{operation} 的 {field} 不符合约定",
        None,
        {"operation": operation, "field": field, "expected": repr(expected), "actual": repr(actual)},
    )
