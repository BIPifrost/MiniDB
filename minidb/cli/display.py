"""面向用户的命令行结果显示。

正常运行时使用容易阅读的 MySQL 风格文本；trace 和 syntax-check 仍由
``main.py`` 使用 JSON 输出，因此调试和自动化接口不会被改变。
"""

from __future__ import annotations

from dataclasses import fields, is_dataclass
from enum import Enum
import unicodedata
from collections.abc import Mapping, Sequence

from minidb.core.result import QueryResult


class StreamFormatter:
    """逐行输出结果表；列宽只依据表头，因此不用缓存整批结果。

    计划第 7.4 节要求 CLI 每读取一行立即格式化输出，所以这里不能用
    ``format_result`` 那样先看完全部行再算列宽。代价是行内长值可能比表头
    宽，边框不会随之变宽；这是流式输出接受的取舍。
    """

    __slots__ = ("_columns", "_widths", "_count")

    def __init__(self, columns: Sequence) -> None:
        self._columns = tuple(columns)
        self._widths = [
            max(display_width(column.name), 1) for column in self._columns
        ]
        self._count = 0

    @property
    def count(self) -> int:
        return self._count

    def header(self) -> str:
        names = [column.name for column in self._columns]
        return "\n".join([
            _border(self._widths, "-"),
            _row_line(names, self._widths),
            _border(self._widths, "-"),
        ])

    def row(self, row: Sequence) -> str:
        self._count += 1
        values = [_format_value(value) for value in row]
        return _row_line(values, self._widths)

    def footer(self) -> str:
        if self._count == 0:
            return "Empty set"
        suffix = "row" if self._count == 1 else "rows"
        return "\n".join([
            _border(self._widths, "-"),
            f"{self._count} {suffix} in set",
        ])


def _border(widths: list[int], fill: str) -> str:
    return "+" + "+".join(fill * (width + 2) for width in widths) + "+"


def _row_line(values: Sequence[str], widths: list[int]) -> str:
    cells = [
        f" {_pad(str(value), widths[index])} "
        for index, value in enumerate(values)
    ]
    return "|" + "|".join(cells) + "|"


def format_result(result: QueryResult) -> str:
    """把一个查询结果格式化成一段可直接打印的文本。"""
    if result.columns:
        if not result.rows:
            return "Empty set"
        table = _format_table(
            [column.name for column in result.columns],
            [[_format_value(value) for value in row] for row in result.rows],
        )
        count = len(result.rows)
        suffix = "row" if count == 1 else "rows"
        return f"{table}\n{count} {suffix} in set"

    affected = result.affected_rows
    if affected is not None:
        suffix = "row" if affected == 1 else "rows"
        return f"Query OK, {affected} {suffix} affected"
    return result.message or "Query OK"


def _format_table(headers: list[str], rows: list[list[str]]) -> str:
    widths = [display_width(header) for header in headers]
    for row in rows:
        for index, value in enumerate(row):
            widths[index] = max(widths[index], display_width(value))

    output = [_border(widths, "-"), _row_line(headers, widths), _border(widths, "-")]
    output.extend(_row_line(row, widths) for row in rows)
    output.append(_border(widths, "-"))
    return "\n".join(output)


def _format_value(value: object) -> str:
    """把单个值渲染成表格单元文本（NULL/TRUE/FALSE 大写）。"""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    return str(value)


def display_width(value: str) -> int:
    """计算终端中一个字符串占用的大致宽度，中文按两个字符处理。"""
    width = 0
    for char in value:
        width += 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
    return width


def _pad(value: str, width: int) -> str:
    return value + " " * max(0, width - display_width(value))


