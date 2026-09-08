"""MiniDB 的抽象语法树（AST）公共定义。

AST 是 Parser 交给 Semantic 的中间结果：它只记录 SQL 写了什么以及每段
内容在源码中的位置，不查询目录、不检查列类型，也不执行任何数据库操作。

字段和构造顺序遵循工作计划第 6.3、15.4 节。所有节点都是不可变数据对象，
这样 Parser 交给 Semantic 后，后续模块不会意外改写同一棵语法树。结构检查
由 ``compiler._ast_validation.validate_ast`` 统一完成；这里不重复实现语义规则。
"""

from __future__ import annotations

from dataclasses import dataclass

from minidb.core.expressions import ExprOp
from minidb.core.schema import DataType
from minidb.core.source import SourceSpan


@dataclass(frozen=True, slots=True)
class NameRef:
    """SQL 中写出的名字及其源码范围。

    ``text`` 保留用户原始大小写；是否为合法标识符、是否需要归一化，
    由 Semantic 在知道语句上下文后处理。
    """

    text: str
    span: SourceSpan


@dataclass(frozen=True, slots=True)
class ColumnDecl:
    """CREATE TABLE 中的一列声明。"""

    name: NameRef
    data_type: DataType
    type_span: SourceSpan
    span: SourceSpan


@dataclass(frozen=True, slots=True)
class IdentifierExpr:
    """表达式中的列名引用，例如 ``age``。"""

    name: str
    span: SourceSpan


@dataclass(frozen=True, slots=True)
class LiteralExpr:
    """表达式中的整数或字符串常量。

    BOOL 常量不从用户 SQL AST 进入；它只允许作为后续优化产生的绑定值。
    """

    value: int | str
    data_type: DataType
    span: SourceSpan


@dataclass(frozen=True, slots=True)
class UnaryExpr:
    """一元表达式；当前只支持 ``NOT``。"""

    op: ExprOp
    operand: Expr
    op_span: SourceSpan
    span: SourceSpan


@dataclass(frozen=True, slots=True)
class BinaryExpr:
    """二元比较或逻辑表达式。"""

    op: ExprOp
    left: Expr
    right: Expr
    op_span: SourceSpan
    span: SourceSpan


# 表达式类型并集。括号不会生成节点，只由 Parser 根据优先级组织这些节点。
Expr = IdentifierExpr | LiteralExpr | UnaryExpr | BinaryExpr


@dataclass(frozen=True, slots=True)
class CreateTableStmt:
    """CREATE TABLE 语句。"""

    table_name: NameRef
    columns: tuple[ColumnDecl, ...]
    span: SourceSpan


@dataclass(frozen=True, slots=True)
class InsertStmt:
    """INSERT INTO ... VALUES ... 语句。"""

    table_name: NameRef
    columns: tuple[NameRef, ...]
    values: tuple[LiteralExpr, ...]
    span: SourceSpan


@dataclass(frozen=True, slots=True)
class SelectStmt:
    """SELECT 语句；星号由 ``select_all`` 表示，不生成特殊列名。"""

    table_name: NameRef
    select_all: bool
    columns: tuple[NameRef, ...]
    where: Expr | None
    span: SourceSpan


@dataclass(frozen=True, slots=True)
class DeleteStmt:
    """DELETE 语句。"""

    table_name: NameRef
    where: Expr | None
    span: SourceSpan


# 语句类型并集，供 Parser、Semantic 和 Session 使用同一个公共定义。
Statement = CreateTableStmt | InsertStmt | SelectStmt | DeleteStmt


__all__ = [
    "NameRef",
    "ColumnDecl",
    "IdentifierExpr",
    "LiteralExpr",
    "UnaryExpr",
    "BinaryExpr",
    "Expr",
    "CreateTableStmt",
    "InsertStmt",
    "SelectStmt",
    "DeleteStmt",
    "Statement",
]
