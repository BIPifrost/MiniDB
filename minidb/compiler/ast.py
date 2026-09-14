"""MiniDB 的不可变抽象语法树（AST）公共定义。

AST 只记录 SQL 的语法结构和源码位置。表、列是否存在，类型能否赋值，
以及约束是否冲突，均由 Semantic 在下一阶段判断。字段遵循 v2 工作计划
第 7.1 节；所有节点使用 frozen、slots，避免后续阶段意外改写语法树。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypeAlias

from minidb.core.expressions import ExprOp
from minidb.core.schema import DataType, TypeSpec
from minidb.core.source import SourceSpan


@dataclass(frozen=True, slots=True)
class NameRef:
    """SQL 中写出的名字及其源码范围；保留用户输入的原始大小写。"""

    text: str
    span: SourceSpan


@dataclass(frozen=True, slots=True)
class TypeDecl:
    """CREATE TABLE 中尚未经过语义归一化的类型声明。"""

    kind: DataType
    length: int | None
    precision: int | None
    scale: int | None
    span: SourceSpan


@dataclass(frozen=True, slots=True)
class ConstraintDecl:
    """列级约束；只有 DEFAULT 的 ``value`` 不为 None。"""

    kind: str
    value: LiteralExpr | None
    span: SourceSpan


@dataclass(frozen=True, slots=True)
class ColumnDecl:
    """CREATE TABLE 中的一列声明。"""

    name: NameRef
    type_decl: TypeDecl
    constraints: tuple[ConstraintDecl, ...]
    span: SourceSpan

    def __post_init__(self) -> None:
        # v1 调用顺序为 (name, DataType, type_span, span)。暂时接收这一
        # 形式，让已完成的旧 CRUD 测试和队友分支不会因 AST 升级立即失效。
        if isinstance(self.type_decl, DataType) and isinstance(self.constraints, SourceSpan):
            object.__setattr__(
                self,
                "type_decl",
                TypeDecl(self.type_decl, None, None, None, self.constraints),
            )
            object.__setattr__(self, "constraints", ())

    @property
    def data_type(self) -> DataType:
        """v1 读取方的只读兼容属性；类型真值只保存在 type_decl。"""

        return self.type_decl.kind

    @property
    def type_span(self) -> SourceSpan:
        """v1 读取方的只读兼容属性。"""

        return self.type_decl.span


@dataclass(frozen=True, slots=True)
class IdentifierExpr:
    """表达式中的列名引用，例如 ``age``。"""

    name: str
    span: SourceSpan


@dataclass(frozen=True, slots=True)
class LiteralExpr:
    """常量；NULL 使用 ``value=None, type_spec=None`` 表示未定类型。"""

    value: object
    type_spec: TypeSpec | None
    span: SourceSpan

    def __post_init__(self) -> None:
        # 兼容 v1 手工 AST 使用 DataType 的写法，但不保留第二份类型来源。
        if isinstance(self.type_spec, DataType):
            object.__setattr__(self, "type_spec", TypeSpec(self.type_spec))

    @property
    def data_type(self) -> DataType | None:
        """v1 读取方的只读兼容属性。"""

        return None if self.type_spec is None else self.type_spec.kind


@dataclass(frozen=True, slots=True)
class UnaryExpr:
    """一元逻辑表达式；当前只允许 NOT。"""

    op: ExprOp
    operand: Expr
    op_span: SourceSpan
    span: SourceSpan


@dataclass(frozen=True, slots=True)
class BinaryExpr:
    """二元比较或 AND/OR 逻辑表达式。"""

    op: ExprOp
    left: Expr
    right: Expr
    op_span: SourceSpan
    span: SourceSpan


@dataclass(frozen=True, slots=True)
class IsNullExpr:
    """``expr IS NULL`` 或 ``expr IS NOT NULL``。"""

    operand: Expr
    negated: bool
    op_span: SourceSpan
    span: SourceSpan


Expr: TypeAlias = IdentifierExpr | LiteralExpr | UnaryExpr | BinaryExpr | IsNullExpr


@dataclass(frozen=True, slots=True)
class Assignment:
    """UPDATE SET 中的一项赋值；右侧只允许列引用或常量。"""

    target: NameRef
    value: IdentifierExpr | LiteralExpr
    span: SourceSpan


@dataclass(frozen=True, slots=True)
class CreateTableStmt:
    table_name: NameRef
    columns: tuple[ColumnDecl, ...]
    span: SourceSpan


@dataclass(frozen=True, slots=True)
class InsertStmt:
    table_name: NameRef
    columns: tuple[NameRef, ...]
    values: tuple[LiteralExpr, ...]
    span: SourceSpan


@dataclass(frozen=True, slots=True)
class SelectStmt:
    """星号由 ``select_all=True`` 表示，此时 columns 必须为空。"""

    table_name: NameRef
    select_all: bool
    columns: tuple[NameRef, ...]
    where: Expr | None
    span: SourceSpan


@dataclass(frozen=True, slots=True)
class DeleteStmt:
    table_name: NameRef
    where: Expr | None
    span: SourceSpan


@dataclass(frozen=True, slots=True)
class UpdateStmt:
    table: NameRef
    assignments: tuple[Assignment, ...]
    predicate: Expr | None
    span: SourceSpan


@dataclass(frozen=True, slots=True)
class CreateIndexStmt:
    name: NameRef
    table: NameRef
    column: NameRef
    unique: bool
    span: SourceSpan


@dataclass(frozen=True, slots=True)
class DescribeStmt:
    table: NameRef
    span: SourceSpan


@dataclass(frozen=True, slots=True)
class ExplainStmt:
    statement: InsertStmt | SelectStmt | DeleteStmt | UpdateStmt
    span: SourceSpan


Statement: TypeAlias = (
    CreateTableStmt
    | InsertStmt
    | SelectStmt
    | DeleteStmt
    | UpdateStmt
    | CreateIndexStmt
    | DescribeStmt
    | ExplainStmt
)


__all__ = [
    "NameRef",
    "TypeDecl",
    "ConstraintDecl",
    "ColumnDecl",
    "IdentifierExpr",
    "LiteralExpr",
    "UnaryExpr",
    "BinaryExpr",
    "IsNullExpr",
    "Expr",
    "Assignment",
    "CreateTableStmt",
    "InsertStmt",
    "SelectStmt",
    "DeleteStmt",
    "UpdateStmt",
    "CreateIndexStmt",
    "DescribeStmt",
    "ExplainStmt",
    "Statement",
]
