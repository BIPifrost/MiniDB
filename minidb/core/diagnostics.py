"""语法检查结果与稳定的 JSON trace 输出。

由赵凯航维护。Parser 使用 SyntaxCheckResult 保存仅语法检查的结果；
CLI 使用 format_trace 把 Token、AST、计划和 DbError 输出为 JSON Lines。
本模块只转换对象，不解析 SQL、不打开数据库，也不修改输入对象。
"""

from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass
from enum import Enum
import json
import math
from typing import Any

from minidb.core.errors import DbError


@dataclass(frozen=True, slots=True)
class SyntaxCheckResult:
    """仅语法检查的汇总结果，见工作计划第 15.14 节。

    errors 按遇到的顺序保存。词法错误会使 Token 流终止，Parser 此时将
    stopped_on_lexical_error 设为 True，且不继续猜测后续 SQL 的边界。
    """

    valid_statement_count: int
    errors: tuple[DbError, ...]
    stopped_on_lexical_error: bool

    def __post_init__(self) -> None:
        if type(self.valid_statement_count) is not int:
            raise TypeError(
                "SyntaxCheckResult.valid_statement_count 必须是 int"
            )
        if self.valid_statement_count < 0:
            raise ValueError(
                "SyntaxCheckResult.valid_statement_count 必须 >= 0"
            )
        if type(self.errors) is not tuple:
            raise TypeError("SyntaxCheckResult.errors 必须是 tuple[DbError, ...]")
        if any(not isinstance(error, DbError) for error in self.errors):
            raise TypeError("SyntaxCheckResult.errors 只能包含 DbError")
        if type(self.stopped_on_lexical_error) is not bool:
            raise TypeError(
                "SyntaxCheckResult.stopped_on_lexical_error 必须是 bool"
            )


def format_trace(value: Any) -> str:
    """将支持的公共对象转为稳定、严格的单行 JSON。

    数据类会保留正式字段，并增加 node_type；枚举使用成员名称；tuple
    输出为 JSON 数组。键按字典序排序，中文不转义，返回值不含末尾换行。
    """
    data = _to_trace_data(value, path="value", active=set())
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _to_trace_data(value: Any, *, path: str, active: set[int]) -> Any:
    """递归转换公共 trace 值，并拒绝循环引用和非 JSON 类型。"""
    if value is None or type(value) in (bool, int, str):
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError(f"{path} 不能是 NaN 或无穷大")
        return value
    if isinstance(value, Enum):
        return value.name
    if is_dataclass(value) and not isinstance(value, type):
        return _dataclass_to_trace_data(value, path=path, active=active)
    if isinstance(value, dict):
        return _mapping_to_trace_data(value, path=path, active=active)
    if isinstance(value, (list, tuple)):
        return _sequence_to_trace_data(value, path=path, active=active)
    raise TypeError(f"{path} 不是可 trace 的公共类型：{type(value).__name__}")


def _dataclass_to_trace_data(value: Any, *, path: str, active: set[int]) -> dict[str, Any]:
    """转换数据类；内部字段不属于公共 trace 契约。"""
    value_id = id(value)
    _enter(value_id, path, active)
    try:
        result: dict[str, Any] = {"node_type": type(value).__name__}
        for item in fields(value):
            if item.name.startswith("_"):
                continue
            if item.name == "node_type":
                raise ValueError(f"{path} 的数据类字段不能命名为 node_type")
            result[item.name] = _to_trace_data(
                getattr(value, item.name), path=f"{path}.{item.name}", active=active
            )
        return result
    finally:
        active.remove(value_id)


def _mapping_to_trace_data(value: dict[Any, Any], *, path: str, active: set[int]) -> dict[str, Any]:
    """转换 JSON 对象；trace 的对象键必须是字符串。"""
    value_id = id(value)
    _enter(value_id, path, active)
    try:
        result: dict[str, Any] = {}
        for key, item in value.items():
            if type(key) is not str:
                raise TypeError(f"{path} 的字典键必须是 str")
            result[key] = _to_trace_data(item, path=f"{path}.{key}", active=active)
        return result
    finally:
        active.remove(value_id)


def _sequence_to_trace_data(value: list[Any] | tuple[Any, ...], *, path: str, active: set[int]) -> list[Any]:
    """转换 list 或 tuple；二者在 JSON 中都表示数组。"""
    value_id = id(value)
    _enter(value_id, path, active)
    try:
        return [
            _to_trace_data(item, path=f"{path}[{index}]", active=active)
            for index, item in enumerate(value)
        ]
    finally:
        active.remove(value_id)


def _enter(value_id: int, path: str, active: set[int]) -> None:
    if value_id in active:
        raise ValueError(f"{path} 包含循环引用，不能输出 JSON trace")
    active.add(value_id)
