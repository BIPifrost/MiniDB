"""不可变逻辑计划；只检查结构，不执行查询或访问数据页。"""
from __future__ import annotations
from dataclasses import dataclass
from minidb.compiler._checks import Check
from minidb.compiler.bound import BoundAssignment, BoundExpr, BoundLiteral
from minidb.compiler.bound_validation import validate_assignments, validate_index_target, validate_predicate
from minidb.core.schema import IndexBounds, IndexDef, Schema, TableDef
from minidb.core.result import ResultColumn
from minidb.core.source import SourceSpan


@dataclass(frozen=True, slots=True)
class CreateTablePlan:
    table_name: str
    schema: Schema
    span: SourceSpan


@dataclass(frozen=True, slots=True)
class InsertPlan:
    table: TableDef
    row: tuple
    span: SourceSpan


@dataclass(frozen=True, slots=True)
class SeqScanPlan:
    table: TableDef
    span: SourceSpan


@dataclass(frozen=True, slots=True)
class IndexScanPlan:
    """优化器选中的索引及规范化边界；root_page_id 是稳定索引锚点。"""
    table: TableDef
    index: IndexDef
    has_lower: bool
    lower: BoundLiteral | None
    lower_inclusive: bool
    has_upper: bool
    upper: BoundLiteral | None
    upper_inclusive: bool
    null_only: bool
    span: SourceSpan


@dataclass(frozen=True, slots=True)
class FilterPlan:
    child: SeqScanPlan | IndexScanPlan | FilterPlan
    predicate: BoundExpr
    span: SourceSpan


@dataclass(frozen=True, slots=True)
class ProjectPlan:
    child: SeqScanPlan | IndexScanPlan | FilterPlan
    column_indexes: tuple[int, ...]
    output_columns: tuple[ResultColumn, ...]
    span: SourceSpan


@dataclass(frozen=True, slots=True)
class DeletePlan:
    table: TableDef
    child: SeqScanPlan | IndexScanPlan | FilterPlan
    span: SourceSpan


@dataclass(frozen=True, slots=True)
class UpdatePlan:
    """SET 只保存绑定表达式；计算全部旧行的新值由 Executor.prepare_write 负责。"""
    table: TableDef
    child: SeqScanPlan | FilterPlan
    assignments: tuple[BoundAssignment, ...]
    span: SourceSpan


@dataclass(frozen=True, slots=True)
class CreateIndexPlan:
    name: str
    table: TableDef
    column_index: int
    unique: bool
    span: SourceSpan


@dataclass(frozen=True, slots=True)
class DescribePlan:
    table: TableDef
    span: SourceSpan


@dataclass(frozen=True, slots=True)
class ExplainPlan:
    child: InsertPlan | ProjectPlan | DeletePlan | UpdatePlan
    span: SourceSpan


Plan = (CreateTablePlan | InsertPlan | SeqScanPlan | IndexScanPlan | FilterPlan |
        ProjectPlan | DeletePlan | UpdatePlan | CreateIndexPlan | DescribePlan | ExplainPlan)


def validate_plan(plan: Plan) -> None:
    """拒绝手工拼接的错误计划，尤其是循环、错误列号和非规范边界。"""
    check = Check("validate_plan", plan=True)
    roots = (CreateTablePlan, InsertPlan, ProjectPlan, DeletePlan, UpdatePlan,
             CreateIndexPlan, DescribePlan, ExplainPlan)
    check.require(isinstance(plan, roots), "root", "完整执行根", type(plan).__name__)
    check.span(plan.span)
    if isinstance(plan, ExplainPlan):
        check.require(isinstance(plan.child, (InsertPlan, ProjectPlan, DeletePlan, UpdatePlan)),
                      "child", "可EXPLAIN的非DDL计划", type(plan.child).__name__)
        check.span(plan.child.span, parent=plan.span)
        validate_plan(plan.child)
    elif isinstance(plan, CreateTablePlan):
        check.name(plan.table_name)
        check.schema(plan.schema)
    elif isinstance(plan, (InsertPlan, CreateIndexPlan, DescribePlan)):
        check.table(plan.table)
        if isinstance(plan, InsertPlan):
            check.row(plan.row, plan.table.schema)
        elif isinstance(plan, CreateIndexPlan):
            validate_index_target(plan.name, plan.table, plan.column_index, plan.unique, check)
    else:
        table = _validate_stream(plan.child, check, plan.span, allow_index=not isinstance(plan, UpdatePlan))
        if isinstance(plan, ProjectPlan):
            check.projection(table, plan.column_indexes, plan.output_columns)
        else:
            check.table(plan.table)
            check.require(plan.table == table, "table", "与扫描表一致", plan.table.ref.name)
            if isinstance(plan, UpdatePlan):
                validate_assignments(plan.assignments, table, check, plan.span)


def _validate_stream(node, check, parent, *, allow_index=True):
    filters, seen = [], set()
    while isinstance(node, FilterPlan):
        check.require(id(node) not in seen, "child", "无环计划", type(node).__name__)
        seen.add(id(node))
        check.span(node.span, parent=parent)
        check.require(node.predicate is not None, "predicate", "Filter条件", None)
        filters.append(node)
        parent, node = node.span, node.child
    kinds = (SeqScanPlan, IndexScanPlan) if allow_index else (SeqScanPlan,)
    check.require(isinstance(node, kinds), "child", "合法完整行扫描", type(node).__name__)
    check.span(node.span, parent=parent)
    check.table(node.table)
    if isinstance(node, IndexScanPlan):
        _validate_index_scan(node, check)
    for filtered in reversed(filters):
        validate_predicate(filtered.predicate, node.table, check, filtered.span)
    return node.table


def _validate_index_scan(node, check):
    check.require(isinstance(node.index, IndexDef), "index", "IndexDef", type(node.index).__name__)
    index = node.index
    check.require(index.table_id == node.table.ref.table_id
                  and index.column_index < len(node.table.schema.columns),
                  "index", "所属表及有效列", index.name)
    column = node.table.schema.columns[index.column_index]
    for present, value in ((node.has_lower, node.lower), (node.has_upper, node.upper)):
        check.require(type(present) is bool and
                      (isinstance(value, BoundLiteral) if present else value is None),
                      "boundary", "has与BoundLiteral一致", repr(value))
        if present:
            check.span(value.span, parent=node.span)
            check.require(value.type_spec == column.type_spec and value.value is not None,
                          "boundary.type_spec", "索引列的非NULL规范常量", repr(value))
            check.value(value.value, column.type_spec, nullable=False)
    # 复用唯一的边界组合规则，再统一映射为 INVALID_PLAN。
    try:
        IndexBounds(node.has_lower, node.lower.value if node.lower else None, node.lower_inclusive,
                    node.has_upper, node.upper.value if node.upper else None, node.upper_inclusive,
                    node.null_only)
    except Exception as error:
        check.require(False, "bounds", "一致的边界标志", str(error))
