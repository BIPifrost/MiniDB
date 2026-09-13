"""张振：六类不可变逻辑计划及执行前的结构校验。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from minidb.compiler._checks import Check
from minidb.compiler.bound import (
    BoundAssignment,
    BoundBinary,
    BoundColumn,
    BoundExpr,
    BoundLiteral,
    BoundUnary,
)
from minidb.compiler.bound_validation import validate_predicate
from minidb.core.schema import Schema, TableDef

if TYPE_CHECKING:
    from minidb.core.records import Row
    from minidb.core.result import ResultColumn
    from minidb.core.source import SourceSpan


@dataclass(frozen=True, slots=True)
class CreateTablePlan:
    """让执行器创建用户表的计划，保持语义阶段给出的表名和 Schema。"""
    table_name: str
    schema: Schema
    span: SourceSpan


@dataclass(frozen=True, slots=True)
class InsertPlan:
    """让执行器插入一行的计划，row 已排好列序，不需要再解析 SQL。"""
    table: TableDef
    row: Row
    span: SourceSpan


@dataclass(frozen=True, slots=True)
class SeqScanPlan:
    """从目标表顺序扫描完整记录的内部计划节点，还不是最终查询结果。"""
    table: TableDef
    span: SourceSpan


@dataclass(frozen=True, slots=True)
class FilterPlan:
    """过滤节点：child 提供完整行，predicate 判断哪些行保留。"""
    child: SeqScanPlan | FilterPlan
    predicate: BoundExpr
    span: SourceSpan


@dataclass(frozen=True, slots=True)
class ProjectPlan:
    """投影节点：column_indexes 决定输出哪些列、按什么顺序输出。"""
    child: SeqScanPlan | FilterPlan
    column_indexes: tuple[int, ...]
    output_columns: tuple[ResultColumn, ...]
    span: SourceSpan


@dataclass(frozen=True, slots=True)
class DeletePlan:
    """删除节点：从扫描或过滤结果获取记录位置，不能使用已丢失位置的投影结果。"""
    table: TableDef
    child: SeqScanPlan | FilterPlan
    span: SourceSpan


@dataclass(frozen=True, slots=True)
class UpdatePlan:
    """批量 UPDATE 计划；child 只提供完整旧行和 RowId，不缓存新值。"""

    table: TableDef
    child: SeqScanPlan | FilterPlan
    assignments: tuple[BoundAssignment, ...]
    span: SourceSpan


Plan = CreateTablePlan | InsertPlan | SeqScanPlan | FilterPlan | ProjectPlan | DeletePlan | UpdatePlan


def validate_plan(plan: Plan) -> None:
    """公开执行根仅允许 Create、Insert、Project、Delete；不读数据页。"""
    check = Check("validate_plan", plan=True)
    check.require(isinstance(plan, (CreateTablePlan, InsertPlan, ProjectPlan, DeletePlan, UpdatePlan)), "root", "完整的执行计划根节点", type(plan).__name__)
    check.span(plan.span)
    if isinstance(plan, CreateTablePlan):
        check.name(plan.table_name)
        check.schema(plan.schema)
    elif isinstance(plan, InsertPlan):
        check.table(plan.table)
        check.row(plan.row, plan.table.schema)
    else:
        table = _validate_stream(plan.child, check, plan.span)
        if isinstance(plan, ProjectPlan):
            check.projection(table, plan.column_indexes, plan.output_columns)
        elif isinstance(plan, DeletePlan):
            check.table(plan.table)
            check.require(plan.table == table, "table", "与删除输入的扫描表一致", repr(plan.table.ref))
        else:
            check.table(plan.table)
            check.require(plan.table == table, "table", "与更新输入的扫描表一致", repr(plan.table.ref))
            _validate_assignments(plan.assignments, table, check)


def _validate_stream(node, check: Check, parent) -> TableDef:
    """沿 Filter 向下找到 SeqScan，拒绝循环和错误节点，然后核对条件引用的原表列。"""
    # 先沿 child 找到最底部的数据源，再用该表的完整 Schema 校验所有 Filter。
    # 如果中途遇到 Project，原行的字段和 RowId 可能已丢失，不能继续当扫描流使用。
    filters = []
    seen = set()
    while isinstance(node, FilterPlan):
        check.require(id(node) not in seen, "child", "无环计划", type(node).__name__)
        seen.add(id(node))
        check.span(node.span, parent=parent)
        check.require(node.predicate is not None, "predicate", "Filter 必须带条件", None)
        filters.append(node)
        parent = node.span
        node = node.child
    check.require(isinstance(node, SeqScanPlan), "child", "SeqScan 或 Filter，不能接收 Project", type(node).__name__)
    check.span(node.span, parent=parent)
    check.table(node.table)
    for filtered in reversed(filters):
        validate_predicate(filtered.predicate, node.table, check, filtered.span)
    return node.table


def _validate_assignments(assignments, table: TableDef, check: Check) -> None:
    """拒绝空、重复、越界赋值；值表达式的类型由绑定阶段统一校验。"""
    check.require(
        isinstance(assignments, tuple) and bool(assignments),
        "assignments",
        "非空 tuple[BoundAssignment, ...]",
        repr(assignments),
    )
    seen: set[int] = set()
    for index, assignment in enumerate(assignments):
        check.require(
            isinstance(assignment, BoundAssignment),
            f"assignments[{index}]",
            "BoundAssignment",
            type(assignment).__name__,
        )
        check.require(
            type(assignment.column_index) is int
            and 0 <= assignment.column_index < len(table.schema.columns),
            f"assignments[{index}].column_index",
            "有效列序号",
            assignment.column_index,
        )
        check.require(
            assignment.column_index not in seen,
            f"assignments[{index}].column_index",
            "每列最多赋值一次",
            assignment.column_index,
        )
        check.require(
            isinstance(assignment.value, (BoundColumn, BoundLiteral, BoundUnary, BoundBinary)),
            f"assignments[{index}].value",
            "BoundExpr",
            type(assignment.value).__name__,
        )
        seen.add(assignment.column_index)
