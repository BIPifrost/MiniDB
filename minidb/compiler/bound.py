"""张振：语义绑定后的不可变数据对象，字段对应规划第 15.5 节。

构造与校验分离：Semantic 输出和 Planner 输入均调用 validate_bound。
前置类型按正式模块引用，不在此复制 SourceSpan、Row 或 ResultColumn。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from minidb.core.expressions import ExprOp
from minidb.core.schema import DataType, Schema, TableDef

if TYPE_CHECKING:
    # 正式类型由其他成员提供。这里只写注解，不新增一套同名公共类型。
    from minidb.core.records import Row
    from minidb.core.result import ResultColumn
    from minidb.core.source import SourceSpan


@dataclass(frozen=True, slots=True)
class BoundColumn:
    """绑定后的列引用。index 是原表的列序号，例如 age 对应索引 2。"""
    index: int            # 从 0 开始，针对原表 Schema，不针对 SELECT 输出列。
    data_type: DataType   # 绑定时从 ColumnDef 取得，执行器无需重新按名称查找。
    span: SourceSpan      # 保留原 SQL 中该列名的位置，方便定位错误。


@dataclass(frozen=True, slots=True)
class BoundLiteral:
    """绑定后的常量，保存原值、类型和位置；BOOL 常量只由后续优化器生成。"""
    value: int | str | bool
    data_type: DataType
    span: SourceSpan


@dataclass(frozen=True, slots=True)
class BoundAssignment:
    """UPDATE 的一项赋值，column_index 针对目标表原始 Schema。"""

    column_index: int
    value: BoundExpr
    span: SourceSpan


@dataclass(frozen=True, slots=True)
class BoundUnary:
    """绑定后的一元 NOT 节点，operand 是子条件，op_span 定位 NOT 本身。"""
    op: ExprOp
    operand: BoundExpr
    data_type: DataType
    op_span: SourceSpan
    span: SourceSpan


@dataclass(frozen=True, slots=True)
class BoundBinary:
    """绑定后的二元比较或逻辑节点，left/right 保存已经绑定的两棵子树。"""
    op: ExprOp
    left: BoundExpr
    right: BoundExpr
    data_type: DataType
    op_span: SourceSpan
    span: SourceSpan


# “|” 表示类型并集：一个 BoundExpr 可以是上述任意一种表达式节点。
BoundExpr = BoundColumn | BoundLiteral | BoundUnary | BoundBinary


@dataclass(frozen=True, slots=True)
class BoundCreate:
    """检查通过的建表请求，仅有名字和 Schema；表号及页号留给执行阶段分配。"""
    table_name: str
    schema: Schema
    span: SourceSpan


@dataclass(frozen=True, slots=True)
class BoundInsert:
    """检查通过的插入请求，row 已按 table.schema 的列顺序排列。"""
    table: TableDef
    row: Row
    span: SourceSpan


@dataclass(frozen=True, slots=True)
class BoundSelect:
    """检查通过的查询：projection 为列索引，output_columns 为结果列，predicate 为可选条件。"""
    table: TableDef
    projection: tuple[int, ...]               # 如 SELECT name,id 对应 (1, 0)。
    output_columns: tuple[ResultColumn, ...]  # 与 projection 一一对应，允许重复列名。
    predicate: BoundExpr | None               # WHERE 条件；没有 WHERE 时为 None。
    span: SourceSpan


@dataclass(frozen=True, slots=True)
class BoundDelete:
    """检查通过的删除请求，predicate=None 表示删除该表全部记录。"""
    table: TableDef
    predicate: BoundExpr | None
    span: SourceSpan


@dataclass(frozen=True, slots=True)
class BoundUpdate:
    """绑定后的 UPDATE；执行器在同一条旧行快照上计算所有赋值。"""

    table: TableDef
    assignments: tuple[BoundAssignment, ...]
    predicate: BoundExpr | None
    span: SourceSpan


BoundStatement = BoundCreate | BoundInsert | BoundSelect | BoundDelete | BoundUpdate
