"""面向用户的命令行结果显示。

正常运行时使用容易阅读的 MySQL 风格文本；trace 和 syntax-check 仍由
``main.py`` 使用 JSON 输出，因此调试和自动化接口不会被改变。
"""

from __future__ import annotations

import unicodedata

from minidb.core.result import QueryResult


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

    def line(fill: str) -> str:
        return "+" + "+".join(fill * (width + 2) for width in widths) + "+"

    def row_line(values: list[str]) -> str:
        cells = [f" {_pad(value, widths[index])} " for index, value in enumerate(values)]
        return "|" + "|".join(cells) + "|"

    output = [line("-"), row_line(headers), line("-")]
    output.extend(row_line(row) for row in rows)
    output.append(line("-"))
    return "\n".join(output)


def _format_value(value: object) -> str:
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


__all__ = ["display_width", "format_result"]
