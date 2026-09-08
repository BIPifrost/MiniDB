"""Token 类型与 Token 数据结构。

由赵凯航维护，张振复核。定义 TokenKind 枚举、Token 不可变数据类、
关键字映射表，以及教学材料用的粗类别映射函数。

TokenKind 名称与工作计划第 15.3 节完全一致，不得新增、删除或改名。
粗类别仅用于 trace 输出和 README 展示，内部 Token 始终使用精确 TokenKind，
不增加另一套可变类别字段（工作计划第 15.13 节）。
"""

from dataclasses import dataclass
from enum import Enum

from minidb.core.source import SourceSpan


class TokenKind(Enum):
    """SQL 词法单元类型。名称固定，见工作计划第 15.3 节。"""

    # ---- 关键字：本期支持 ----
    KW_CREATE = "KW_CREATE"
    KW_TABLE = "KW_TABLE"
    KW_INSERT = "KW_INSERT"
    KW_INTO = "KW_INTO"
    KW_VALUES = "KW_VALUES"
    KW_SELECT = "KW_SELECT"
    KW_FROM = "KW_FROM"
    KW_WHERE = "KW_WHERE"
    KW_DELETE = "KW_DELETE"
    KW_AND = "KW_AND"
    KW_OR = "KW_OR"
    KW_NOT = "KW_NOT"
    KW_INT = "KW_INT"
    KW_VARCHAR = "KW_VARCHAR"

    # ---- 关键字：扩展（Lexer 识别，Parser 报 UNSUPPORTED_FEATURE）----
    KW_UPDATE = "KW_UPDATE"
    KW_JOIN = "KW_JOIN"
    KW_ORDER = "KW_ORDER"
    KW_BY = "KW_BY"
    KW_GROUP = "KW_GROUP"
    KW_DISTINCT = "KW_DISTINCT"
    KW_NULL = "KW_NULL"
    KW_TRUE = "KW_TRUE"
    KW_FALSE = "KW_FALSE"
    KW_EXPLAIN = "KW_EXPLAIN"

    # ---- 标识符与字面量 ----
    IDENT = "IDENT"
    INTEGER_LITERAL = "INTEGER_LITERAL"
    DECIMAL_LITERAL = "DECIMAL_LITERAL"
    STRING_LITERAL = "STRING_LITERAL"

    # ---- 运算符 ----
    EQ = "EQ"        # = 或 ==
    NE = "NE"        # != 或 <>
    LT = "LT"        # <
    LE = "LE"        # <=
    GT = "GT"        # >
    GE = "GE"        # >=
    PLUS = "PLUS"    # +
    MINUS = "MINUS"  # -
    STAR = "STAR"    # *
    SLASH = "SLASH"  # /

    # ---- 分隔符 ----
    LPAREN = "LPAREN"      # (
    RPAREN = "RPAREN"      # )
    COMMA = "COMMA"        # ,
    SEMICOLON = "SEMICOLON"  # ;

    # ---- 输入结束 ----
    EOF = "EOF"


@dataclass(frozen=True)
class Token:
    """一个词法单元。

    Attributes:
        kind: Token 类型。
        lexeme: 原始写法（原文切片），标识符保留原始大小写；EOF 为空字符串。
        value: 解码后的值。标识符=原始名字，整数/小数=原始数字串，
               字符串=处理双单引号后的内容；关键字、运算符、分隔符、EOF 为 None。
        span: 在源码中的位置范围。
    """

    kind: TokenKind
    lexeme: str
    value: str | None
    span: SourceSpan

    def __post_init__(self) -> None:
        if not isinstance(self.kind, TokenKind):
            raise TypeError(f"Token.kind 必须是 TokenKind，实际为 {type(self.kind).__name__}")
        if type(self.lexeme) is not str:
            raise TypeError(f"Token.lexeme 必须是 str，实际为 {type(self.lexeme).__name__}")
        if self.value is not None and type(self.value) is not str:
            raise TypeError(f"Token.value 必须是 str 或 None，实际为 {type(self.value).__name__}")
        if not isinstance(self.span, SourceSpan):
            raise TypeError(f"Token.span 必须是 SourceSpan，实际为 {type(self.span).__name__}")


