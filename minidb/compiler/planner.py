"""张振：将已绑定语句转换为逻辑计划，不执行语句或修改目录。"""

from minidb.compiler.bound import BoundCreate, BoundDelete, BoundInsert, BoundSelect, BoundStatement
from minidb.compiler.bound_validation import validate_bound
from minidb.compiler.plan import (
    CreateTablePlan, DeletePlan, FilterPlan, InsertPlan, Plan,
    ProjectPlan, SeqScanPlan, validate_plan,
)


class Planner:
    """把已绑定语句组织为计划树；不执行 SQL，也不接触数据文件。"""
    def build(self, bound: BoundStatement) -> Plan:
        """先验收 Bound，再生成对应计划，最后检查计划结构并返回根节点。"""
        validate_bound(bound, plan=True)
        if isinstance(bound, BoundCreate):
            plan = CreateTablePlan(bound.table_name, bound.schema, bound.span)
        elif isinstance(bound, BoundInsert):
            # 语义阶段已按 Schema 排好 Row；Planner 原样传递，不再重排。
            plan = InsertPlan(bound.table, bound.row, bound.span)
        else:
            # 数据自下而上流动：SeqScan 读完整行，Filter 筛行，Project 最后选列。
            # 缺少 WHERE 时省略 Filter；DELETE 则把筛出的记录交给 DeletePlan。
            stream = SeqScanPlan(bound.table, bound.span)
            if bound.predicate is not None:
                stream = FilterPlan(stream, bound.predicate, bound.span)
            if isinstance(bound, BoundSelect):
                plan = ProjectPlan(stream, bound.projection, bound.output_columns, bound.span)
            elif isinstance(bound, BoundDelete):
                plan = DeletePlan(bound.table, stream, bound.span)
        validate_plan(plan)
        return plan