def format_trace_readable(event: object) -> str:
    """把一条 JSON trace 事件转换为适合人阅读的分阶段文本。

    该函数只负责展示，不改变 ``--trace`` 使用的 JSON 契约，也不修改事件
    中的 AST、Bound 或 Plan 对象。
    """
    if not isinstance(event, Mapping):
        return _pretty(event)
    stage = str(event.get("stage", "TRACE"))
    index = event.get("statement_index")
    title = {
        "TOKEN": "Token 流",
        "AST": "AST 语法树",
        "SEMANTIC": "语义检查结果",
        "PLAN": "执行计划",
        "OPTIMIZED_PLAN": "优化后计划",
        "ERROR": "错误",
    }.get(stage, stage)
    prefix = f"语句 {index} · {title}" if index is not None else title
    data = event.get("data")

    if stage == "TOKEN" and isinstance(data, Sequence) and not isinstance(data, (str, bytes)):
        lines = [prefix]
        for token in data:
            kind = getattr(token, "kind", None)
            kind_name = getattr(kind, "name", str(kind))
            lexeme = getattr(token, "lexeme", "")
            span = getattr(token, "span", None)
            position = ""
            if span is not None and getattr(span, "start", None) is not None:
                position = f"  (第 {span.start.line} 行第 {span.start.column} 列)"
            shown = "<EOF>" if kind_name == "EOF" else repr(lexeme)
            lines.append(f"  {kind_name:<18} {shown}{position}")
        return "\n".join(lines)

    if stage == "ERROR":
        error = data
        code = getattr(error, "code", "ERROR")
        message = getattr(error, "message", str(error))
        lines = [f"{prefix}: [{code}] {message}"]
        span = getattr(error, "span", None)
        if span is not None:
            lines.append(f"  位置：{span.source_name} 第 {span.start.line} 行第 {span.start.column} 列")
        return "\n".join(lines)

    if stage == "OPTIMIZED_PLAN" and isinstance(data, Mapping):
        enabled = data.get("enabled")
        lines = [prefix, f"  优化：{'开启' if enabled else '关闭'}"]
        lines.extend(_pretty_lines(data.get("plan"), 2))
        return "\n".join(lines)

    return "\n".join([prefix, *_pretty_lines(data, 2)])


def _pretty(value: object) -> str:
    return "\n".join(_pretty_lines(value, 0))


def _pretty_lines(value: object, indent: int) -> list[str]:
    spaces = " " * indent
    if value is None or type(value) in (bool, int, float, str):
        return [spaces + _display_scalar(value)]
    if isinstance(value, Enum):
        return [spaces + value.name]
    if is_dataclass(value) and not isinstance(value, type):
        lines = [spaces + type(value).__name__]
        for field in fields(value):
            if field.name.startswith("_") or field.name in {"span", "op_span", "type_span"}:
                continue
            item = getattr(value, field.name)
            if _is_scalar(item):
                lines.append(f"{' ' * (indent + 2)}{field.name}: {_display_scalar(item)}")
            else:
                lines.append(f"{' ' * (indent + 2)}{field.name}:")
                lines.extend(_pretty_lines(item, indent + 4))
        return lines
    if isinstance(value, Mapping):
        lines: list[str] = []
        for key, item in value.items():
            if key in {"span", "op_span", "type_span"}:
                continue
            if _is_scalar(item):
                lines.append(f"{spaces}{key}: {_display_scalar(item)}")
            else:
                lines.append(f"{spaces}{key}:")
                lines.extend(_pretty_lines(item, indent + 2))
        return lines or [spaces + "{}"]
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        if not value:
            return [spaces + "[]"]
        if all(_is_scalar(item) for item in value):
            return [spaces + "[" + ", ".join(_display_scalar(item) for item in value) + "]"]
        lines = []
        for item in value:
            item_lines = _pretty_lines(item, indent + 2)
            lines.append(f"{spaces}- {item_lines[0].lstrip()}")
            lines.extend(item_lines[1:])
        return lines
    return [spaces + _display_scalar(value)]


def _is_scalar(value: object) -> bool:
    return value is None or type(value) in (bool, int, float, str) or isinstance(value, Enum)


def _display_scalar(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, Enum):
        return value.name
    if isinstance(value, str):
        return repr(value)
    return str(value)


__all__ = [
    "StreamFormatter",
    "display_width",
    "format_result",
    "format_trace_readable",
]
