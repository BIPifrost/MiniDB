"""源码位置与文本封装。

由赵凯航维护。定义 SourceText、SourcePos、SourceSpan 三个公共结构，
供 Token、AST、DbError 等所有需要源码位置的模块使用。

位置规则（见工作计划第 6.1、15.3 节）：
- 行、列从 1 开始；offset 从 0 开始，按 Unicode 字符计数，不按 UTF-8 字节。
- CRLF 在行列统计中算一次换行，在 offset 中仍占两个字符。
- Span 为起点包含、终点不包含；制表符按一个字符计数。
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class SourcePos:
    """源码中的一个字符位置。

    Attributes:
        line: 行号，从 1 开始。
        column: 列号，从 1 开始。
        offset: 字符偏移量，从 0 开始，按 Unicode 字符计数。
    """

    line: int
    column: int
    offset: int

    def __post_init__(self) -> None:
        if type(self.line) is not int:
            raise TypeError(f"SourcePos.line 必须是 int，实际为 {type(self.line).__name__}")
        if type(self.column) is not int:
            raise TypeError(f"SourcePos.column 必须是 int，实际为 {type(self.column).__name__}")
        if type(self.offset) is not int:
            raise TypeError(f"SourcePos.offset 必须是 int，实际为 {type(self.offset).__name__}")
        if self.line < 1:
            raise ValueError(f"SourcePos.line 必须 >= 1，实际为 {self.line}")
        if self.column < 1:
            raise ValueError(f"SourcePos.column 必须 >= 1，实际为 {self.column}")
        if self.offset < 0:
            raise ValueError(f"SourcePos.offset 必须 >= 0，实际为 {self.offset}")


@dataclass(frozen=True)
class SourceSpan:
    """源码中的一个范围，起点包含、终点不包含。

    Attributes:
        start: 起始位置（包含）。
        end: 结束位置（不包含）。
        source_name: 所属 SourceText 的名称。
    """

    start: SourcePos
    end: SourcePos
    source_name: str

    def __post_init__(self) -> None:
        if not isinstance(self.start, SourcePos):
            raise TypeError(f"SourceSpan.start 必须是 SourcePos，实际为 {type(self.start).__name__}")
        if not isinstance(self.end, SourcePos):
            raise TypeError(f"SourceSpan.end 必须是 SourcePos，实际为 {type(self.end).__name__}")
        if type(self.source_name) is not str:
            raise TypeError(f"SourceSpan.source_name 必须是 str，实际为 {type(self.source_name).__name__}")
        if self.start.offset > self.end.offset:
            raise ValueError(
                f"SourceSpan.start.offset ({self.start.offset}) "
                f"不能大于 end.offset ({self.end.offset})"
            )


@dataclass(frozen=True)
class SourceText:
    """一份待分析的源码文本。

    Attributes:
        name: 源码名称。文件输入用实际路径；交互模式用 "<stdin>"；测试用 "<test>"。
        text: 已按 UTF-8 解码并移除开头 BOM 的完整文本，保留原始 CRLF。
    """

    name: str
    text: str

    def __post_init__(self) -> None:
        if type(self.name) is not str:
            raise TypeError(f"SourceText.name 必须是 str，实际为 {type(self.name).__name__}")
        if type(self.text) is not str:
            raise TypeError(f"SourceText.text 必须是 str，实际为 {type(self.text).__name__}")

    def __len__(self) -> int:
        return len(self.text)
