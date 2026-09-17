"""成本模型与统计信息（MiniDB 成本优化第一版）。

为 Optimizer 提供"统计 → 成本 → 路径决策"的纯函数层：

- 行数由调用方（Session）从存储层只读统计后传入，本模块不访问数据页；
- 页数与树高按行编码与 B+ 树扇出公式估算（口径见各函数文档）；
- 选择率对主键等值给出精确 1/N，范围谓词用 System R 的 1/3 启发式，
  无法估计时取保守默认值 0.1；
- 成本公式以页 IO 为纲：全表扫 = 表页数；索引扫 = 树高 + 命中页数
  × 随机 IO 惩罚系数（C_RAND，经验假设，暂无计时器校准）。

与 index_selection.py 的边界：``choose_index_scan`` 决定"能用哪些索引"，
本模块决定"按成本是否采用该索引"；成本不占优时返回 None，由 Optimizer
保持原 SeqScan。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from minidb.compiler.bound import (
    BoundBinary,
    BoundColumn,
    BoundExpr,
    BoundIsNull,
    BoundLiteral,
    BoundUnary,
)
from minidb.compiler.plan import IndexScanPlan, Plan, SeqScanPlan
from minidb.core.expressions import ExprOp
from minidb.core.schema import DataType, Schema
from minidb.storage.row_codec import (
    BOOL_BYTE_SIZE,
    DATE_BYTE_SIZE,
    DECIMAL_BYTE_SIZE,
    INT_BYTE_SIZE,
    MAX_ROW_SIZE,
    ROW_PREFIX_SIZE,
    VARCHAR_LENGTH_SIZE,
)

# 每索引节点约可容纳 (键8B + 子指针8B) 个条目，用于估算树高。
FANOUT = MAX_ROW_SIZE // 16
# 随机 IO 惩罚系数：磁盘上随机读一页按两页顺序读计（经验假设）。
C_RAND = 2.0
# 范围谓词选择率启发式（System R 1/3 规则）。
RANGE_SELECTIVITY = 1 / 3
# 无法估计时的保守默认选择率。
DEFAULT_SELECTIVITY = 0.1
# 未声明长度的 VARCHAR 平均字节假设（估算口径）。
VARCHAR_AVG_BYTES = 32


def estimate_row_size(schema: Schema) -> int:
    """按 v2 行格式估算单行编码字节数（不含尾随填充）。

    VARCHAR 长度不定，按声明长度的一半估算；未声明长度按 32B 假设。
    其余类型为定长负载，与 row_codec 的常量一一对应。
    """
    columns = schema.columns
    size = ROW_PREFIX_SIZE + (len(columns) + 7) // 8
    for column in columns:
        kind = column.type_spec.kind
        if kind is DataType.INT:
            size += INT_BYTE_SIZE
        elif kind is DataType.BOOL:
            size += BOOL_BYTE_SIZE
        elif kind is DataType.DATE:
            size += DATE_BYTE_SIZE
        elif kind is DataType.DECIMAL:
            size += DECIMAL_BYTE_SIZE
        elif kind is DataType.VARCHAR:
            declared = column.type_spec.length
            average = declared // 2 if isinstance(declared, int) and declared > 0 else VARCHAR_AVG_BYTES
            size += VARCHAR_LENGTH_SIZE + average
        else:
            raise TypeError(f"不支持的行编码类型: {kind}")
    return size


@dataclass(frozen=True, slots=True)
class TableStats:
    """一张表的统计快照；rows 为实测，pages/height 为公式估算。"""

    rows: int
    pages: int
    height: int
    row_size: int


def collect_table_stats(rows: int, schema: Schema) -> TableStats:
    """由实测行数与 Schema 推导统计：页数 = ceil(rows / 每页行数)、树高按扇出。"""
    row_size = estimate_row_size(schema)
    per_page = MAX_ROW_SIZE // row_size
    pages = math.ceil(rows / per_page) if rows else 0
    height = max(1, math.ceil(math.log(max(rows, 1), FANOUT)))
    return TableStats(rows, pages, height, row_size)


def cost_seq(stats: TableStats) -> float:
    """全表顺序扫描成本：读全部表页。"""
    return stats.pages * 1.0


def cost_index(stats: TableStats, selectivity: float) -> float:
    """索引扫描成本：树高定位 + 命中数据页数（含随机 IO 惩罚）。"""
    s = min(1.0, max(0.0, selectivity))
    return stats.height + stats.pages * s * C_RAND


def index_selectivity(chosen: IndexScanPlan, stats: TableStats) -> float:
    """按索引边界推选择率：点查（等值/IS NULL）精确 1/N，范围取 1/3。"""
    if chosen.null_only:
        return 1.0 / max(1, stats.rows)
    if (chosen.has_lower and chosen.has_upper
            and chosen.lower is not None and chosen.upper is not None
            and chosen.lower.value == chosen.upper.value):
        return 1.0 / max(1, stats.rows)
    return RANGE_SELECTIVITY


def decide_scan_path(chosen: IndexScanPlan, stats: TableStats) -> IndexScanPlan | None:
    """按成本决定是否采用索引：索引不更便宜时返回 None（保持 SeqScan）。

    最坏情况与现状持平（仍走索引），选对时纠正小表/高选择率下的次优扫描。
    """
    if cost_index(stats, index_selectivity(chosen, stats)) < cost_seq(stats):
        return chosen
    return None


def node_cost(plan: Plan, stats: TableStats | None) -> float | None:
    """扫描节点的估算成本；非扫描节点或缺少统计时返回 None（渲染为 '-'）。"""
    if stats is None:
        return None
    if isinstance(plan, SeqScanPlan):
        return cost_seq(stats)
    if isinstance(plan, IndexScanPlan):
        return cost_index(stats, index_selectivity(plan, stats))
    return None


def estimate_selectivity(predicate: BoundExpr, stats: TableStats) -> float:
    """通用谓词选择率：等值 = 1/N、范围 = 1/3、AND 乘 / OR 加、无法估计 = 0.1。

    当前无直方图与多列统计，普通列分布未知，估计值是启发式；主键等值
    是本项目中唯一精确的选择率输入。
    """
    if isinstance(predicate, BoundLiteral):
        return 1.0 if predicate.value is True else 0.0
    if isinstance(predicate, BoundColumn):
        return 1.0
    if isinstance(predicate, BoundIsNull):
        return 1.0 / max(1, stats.rows)
    if isinstance(predicate, BoundUnary):
        # NOT 在三值逻辑下与 operand 选择率互补；无分布信息时保守取原值。
        return estimate_selectivity(predicate.operand, stats)
    if isinstance(predicate, BoundBinary):
        op = predicate.op
        if op is ExprOp.AND:
            return estimate_selectivity(predicate.left, stats) * estimate_selectivity(
                predicate.right, stats
            )
        if op is ExprOp.OR:
            return min(
                1.0,
                estimate_selectivity(predicate.left, stats)
                + estimate_selectivity(predicate.right, stats),
            )
        if op in (ExprOp.EQ, ExprOp.NE, ExprOp.LT, ExprOp.LE, ExprOp.GT, ExprOp.GE):
            return 1.0 / max(1, stats.rows)
    return DEFAULT_SELECTIVITY


__all__ = [
    "TableStats",
    "collect_table_stats",
    "cost_seq",
    "cost_index",
    "decide_scan_path",
    "estimate_row_size",
    "estimate_selectivity",
    "index_selectivity",
    "node_cost",
]
