"""错误类型与结构化错误。

由赵凯航维护，涉及错误码的模块负责人复核。定义 ErrorStage 枚举、
全部错误码常量、DbError 不可变数据类。

错误码名称与工作计划第 15.2 节一致；跨模块新增错误码必须同步更新测试。
公共错误文件不得依赖具体业务模块（工作计划第 6.1 节）。
"""

from dataclasses import dataclass, field
from enum import Enum
import math
from typing import Any

from minidb.core.source import SourceSpan


class ErrorStage(Enum):
    """错误发生阶段，见工作计划第 6.1 节。"""

    LEXICAL = "LEXICAL"      # 词法分析 / 输入读取
    SYNTAX = "SYNTAX"        # 语法分析
    SEMANTIC = "SEMANTIC"    # 语义分析
    PLAN = "PLAN"            # 计划结构 / 表达式类型规则
    EXECUTION = "EXECUTION"  # 会话边界未预期异常
    STORAGE = "STORAGE"      # Codec / 页 / 文件 / 目录持久化


# ---------------------------------------------------------------------------
# 错误码常量（工作计划第 15.12 节，共 47 个）
# 使用模块级字符串常量，DbError.code 接受 str，传常量即可。
# ---------------------------------------------------------------------------

# ---- 输入读取（LEXICAL）----
INPUT_INVALID_UTF8 = "INPUT_INVALID_UTF8"
INPUT_READ_FAILED = "INPUT_READ_FAILED"

# ---- 词法（LEXICAL）----
INVALID_CHARACTER = "INVALID_CHARACTER"
UNTERMINATED_STRING = "UNTERMINATED_STRING"
UNTERMINATED_COMMENT = "UNTERMINATED_COMMENT"
INVALID_NUMBER = "INVALID_NUMBER"
IDENTIFIER_TOO_LONG = "IDENTIFIER_TOO_LONG"

# ---- 语法（SYNTAX）----
UNEXPECTED_TOKEN = "UNEXPECTED_TOKEN"
UNEXPECTED_EOF = "UNEXPECTED_EOF"
UNSUPPORTED_FEATURE = "UNSUPPORTED_FEATURE"
INT_OUT_OF_RANGE = "INT_OUT_OF_RANGE"

# ---- 语义（SEMANTIC）----
TABLE_EXISTS = "TABLE_EXISTS"
TABLE_NOT_FOUND = "TABLE_NOT_FOUND"
COLUMN_NOT_FOUND = "COLUMN_NOT_FOUND"
DUPLICATE_COLUMN = "DUPLICATE_COLUMN"
DUPLICATE_INSERT_COLUMN = "DUPLICATE_INSERT_COLUMN"
INSERT_COLUMN_SET_MISMATCH = "INSERT_COLUMN_SET_MISMATCH"
VALUE_COUNT_MISMATCH = "VALUE_COUNT_MISMATCH"
RESERVED_NAME = "RESERVED_NAME"
TYPE_MISMATCH = "TYPE_MISMATCH"
UNSUPPORTED_COMPARISON = "UNSUPPORTED_COMPARISON"
CONDITION_NOT_BOOL = "CONDITION_NOT_BOOL"

# ---- 计划（PLAN）----
INVALID_PLAN = "INVALID_PLAN"

# ---- 行编码（STORAGE）----
ROW_TYPE_MISMATCH = "ROW_TYPE_MISMATCH"
ROW_VALUE_COUNT_MISMATCH = "ROW_VALUE_COUNT_MISMATCH"
ROW_TOO_LARGE = "ROW_TOO_LARGE"
ROW_CORRUPTED = "ROW_CORRUPTED"
ROW_ENCODING_ERROR = "ROW_ENCODING_ERROR"

# ---- 页与文件（STORAGE）----
PAGE_ID_INVALID = "PAGE_ID_INVALID"
PAGE_NOT_ALLOCATED = "PAGE_NOT_ALLOCATED"
PAGE_ALREADY_FREE = "PAGE_ALREADY_FREE"
RESERVED_PAGE = "RESERVED_PAGE"
SLOT_ID_INVALID = "SLOT_ID_INVALID"
STALE_ROW = "STALE_ROW"
PAGE_CORRUPTED = "PAGE_CORRUPTED"
DB_FORMAT_MISMATCH = "DB_FORMAT_MISMATCH"
DB_FILE_TRUNCATED = "DB_FILE_TRUNCATED"
CATALOG_CORRUPTED = "CATALOG_CORRUPTED"
ID_EXHAUSTED = "ID_EXHAUSTED"

