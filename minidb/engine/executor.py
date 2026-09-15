"""把正式 Plan 接到目录、流式读取和记录存储接口。"""

from __future__ import annotations

from collections.abc import Iterator
from uuid import UUID, uuid4

from minidb.compiler.plan import (
    CreateIndexPlan,
    CreateTablePlan,
    DeletePlan,
    FilterPlan,
    IndexScanPlan,
    InsertPlan,
    Plan,
    ProjectPlan,
    SeqScanPlan,
    UpdatePlan,
    validate_plan,
)
from minidb.core import errors
from minidb.core.errors import DbError, ErrorStage, TABLE_EXISTS
from minidb.core.records import RowMovement, RowUpdate, UpdateBatch, WriteKind
from minidb.core.result import ExecRecord, QueryResult, ResultCursor
from minidb.core.schema import (
    IndexBounds,
    IndexDef,
    IndexOrigin,
    PendingIndexDef,
    TableDef,
    TableRef,
)
from minidb.storage.constraints import validate_unique_stream
from minidb.storage.index_manager import MAX_BUILD_ENTRIES

from . import expression_eval
from .context import ExecutionContext
from .write_contract import PreparedWrite


class Executor:
    """接收张振的逻辑计划，调用会话传入的目录和存储对象。"""

    def execute(self, plan: Plan, context: ExecutionContext) -> QueryResult:
        """兼容旧 Session 的物化入口；新 SELECT 使用 execute_read。"""
        # 复用已有校验，不在执行器重复定义一套计划规则。
        validate_plan(plan)
        if isinstance(plan, CreateTablePlan):
            return self._execute_create_table(plan, context)
        if isinstance(plan, CreateIndexPlan):
            return self._execute_create_index(plan, context)
        if isinstance(plan, InsertPlan):
            return self._execute_insert(plan, context)
        if isinstance(plan, ProjectPlan):
            return self._execute_project(plan, context)
        if isinstance(plan, DeletePlan):
            return self._execute_delete(plan, context)
        raise TypeError(f"unsupported plan type: {type(plan).__name__}")

    def prepare_write(self, plan: Plan, context: ExecutionContext) -> PreparedWrite:
        """Prepare INSERT/UPDATE candidates through the shared validator.

        This method only reads rows and asks ConstraintValidator to normalize
        and authorize them. It never allocates pages or changes table state.
        DDL preparation remains owned by Session/Catalog.
        """
        validate_plan(plan)
        if not isinstance(plan, (InsertPlan, UpdatePlan)):
            raise NotImplementedError(
                "prepare_write for DELETE/CREATE_TABLE/CREATE_INDEX belongs to the Session DDL/DML adapter"
            )
        validator = context.constraint_validator
        if validator is None:
            raise RuntimeError("prepare_write requires ExecutionContext.constraint_validator")
        if not isinstance(context.session_id, UUID):
            raise RuntimeError("prepare_write requires Session.session_id")
        table = plan.table
        lookup = context.index_manager
        if lookup is None or not callable(getattr(lookup, "probe", None)):
            raise RuntimeError("prepare_write requires IndexManager ConstraintLookup")
        indexes_for_table = getattr(context.catalog, "indexes_for_table", None)
        if not callable(indexes_for_table):
            raise RuntimeError("prepare_write requires Catalog.indexes_for_table")
        affected_indexes = tuple(indexes_for_table(table.ref.table_id))
        prepared_id = uuid4()
        generation = context.catalog.generation
        if type(generation) is not int or generation < 0:
            raise RuntimeError("catalog generation must be a non-negative int")
        if isinstance(plan, InsertPlan):
            validated = validator.validate_insert(
                table, lookup, plan.row, prepared_id
            )
            return PreparedWrite(
                prepared_id, context.session_id, generation,
                WriteKind.INSERT, plan.span, table,
                None, None, (), (), validated.row, (), (), affected_indexes,
                _encoded_size(context, validated.row, table), validated.token,
            )
        if isinstance(plan, UpdatePlan):
            batch = self._collect_update_batch(plan, context)
            validated = validator.validate_update(
                table, lookup, batch, prepared_id
            )
            return PreparedWrite(
                prepared_id, context.session_id, generation,
                WriteKind.UPDATE, plan.span, table,
                None, None, (), (), None, validated.batch.items, (), affected_indexes,
                _encoded_batch_size(context, validated.batch, table), validated.token,
            )

    def apply_write(self, prepared: PreparedWrite, context: ExecutionContext) -> QueryResult:
        """Apply one validated batch and then synchronize affected indexes."""
        if not isinstance(prepared, PreparedWrite):
            raise TypeError("prepared must be a PreparedWrite")
        if context.catalog.generation != prepared.catalog_generation:
            raise RuntimeError("prepared write belongs to an old catalog generation")
        if context.session_id != prepared.session_id:
            raise RuntimeError("prepared write belongs to another session")
        if prepared.kind not in (
            WriteKind.INSERT,
            WriteKind.UPDATE,
            WriteKind.DELETE,
        ):
            raise NotImplementedError("DDL apply belongs to Session/Catalog")
        if prepared.table is None:
            raise RuntimeError("DML prepared write requires a target table")
        index_manager = context.index_manager
        if prepared.affected_indexes and (
            index_manager is None
            or not callable(getattr(index_manager, "apply_movements", None))
        ):
            raise RuntimeError("indexed write requires IndexManager.apply_movements")
        token = prepared.validation_token
        if prepared.kind is WriteKind.INSERT:
            movement = context.storage.insert_row(
                prepared.table, prepared.insert_row, token
            )
            movements = (movement,)
        elif prepared.kind is WriteKind.UPDATE:
            movements = context.storage.update_rows(
                prepared.table, UpdateBatch(prepared.updates), token
            )
        elif prepared.kind is WriteKind.DELETE:
            movements = context.storage.delete_rows(
                prepared.table, prepared.deletes, token
            )
        if type(movements) is not tuple or any(
            not isinstance(movement, RowMovement) for movement in movements
        ):
            raise TypeError("StorageEngine writes must return RowMovement values")
        if prepared.affected_indexes:
            index_manager.apply_movements(prepared.affected_indexes, movements)
        affected = len(movements)
        return QueryResult(affected_rows=affected, message=f"{affected} rows written")

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

    def _execute_create_index(
        self, plan: CreateIndexPlan, context: ExecutionContext
    ) -> QueryResult:
        """Build and register one user index after complete read-only preflight."""
        manager = context.index_manager
        if manager is None:
            raise RuntimeError("CreateIndexPlan requires ExecutionContext.index_manager")
        prepare_entries = getattr(manager, "prepare_build_entries", None)
        if not callable(prepare_entries):
            raise RuntimeError(
                "CreateIndexPlan requires IndexManager.prepare_build_entries"
            )

        scan = context.storage.scan_rows(plan.table)
        rows = []
        try:
            for stored in scan:
                rows.append(stored)
                if len(rows) > MAX_BUILD_ENTRIES:
                    raise DbError(
                        ErrorStage.EXECUTION,
                        errors.RESOURCE_LIMIT,
                        "建索引候选超过 100000 项",
                        plan.span,
                        {
                            "operation": "Executor.execute",
                            "limit": MAX_BUILD_ENTRIES,
                        },
                    )
        finally:
            scan.close()

        pending = PendingIndexDef(
            plan.name, plan.column_index, plan.unique, IndexOrigin.USER
        )
        materialized = tuple(rows)
        # Relationship-level uniqueness stays in the shared validator helper;
        # physical key encoding and ordering stay in IndexManager.
        validate_unique_stream(
            plan.table,
            (pending,),
            (stored.values for stored in materialized),
            manager,
        )
        entries = prepare_entries(plan.table, plan.column_index, materialized)

        # No page or catalog mutation occurs before every source row and key is
        # validated and the full canonical build order is known.
        index_id = context.catalog.reserve_index_id()
        anchor_id = manager.reserve_anchor()
        index = IndexDef(
            index_id,
            plan.name,
            plan.table.ref.table_id,
            plan.column_index,
            anchor_id,
            plan.unique,
            IndexOrigin.USER,
        )
        manager.create(index, entries)
        context.catalog.persist_and_register_index(index)
        return QueryResult(affected_rows=0, message="CREATE INDEX OK")

    def _execute_insert(self, plan: InsertPlan, context: ExecutionContext) -> QueryResult:
        """正式 InsertPlan 已携带表定义和排好列序的行，直接传给存储。"""
        context.storage.insert_row(plan.table, plan.row)
        return QueryResult(affected_rows=1, message="1 row inserted")

    def _execute_seq_scan(
        self, plan: SeqScanPlan, context: ExecutionContext
    ) -> Iterator[ExecRecord]:
        """惰性地产生执行记录，并在耗尽、关闭或异常时关闭 RowScan。"""
        scan = context.storage.scan_rows(plan.table)
        primary_error = None
        try:
            for record in scan:
                yield ExecRecord(record.values, record.row_id)
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

    def _execute_filter(
        self, plan: FilterPlan, context: ExecutionContext
    ) -> Iterator[ExecRecord]:
        """对完整原行求条件值，同时保留删除阶段需要的 RowId。"""
        records = self._execute_stream(plan.child, context)
        try:
            for record in records:
                if expression_eval.evaluate(plan.predicate, record.values) is True:
                    yield record
        finally:
            close = getattr(records, "close", None)
            if callable(close):
                close()

    def _execute_index_scan(
        self, plan: IndexScanPlan, context: ExecutionContext
    ) -> Iterator[ExecRecord]:
        """Resolve index RowIds back to current table rows and verify the key."""
        if context.index_manager is None:
            raise RuntimeError("IndexScanPlan requires ExecutionContext.index_manager")
        bounds = IndexBounds(
            plan.has_lower,
            plan.lower.value if plan.lower is not None else None,
            plan.lower_inclusive,
            plan.has_upper,
            plan.upper.value if plan.upper is not None else None,
            plan.upper_inclusive,
            plan.null_only,
        )
        cursor = context.index_manager.search(plan.index, bounds)
        try:
            for row_id in cursor:
                try:
                    stored = context.storage.fetch_row(plan.table, row_id)
                except DbError as error:
                    if error.code not in (
                        errors.STALE_ROW,
                        errors.SLOT_ID_INVALID,
                        errors.PAGE_CORRUPTED,
                    ):
                        raise
                    raise DbError(
                        ErrorStage.STORAGE,
                        errors.INDEX_CORRUPTED,
                        "索引项指向不存在或无效的表记录",
                        plan.span,
                        {
                            "index_name": plan.index.name,
                            "row_id": repr(row_id),
                            "cause": error.code,
                        },
                    ) from error
                if stored.values[plan.index.column_index] != cursor.current_value:
                    raise DbError(
                        ErrorStage.STORAGE,
                        errors.INDEX_CORRUPTED,
                        "索引键与当前表记录不一致",
                        plan.span,
                        {
                            "index_name": plan.index.name,
                            "row_id": repr(row_id),
                        },
                    )
                yield ExecRecord(stored.values, stored.row_id)
        finally:
            cursor.close()

    def _execute_stream(
        self,
        plan: SeqScanPlan | IndexScanPlan | FilterPlan,
        context: ExecutionContext,
    ) -> Iterator[ExecRecord]:
        """内部节点传递带 RowId 的记录，避免过早转成最终查询结果。"""
        if isinstance(plan, SeqScanPlan):
            return self._execute_seq_scan(plan, context)
        if isinstance(plan, IndexScanPlan):
            return self._execute_index_scan(plan, context)
        if isinstance(plan, FilterPlan):
            return self._execute_filter(plan, context)
        raise TypeError(f"unsupported stream type: {type(plan).__name__}")

    def _execute_project(self, plan: ProjectPlan, context: ExecutionContext) -> QueryResult:
        """在旧接口边界物化流，保持既有 Session 和调用方兼容。"""
        cursor = self.execute_read(plan, context)
        try:
            rows = list(cursor)
        finally:
            cursor.close()
        return QueryResult(
            columns=list(cursor.columns),
            rows=rows,
            affected_rows=None,
            message=f"{len(rows)} rows selected",
        )

    def execute_read(
        self, plan: ProjectPlan, context: ExecutionContext
    ) -> ResultCursor:
        """Open a lazy SELECT cursor without reading or materializing table rows.

        Handoff note: this is the Executor-side streaming contract. Session
        and CLI still use the legacy materialized ``execute`` path until
        ``Session.iter_results`` is connected.
        """
        validate_plan(plan)
        if not isinstance(plan, ProjectPlan):
            raise TypeError(f"unsupported read plan type: {type(plan).__name__}")
        records = self._execute_stream(plan.child, context)

        def projected_rows():
            try:
                for record in records:
                    yield tuple(
                        record.values[index] for index in plan.column_indexes
                    )
            finally:
                close = getattr(records, "close", None)
                if callable(close):
                    close()

        return ResultCursor(tuple(plan.output_columns), projected_rows())

    def _execute_delete(self, plan: DeletePlan, context: ExecutionContext) -> QueryResult:
        """用正式逐行删除接口替换旧 delete_rows，统计实际删除的数量。"""
        # _execute_stream 返回前已关闭扫描，之后才允许修改存储。
        stream = self._execute_stream(plan.child, context)
        try:
            records = list(stream)
        finally:
            stream.close()
        deleted = 0
        for record in records:
            if context.storage.delete_row(plan.table, record.row_id):
                deleted += 1
        # 所有 RowId 用完后才能回收空页，避免尚未删除的记录位置提前失效。
        context.storage.reclaim_empty_pages(plan.table)
        return QueryResult(affected_rows=deleted, message=f"{deleted} rows deleted")

    def _collect_update_batch(
        self, plan: UpdatePlan, context: ExecutionContext
    ) -> UpdateBatch:
        """Collect immutable UPDATE candidates without modifying storage.

        Handoff note: this is the PREPARING half only. It intentionally does
        not issue/consume a production token or call StorageEngine batch
        writes. The next integration step must pass this batch through the
        Validator and apply it atomically in ACTIVE.
        """
        validate_plan(plan)
        if not isinstance(plan, UpdatePlan):
            raise TypeError(f"unsupported update plan type: {type(plan).__name__}")
        stream = self._execute_stream(plan.child, context)
        updates: list[RowUpdate] = []
        try:
            for record in stream:
                if record.row_id is None:
                    raise TypeError("UPDATE input must retain its RowId")
                old_row = record.values
                evaluated = tuple(
                    (
                        assignment.column_index,
                        expression_eval.evaluate(assignment.value, old_row),
                    )
                    for assignment in plan.assignments
                )
                new_values = list(old_row)
                for column_index, value in evaluated:
                    new_values[column_index] = value
                updates.append(
                    RowUpdate(record.row_id, old_row, tuple(new_values))
                )
        finally:
            stream.close()
        return UpdateBatch(tuple(updates))


def _encoded_size(context: ExecutionContext, row: tuple, table: TableDef) -> int:
    """Read the storage-owned candidate size without duplicating RowCodec."""
    size_for = getattr(context.storage, "encoded_size", None)
    if not callable(size_for):
        raise RuntimeError("prepare_write requires StorageEngine.encoded_size")
    size = size_for(table, row)
    if type(size) is not int or size < 0:
        raise RuntimeError("StorageEngine.encoded_size must return a non-negative int")
    return size


def _encoded_batch_size(
    context: ExecutionContext, batch: UpdateBatch, table: TableDef
) -> int:
    """Record the same old+new byte budget checked by ConstraintValidator."""
    return sum(
        _encoded_size(context, item.expected_old, table)
        + _encoded_size(context, item.new_row, table)
        for item in batch.items
    )
