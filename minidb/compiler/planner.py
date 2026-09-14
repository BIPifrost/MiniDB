"""将绑定语句转换为逻辑计划；索引选择由优化器调用专门的纯函数完成。"""
from minidb.compiler.bound import (
    BoundCreate, BoundInsert, BoundSelect, BoundDelete, BoundUpdate,
    BoundCreateIndex, BoundDescribe, BoundExplain,
)
from minidb.compiler.bound_validation import validate_bound
from minidb.compiler.plan import (
    CreateTablePlan, InsertPlan, SeqScanPlan, FilterPlan, ProjectPlan,
    DeletePlan, UpdatePlan, CreateIndexPlan, DescribePlan, ExplainPlan, validate_plan,
)


class Planner:
    def build(self, bound):
        validate_bound(bound, plan=True)
        if isinstance(bound, BoundCreate):
            plan = CreateTablePlan(bound.table_name, bound.schema, bound.span)
        elif isinstance(bound, BoundInsert):
            plan = InsertPlan(bound.table, bound.row, bound.span)
        elif isinstance(bound, BoundCreateIndex):
            plan = CreateIndexPlan(bound.name, bound.table, bound.column_index, bound.unique, bound.span)
        elif isinstance(bound, BoundDescribe):
            plan = DescribePlan(bound.table, bound.span)
        elif isinstance(bound, BoundExplain):
            plan = ExplainPlan(self.build(bound.statement), bound.span)
        else:
            stream = SeqScanPlan(bound.table, bound.span)
            if bound.predicate is not None:
                stream = FilterPlan(stream, bound.predicate, bound.span)
            if isinstance(bound, BoundSelect):
                plan = ProjectPlan(stream, bound.projection, bound.output_columns, bound.span)
            elif isinstance(bound, BoundDelete):
                plan = DeletePlan(bound.table, stream, bound.span)
            elif isinstance(bound, BoundUpdate):
                plan = UpdatePlan(bound.table, stream, bound.assignments, bound.span)
        validate_plan(plan)
        return plan