# ---- I/O（STORAGE）----
IO_OPEN_FAILED = "IO_OPEN_FAILED"
IO_READ_FAILED = "IO_READ_FAILED"
IO_WRITE_FAILED = "IO_WRITE_FAILED"
IO_SYNC_FAILED = "IO_SYNC_FAILED"
IO_CLOSE_FAILED = "IO_CLOSE_FAILED"

# ---- 会话与资源（EXECUTION / STORAGE）----
ACTIVE_SCAN = "ACTIVE_SCAN"
CLOSED = "CLOSED"
INVALID_ARGUMENT = "INVALID_ARGUMENT"
INTERNAL_ERROR = "INTERNAL_ERROR"


# 全部错误码集合，用于 DbError 构造时校验
ALL_ERROR_CODES: frozenset[str] = frozenset({
    # 输入读取
    INPUT_INVALID_UTF8, INPUT_READ_FAILED,
    # 词法
    INVALID_CHARACTER, UNTERMINATED_STRING, UNTERMINATED_COMMENT,
    INVALID_NUMBER, IDENTIFIER_TOO_LONG,
    # 语法
    UNEXPECTED_TOKEN, UNEXPECTED_EOF, UNSUPPORTED_FEATURE, INT_OUT_OF_RANGE,
    # 语义
    TABLE_EXISTS, TABLE_NOT_FOUND, COLUMN_NOT_FOUND, DUPLICATE_COLUMN,
    DUPLICATE_INSERT_COLUMN, INSERT_COLUMN_SET_MISMATCH, VALUE_COUNT_MISMATCH,
    RESERVED_NAME, TYPE_MISMATCH, UNSUPPORTED_COMPARISON, CONDITION_NOT_BOOL,
    # 计划
    INVALID_PLAN,
    # 行编码
    ROW_TYPE_MISMATCH, ROW_VALUE_COUNT_MISMATCH, ROW_TOO_LARGE,
    ROW_CORRUPTED, ROW_ENCODING_ERROR,
    # 页与文件
    PAGE_ID_INVALID, PAGE_NOT_ALLOCATED, PAGE_ALREADY_FREE, RESERVED_PAGE,
    SLOT_ID_INVALID, STALE_ROW, PAGE_CORRUPTED, DB_FORMAT_MISMATCH, DB_FILE_TRUNCATED,
    CATALOG_CORRUPTED, ID_EXHAUSTED,
    # I/O
    IO_OPEN_FAILED, IO_READ_FAILED, IO_WRITE_FAILED, IO_SYNC_FAILED,
    IO_CLOSE_FAILED,
    # 会话与资源
    ACTIVE_SCAN, CLOSED, INVALID_ARGUMENT, INTERNAL_ERROR,
})


