"""不可变v2绑定对象，只保存类型、列序和源码位置，不保存页、token或新RowId。"""
from __future__ import annotations
from dataclasses import dataclass
from typing import TYPE_CHECKING
from minidb.core.schema import Schema, TableDef, TypeSpec, as_type_spec
from minidb.core.expressions import ExprOp
if TYPE_CHECKING:
    from minidb.core.source import SourceSpan
    from minidb.core.result import ResultColumn


class _Typed:
    __slots__ = ()

    def __post_init__(self):
        if self.type_spec is not None:
            object.__setattr__(self, "type_spec", as_type_spec(self.type_spec))

    @property
    def data_type(self):
        """临时只读兼容属性；新消费方应使用type_spec和nullable。"""
        return None if self.type_spec is None else self.type_spec.kind


@dataclass(frozen=True, slots=True)
class BoundColumn(_Typed):
    index: int
    type_spec: TypeSpec
    span: SourceSpan
    nullable: bool = False


@dataclass(frozen=True, slots=True)
class BoundLiteral(_Typed):
    value: object
    type_spec: TypeSpec | None
    span: SourceSpan

    @property
    def nullable(self):
        return self.value is None


@dataclass(frozen=True, slots=True)
class BoundUnary(_Typed):
    op: ExprOp
    operand: BoundExpr
    type_spec: TypeSpec
    op_span: SourceSpan
    span: SourceSpan
    nullable: bool = False


@dataclass(frozen=True, slots=True)
class BoundBinary(_Typed):
    op: ExprOp
    left: BoundExpr
    right: BoundExpr
    type_spec: TypeSpec
    op_span: SourceSpan
    span: SourceSpan
    nullable: bool = False


@dataclass(frozen=True, slots=True)
class BoundIsNull:
    operand: BoundExpr
    negated: bool
    op_span: SourceSpan
    span: SourceSpan

    @property
    def type_spec(self):
        from minidb.core.schema import DataType
        return TypeSpec(DataType.BOOL)

    @property
    def data_type(self):
        return self.type_spec.kind

    @property
    def nullable(self):
        return False


BoundExpr = BoundColumn | BoundLiteral | BoundUnary | BoundBinary | BoundIsNull


@dataclass(frozen=True, slots=True)
class BoundAssignment:
    column_index: int
    value: BoundExpr
    span: SourceSpan


@dataclass(frozen=True, slots=True)
class BoundCreate:
    table_name: str
    schema: Schema
    span: SourceSpan


@dataclass(frozen=True, slots=True)
class BoundInsert:
    table: TableDef
    row: tuple
    span: SourceSpan


@dataclass(frozen=True, slots=True)
class BoundSelect:
    table: TableDef
    projection: tuple[int, ...]
    output_columns: tuple[ResultColumn, ...]
    predicate: BoundExpr | None
    span: SourceSpan


@dataclass(frozen=True, slots=True)
class BoundDelete:
    table: TableDef
    predicate: BoundExpr | None
    span: SourceSpan


@dataclass(frozen=True, slots=True)
class BoundUpdate:
    table: TableDef
    assignments: tuple[BoundAssignment, ...]
    predicate: BoundExpr | None
    span: SourceSpan


@dataclass(frozen=True, slots=True)
class BoundCreateIndex:
    name: str
    table: TableDef
    column_index: int
    unique: bool
    span: SourceSpan


@dataclass(frozen=True, slots=True)
class BoundDescribe:
    table: TableDef
    span: SourceSpan


@dataclass(frozen=True, slots=True)
class BoundExplain:
    statement: BoundStatement
    span: SourceSpan


BoundStatement = (BoundCreate | BoundInsert | BoundSelect | BoundDelete | BoundUpdate
                  | BoundCreateIndex | BoundDescribe | BoundExplain)
