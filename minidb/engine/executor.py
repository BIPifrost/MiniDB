"""PARTIAL: execution scaffold for fixed, temporary logical plans.

Already stable:

* the public return type is ``minidb.core.result.QueryResult``;
* dependencies arrive through the official ``ExecutionContext`` fields.

Still temporary and waiting for teammates:

* plan node classes and their field names wait for Zhang Zhen's
  ``compiler/plan.py``;
* table metadata and catalog operations wait for Zhang Zhen's schema and
  ``CatalogManager`` implementations;
* row scans and mutations wait for the real page-backed ``StorageEngine``.

Consequently, this file currently imports ``._scaffold`` and recursively
materializes rows.  Remove that import when the real Plan types land.  The
final executor must use closeable ``RowScan`` values, preserve ``RowId`` until
DELETE completes, and never call the legacy create_table/delete_rows methods.
"""

from __future__ import annotations

from minidb.core.result import QueryResult, ResultColumn

# TEMPORARY IMPORT: see the module docstring and _scaffold.py replacement list.
from ._scaffold import (
    CreateTablePlan,
    DeletePlan,
    FilterPlan,
    InsertPlan,
    Plan,
    ProjectPlan,
    SeqScanPlan,
    ValuesPlan,
)
from .context import ExecutionContext


class Executor:
    """Dispatch temporary plans while preserving the final public entry point.

    ``execute(plan, context) -> QueryResult`` is the intended stable call
    shape.  Operator internals below are scaffolding, not the final execution
    algorithm.
    """

    def execute(self, plan: Plan, context: ExecutionContext) -> QueryResult:
        if isinstance(plan, ValuesPlan):
            columns = [ResultColumn(name, "VARCHAR") for name in plan.columns]
            return QueryResult(
                columns=columns,
                rows=list(plan.rows),
                affected_rows=None,
                message="values",
            )
        if isinstance(plan, CreateTablePlan):
            return self._execute_create_table(plan, context)
        if isinstance(plan, InsertPlan):
            return self._execute_insert(plan, context)
        if isinstance(plan, SeqScanPlan):
            return self._execute_seq_scan(plan, context)
        if isinstance(plan, FilterPlan):
            return self._execute_filter(plan, context)
        if isinstance(plan, ProjectPlan):
            return self._execute_project(plan, context)
        if isinstance(plan, DeletePlan):
            return self._execute_delete(plan, context)
        raise TypeError(f"unsupported plan type: {type(plan).__name__}")

    def _execute_create_table(
        self, plan: CreateTablePlan, context: ExecutionContext
    ) -> QueryResult:
        # TEMPORARY: final CreateTable execution must reserve a table id through
        # CatalogManager, call StorageEngine.create_heap, then persist/register
        # the completed TableDef.  The current stand-ins cannot express that yet.
        context.catalog.register_table(plan.table)
        context.storage.create_table(plan.table)
        context.storage.sync()
        return QueryResult(affected_rows=0, message="CREATE TABLE OK")

    def _execute_insert(self, plan: InsertPlan, context: ExecutionContext) -> QueryResult:
        table = self._require_table(plan.table_name, context)
        context.storage.insert_row(table, plan.row)
        context.storage.sync()
        return QueryResult(affected_rows=1, message="1 row inserted")

    def _execute_seq_scan(
        self, plan: SeqScanPlan, context: ExecutionContext
    ) -> QueryResult:
        # TEMPORARY: the final storage scan yields StoredRow and must be closed
        # in a finally block.  This stub storage returns a materialized Row list.
        table = self._require_table(plan.table_name, context)
        rows = context.storage.scan_rows(table)
        return QueryResult(
            columns=[ResultColumn(column.name, column.data_type) for column in table.columns],
            rows=rows,
            affected_rows=None,
            message=f"{len(rows)} rows selected",
        )

    def _execute_filter(self, plan: FilterPlan, context: ExecutionContext) -> QueryResult:
        # TEMPORARY: predicates are Python callables until BoundExpr and the
        # shared expression type rules are supplied by Zhang Zhen.
        result = self.execute(plan.child, context)
        rows = [row for row in result.rows if plan.predicate(row)]
        return QueryResult(
            columns=result.columns,
            rows=rows,
            affected_rows=None,
            message=f"{len(rows)} rows selected",
        )

    def _execute_project(self, plan: ProjectPlan, context: ExecutionContext) -> QueryResult:
        result = self.execute(plan.child, context)
        positions = []
        output_columns = []
        for column_name in plan.columns:
            try:
                position = next(
                    index
                    for index, column in enumerate(result.columns)
                    if column.name == column_name
                )
            except StopIteration as exc:
                raise ValueError(
                    f"column not found in plan result: {column_name}"
                ) from exc
            positions.append(position)
            output_columns.append(result.columns[position])
        rows = [tuple(row[index] for index in positions) for row in result.rows]
        return QueryResult(
            columns=output_columns,
            rows=rows,
            affected_rows=None,
            message=f"{len(rows)} rows selected",
        )

    def _execute_delete(self, plan: DeletePlan, context: ExecutionContext) -> QueryResult:
        # TEMPORARY: final DELETE collects RowIds from a closed child scan,
        # calls delete_row for each, and only then reclaims empty pages.
        table = self._require_table(plan.table_name, context)
        deleted = context.storage.delete_rows(table, plan.predicate)
        context.storage.sync()
        return QueryResult(affected_rows=deleted, message=f"{deleted} rows deleted")

    @staticmethod
    def _require_table(name: str, context: ExecutionContext):
        table = context.catalog.find_table(name)
        if table is None:
            raise KeyError(f"table not found: {name}")
        return table