# 固定错误码对应的发生阶段。INVALID_ARGUMENT 不放入此表：
# 它表示调用参数不合法，阶段由具体公共接口所属模块决定。
ERROR_STAGE_BY_CODE: dict[str, ErrorStage] = {
    # 输入读取与词法
    INPUT_INVALID_UTF8: ErrorStage.LEXICAL,
    INPUT_READ_FAILED: ErrorStage.LEXICAL,
    INVALID_CHARACTER: ErrorStage.LEXICAL,
    UNTERMINATED_STRING: ErrorStage.LEXICAL,
    UNTERMINATED_COMMENT: ErrorStage.LEXICAL,
    INVALID_NUMBER: ErrorStage.LEXICAL,
    IDENTIFIER_TOO_LONG: ErrorStage.LEXICAL,
    # 语法
    UNEXPECTED_TOKEN: ErrorStage.SYNTAX,
    UNEXPECTED_EOF: ErrorStage.SYNTAX,
    UNSUPPORTED_FEATURE: ErrorStage.SYNTAX,
    INT_OUT_OF_RANGE: ErrorStage.SYNTAX,
    # 语义
    TABLE_EXISTS: ErrorStage.SEMANTIC,
    TABLE_NOT_FOUND: ErrorStage.SEMANTIC,
    COLUMN_NOT_FOUND: ErrorStage.SEMANTIC,
    DUPLICATE_COLUMN: ErrorStage.SEMANTIC,
    DUPLICATE_INSERT_COLUMN: ErrorStage.SEMANTIC,
    INSERT_COLUMN_SET_MISMATCH: ErrorStage.SEMANTIC,
    VALUE_COUNT_MISMATCH: ErrorStage.SEMANTIC,
    RESERVED_NAME: ErrorStage.SEMANTIC,
    TYPE_MISMATCH: ErrorStage.SEMANTIC,
    UNSUPPORTED_COMPARISON: ErrorStage.SEMANTIC,
    CONDITION_NOT_BOOL: ErrorStage.SEMANTIC,
    # 计划
    INVALID_PLAN: ErrorStage.PLAN,
    # 行、页、文件、目录与资源
    ROW_TYPE_MISMATCH: ErrorStage.STORAGE,
    ROW_VALUE_COUNT_MISMATCH: ErrorStage.STORAGE,
    ROW_TOO_LARGE: ErrorStage.STORAGE,
    ROW_CORRUPTED: ErrorStage.STORAGE,
    ROW_ENCODING_ERROR: ErrorStage.STORAGE,
    PAGE_ID_INVALID: ErrorStage.STORAGE,
    PAGE_NOT_ALLOCATED: ErrorStage.STORAGE,
    PAGE_ALREADY_FREE: ErrorStage.STORAGE,
    RESERVED_PAGE: ErrorStage.STORAGE,
    SLOT_ID_INVALID: ErrorStage.STORAGE,
    STALE_ROW: ErrorStage.STORAGE,
    PAGE_CORRUPTED: ErrorStage.STORAGE,
    DB_FORMAT_MISMATCH: ErrorStage.STORAGE,
    DB_FILE_TRUNCATED: ErrorStage.STORAGE,
    CATALOG_CORRUPTED: ErrorStage.STORAGE,
    ID_EXHAUSTED: ErrorStage.STORAGE,
    IO_OPEN_FAILED: ErrorStage.STORAGE,
    IO_READ_FAILED: ErrorStage.STORAGE,
    IO_WRITE_FAILED: ErrorStage.STORAGE,
    IO_SYNC_FAILED: ErrorStage.STORAGE,
    IO_CLOSE_FAILED: ErrorStage.STORAGE,
    ACTIVE_SCAN: ErrorStage.STORAGE,
    CLOSED: ErrorStage.STORAGE,
    # 未预期的执行期异常
    INTERNAL_ERROR: ErrorStage.EXECUTION,
}


# 张振接口对接：计划 15.12 要求手工 AST 和目录登记再次检查这些错误。
# 保留上表的默认阶段，仅允许规范明确需要的复核阶段；其余组合仍拒绝。
_RECHECK_STAGES_BY_CODE = {
    IDENTIFIER_TOO_LONG: ErrorStage.SEMANTIC,
    UNSUPPORTED_FEATURE: ErrorStage.SEMANTIC,
    TABLE_EXISTS: ErrorStage.STORAGE,
}


class _FrozenDict(dict):
    """JSON-compatible dict whose mutating operations are disabled."""

    def _immutable(self, *args: Any, **kwargs: Any) -> None:
        raise TypeError("DbError.context 是只读的")

    __setitem__ = __delitem__ = clear = pop = popitem = setdefault = update = _immutable
    __ior__ = _immutable


class _FrozenList(list):
    """JSON-compatible list whose mutating operations are disabled."""

    def _immutable(self, *args: Any, **kwargs: Any) -> None:
        raise TypeError("DbError.context 是只读的")

    __setitem__ = __delitem__ = append = clear = extend = insert = pop = remove = reverse = sort = _immutable
    __iadd__ = __imul__ = _immutable


def _freeze_json(value: Any, path: str) -> Any:
    """Validate and recursively freeze a JSON-compatible value."""
    if value is None:
        raise ValueError(f"DbError.context['{path}'] 不允许为 None；未知字段应直接省略")
    if type(value) in (str, int, bool):
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError(f"DbError.context['{path}'] 必须是有限 JSON 数字")
        return value
    if isinstance(value, list):
        return _FrozenList(_freeze_json(item, f"{path}[{index}]") for index, item in enumerate(value))
    if isinstance(value, dict):
        frozen = _FrozenDict()
        for key, item in value.items():
            if type(key) is not str:
                raise TypeError(
                    f"DbError.context 的键必须是 str，实际为 {type(key).__name__}"
                )
            dict.__setitem__(frozen, key, _freeze_json(item, f"{path}.{key}"))
        return frozen
    raise TypeError(
        f"DbError.context['{path}'] 必须是 JSON 可表示值，实际为 {type(value).__name__}"
    )


