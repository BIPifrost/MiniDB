"""把已有执行器接到正式 Plan、CatalogManager 和 StorageEngine 接口。

这里仍把扫描结果收集到内存中，保留适合教学项目规模的简单执行方式。
表达式求值、正式 RowCodec 和页式 StorageEngine 均通过各自稳定接口接入。
"""

from __future__ import annotations

from minidb.compiler.plan import (
    CreateTablePlan,
    DeletePlan,
    FilterPlan,
    InsertPlan,
    Plan,
    ProjectPlan,
    SeqScanPlan,
    validate_plan,
)
from minidb.core.errors import DbError, ErrorStage, TABLE_EXISTS
from minidb.core.result import ExecRecord, QueryResult
from minidb.core.schema import TableDef, TableRef

from . import expression_eval
from .context import ExecutionContext


class Executor:
    """接收张振的逻辑计划，调用会话传入的目录和存储对象。"""

    def execute(self, plan: Plan, context: ExecutionContext) -> QueryResult:
        """执行完整语句；内部扫描和过滤节点不能单独作为公开入口。"""
        # 复用已有校验，不在执行器重复定义一套计划规则。
        validate_plan(plan)
        if isinstance(plan, CreateTablePlan):
            return self._execute_create_table(plan, context)
        if isinstance(plan, InsertPlan):
            return self._execute_insert(plan, context)
        if isinstance(plan, ProjectPlan):
            return self._execute_project(plan, context)
        if isinstance(plan, DeletePlan):
            return self._execute_delete(plan, context)
        raise TypeError(f"unsupported plan type: {type(plan).__name__}")

    def _execute_create_table(
        self, plan: CreateTablePlan, context: ExecutionContext
    ) -> QueryResult:
        """先复核表名，再领表号、创建根页，最后交给目录登记。"""
        # 计划生成后目录可能已有同名表，例如同一计划被重复执行。
        # 调用张振已有的查询接口提前拒绝，避免到登记时才报错、留下多分配的页。
        if context.catalog.find_table(plan.table_name) is not None:
            raise DbError(
                ErrorStage.SEMANTIC, TABLE_EXISTS, "表名已经登记", plan.span,
                {"operation": "Executor.execute", "table_name": plan.table_name},
            )
        table_id = context.catalog.reserve_table_id()
        root_page_id = context.storage.create_heap(table_id)
        table = TableDef(TableRef(table_id, plan.table_name, root_page_id), plan.schema)
        # 沿用已有目录持久化方法，由它调用 RowCodec 完成全部目录行预检。
        context.catalog.persist_and_register(table)
        # 写入后的 sync 由 Session 统一调用，与 CatalogManager 的约定一致。
        return QueryResult(affected_rows=0, message="CREATE TABLE OK")

    def _execute_insert(self, plan: InsertPlan, context: ExecutionContext) -> QueryResult:
        """正式 InsertPlan 已携带表定义和排好列序的行，直接传给存储。"""
        context.storage.insert_row(plan.table, plan.row)
        return QueryResult(affected_rows=1, message="1 row inserted")

    def _execute_seq_scan(
        self, plan: SeqScanPlan, context: ExecutionContext
    ) -> list[ExecRecord]:
        """把存储记录转成执行记录，保留删除要用的位置，并关闭扫描。"""
        scan = context.storage.scan_rows(plan.table)
        primary_error = None
        try:
            return [ExecRecord(record.values, record.row_id) for record in scan]
        except BaseException as error:
            primary_error = error
            raise
        finally:
            try:
                scan.close()
            except Exception as cleanup_error:
                if primary_error is None:
                    raise
                # 读取和关闭同时失败时，让调用者仍能看到最初的读取异常。
                primary_error.add_note(f"关闭扫描时又发生异常：{cleanup_error}")

    def _execute_filter(self, plan: FilterPlan, context: ExecutionContext) -> list[ExecRecord]:
        """对完整原行求条件值，同时保留删除阶段需要的 RowId。"""
        records = self._execute_stream(plan.child, context)
        return [
            record
            for record in records
            if expression_eval.evaluate(plan.predicate, record.values)
        ]

    def _execute_stream(
        self, plan: SeqScanPlan | FilterPlan, context: ExecutionContext
    ) -> list[ExecRecord]:
        """内部节点传递带 RowId 的记录，避免过早转成最终查询结果。"""
        if isinstance(plan, SeqScanPlan):
            return self._execute_seq_scan(plan, context)
        if isinstance(plan, FilterPlan):
            return self._execute_filter(plan, context)
        raise TypeError(f"unsupported stream type: {type(plan).__name__}")

    def _execute_project(self, plan: ProjectPlan, context: ExecutionContext) -> QueryResult:
        """使用计划中已绑定的列序号投影，输出列顺序和名称也直接取自计划。"""
        records = self._execute_stream(plan.child, context)
        rows = [tuple(record.values[index] for index in plan.column_indexes) for record in records]
        # 最终 QueryResult 只携带值，不再暴露用于删除的 RowId。
        return QueryResult(
            columns=list(plan.output_columns),
            rows=rows,
            affected_rows=None,
            message=f"{len(rows)} rows selected",
        )

    def _execute_delete(self, plan: DeletePlan, context: ExecutionContext) -> QueryResult:
        """用正式逐行删除接口替换旧 delete_rows，统计实际删除的数量。"""
        # _execute_stream 返回前已关闭扫描，之后才允许修改存储。
        records = self._execute_stream(plan.child, context)
        deleted = 0
        for record in records:
            if context.storage.delete_row(plan.table, record.row_id):
                deleted += 1
        # 所有 RowId 用完后才能回收空页，避免尚未删除的记录位置提前失效。
        context.storage.reclaim_empty_pages(plan.table)
        return QueryResult(affected_rows=deleted, message=f"{deleted} rows deleted")