# ---------------------------------------------------------------------------
# 关键字映射：小写关键字字符串 -> TokenKind
#
# 包含全部关键字（含扩展关键字）。Lexer 用它判断一个标识符是否为关键字。
# 扩展关键字（UPDATE/JOIN/ORDER 等）被识别为对应 KW_* Token，
# 后续由 Parser 报 UNSUPPORTED_FEATURE，不能当作普通标识符接受
# （工作计划第 1.3 节第 7 条、第 15.3 节）。
# ---------------------------------------------------------------------------
KEYWORDS: dict[str, TokenKind] = {
    "create": TokenKind.KW_CREATE,
    "table": TokenKind.KW_TABLE,
    "insert": TokenKind.KW_INSERT,
    "into": TokenKind.KW_INTO,
    "values": TokenKind.KW_VALUES,
    "select": TokenKind.KW_SELECT,
    "from": TokenKind.KW_FROM,
    "where": TokenKind.KW_WHERE,
    "delete": TokenKind.KW_DELETE,
    "and": TokenKind.KW_AND,
    "or": TokenKind.KW_OR,
    "not": TokenKind.KW_NOT,
    "int": TokenKind.KW_INT,
    "varchar": TokenKind.KW_VARCHAR,
    "update": TokenKind.KW_UPDATE,
    "join": TokenKind.KW_JOIN,
    "order": TokenKind.KW_ORDER,
    "by": TokenKind.KW_BY,
    "group": TokenKind.KW_GROUP,
    "distinct": TokenKind.KW_DISTINCT,
    "null": TokenKind.KW_NULL,
    "true": TokenKind.KW_TRUE,
    "false": TokenKind.KW_FALSE,
    "explain": TokenKind.KW_EXPLAIN,
}


# ---------------------------------------------------------------------------
# 粗类别集合（供 token_category 使用，工作计划第 15.13 节）
# ---------------------------------------------------------------------------
_OPERATOR_KINDS = frozenset({
    TokenKind.EQ, TokenKind.NE, TokenKind.LT, TokenKind.LE,
    TokenKind.GT, TokenKind.GE, TokenKind.PLUS, TokenKind.MINUS,
    TokenKind.STAR, TokenKind.SLASH,
})

_DELIMITER_KINDS = frozenset({
    TokenKind.LPAREN, TokenKind.RPAREN, TokenKind.COMMA, TokenKind.SEMICOLON,
})

_LITERAL_KINDS = frozenset({
    TokenKind.INTEGER_LITERAL, TokenKind.DECIMAL_LITERAL, TokenKind.STRING_LITERAL,
})


def token_category(kind: TokenKind) -> str:
    """返回教学材料中的粗类别，仅用于 trace 输出和 README 展示。

    映射规则（工作计划第 15.13 节）：
    - KW_* -> KEYWORD（布尔连接关键字 AND/OR/NOT 仍归 KEYWORD）
    - IDENT -> IDENTIFIER
    - INTEGER_LITERAL / DECIMAL_LITERAL / STRING_LITERAL -> CONST
    - 比较与算术运算符（EQ/NE/LT/LE/GT/GE/PLUS/MINUS/STAR/SLASH）-> OPERATOR
    - 括号/逗号/分号 -> DELIMITER
    - EOF -> EOF（独立标记）

    内部 Token 继续使用精确 TokenKind，不增加另一套可变类别字段。
    """
    if not isinstance(kind, TokenKind):
        raise TypeError(f"token_category 参数必须是 TokenKind，实际为 {type(kind).__name__}")
    if kind.name.startswith("KW_"):
        return "KEYWORD"
    if kind is TokenKind.IDENT:
        return "IDENTIFIER"
    if kind in _LITERAL_KINDS:
        return "CONST"
    if kind in _OPERATOR_KINDS:
        return "OPERATOR"
    if kind in _DELIMITER_KINDS:
        return "DELIMITER"
    if kind is TokenKind.EOF:
        return "EOF"
    raise ValueError(f"未知 TokenKind: {kind}")