@dataclass
class DbError(Exception):
    """结构化错误，见工作计划第 6.1 节、第 15.12 节。

    继承 Exception，可以直接 raise / except。错误字段构造后不可修改，
    但保留 Exception 写入 traceback、cause 等运行时属性的能力。

    Attributes:
        stage: 错误发生阶段（LEXICAL/SYNTAX/SEMANTIC/PLAN/EXECUTION/STORAGE）。
        code: 稳定错误码，必须使用本文件定义的常量；不得依赖 message 文本进行
              程序分支或自动测试（工作计划第 15.12 节）。
        message: 清晰中文错误描述，供人阅读。
        span: 源码位置范围；存储错误如果没有 SQL 字符位置，允许为 None
              （工作计划第 2.3 节第 3 条）。
        context: 结构化上下文，如 expected、actual、table_name、page_id；
                 键必须是 str，值只能为 JSON 可表示的基本值、列表或字典，
                 不允许 None 占位——未知字段应直接省略（工作计划第 15.15 节）。
    """

    stage: ErrorStage
    code: str
    message: str
    span: SourceSpan | None = None
    context: dict[str, Any] = field(default_factory=dict)
    _sealed: bool = field(default=False, init=False, repr=False, compare=False)

    def __setattr__(self, name: str, value: Any) -> None:
        if getattr(self, "_sealed", False) and name in {
            "stage", "code", "message", "span", "context"
        }:
            raise TypeError("DbError 构造后的字段不可修改")
        object.__setattr__(self, name, value)

    def __post_init__(self) -> None:
        Exception.__init__(self, self.message)
        if not isinstance(self.stage, ErrorStage):
            raise TypeError(
                f"DbError.stage 必须是 ErrorStage，实际为 {type(self.stage).__name__}"
            )
        if type(self.code) is not str:
            raise TypeError(
                f"DbError.code 必须是 str，实际为 {type(self.code).__name__}"
            )
        if self.code not in ALL_ERROR_CODES:
            raise ValueError(
                f"DbError.code '{self.code}' 不是工作计划第 15.12 节定义的错误码"
            )
        expected_stage = ERROR_STAGE_BY_CODE.get(self.code)
        recheck_stage = _RECHECK_STAGES_BY_CODE.get(self.code)
        if (expected_stage is not None
                and self.stage is not expected_stage and self.stage is not recheck_stage):
            allowed = expected_stage.name
            if recheck_stage is not None:
                allowed += f" 或 {recheck_stage.name}"
            raise ValueError(
                f"错误码 {self.code} 必须使用 {allowed} 阶段，"
                f"实际为 {self.stage.name}"
            )
        if type(self.message) is not str:
            raise TypeError(
                f"DbError.message 必须是 str，实际为 {type(self.message).__name__}"
            )
        if self.span is not None and not isinstance(self.span, SourceSpan):
            raise TypeError(
                f"DbError.span 必须是 SourceSpan 或 None，实际为 {type(self.span).__name__}"
            )
        if type(self.context) is not dict:
            raise TypeError(
                f"DbError.context 必须是 dict，实际为 {type(self.context).__name__}"
            )
        frozen_context = _freeze_json(self.context, "context")
        object.__setattr__(self, "context", frozen_context)
        object.__setattr__(self, "_sealed", True)

    def _update_context(self, **updates: Any) -> None:
        """供跨层包装代码补充上下文，外部仍不能直接修改 context。"""
        if not updates:
            return
        merged = dict(self.context)
        merged.update(updates)
        object.__setattr__(self, "context", _freeze_json(merged, "context"))

    def __str__(self) -> str:
        """供 CLI 展示的可读错误信息（工作计划第 2.3 节：错误报告行、列、原因）。"""
        parts = [f"[{self.code}] {self.message}"]
        if self.span is not None:
            parts.append(
                f"  位置: {self.span.source_name} "
                f"第 {self.span.start.line} 行第 {self.span.start.column} 列"
            )
        for key, value in self.context.items():
            parts.append(f"  {key}: {value}")
        return "\n".join(parts)
