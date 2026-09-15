"""Executor prepare/apply contract tests independent of Session assembly."""

from types import SimpleNamespace
import unittest
from uuid import uuid4

from minidb.compiler.bound import BoundAssignment, BoundColumn
from minidb.compiler.plan import InsertPlan, SeqScanPlan, UpdatePlan
from minidb.core.records import (
    RowId,
    RowMovement,
    StoredRow,
    UpdateBatch,
    WriteKind,
    _issue_validated_write_token,
)
from minidb.core.schema import (
    ColumnDef,
    DataType,
    IndexDef,
    IndexOrigin,
    Schema,
    TableDef,
    TableRef,
    TypeSpec,
)
from minidb.core.source import SourcePos, SourceSpan
from minidb.engine.context import ExecutionContext
from minidb.engine.executor import Executor


SPAN = SourceSpan(SourcePos(1, 1, 0), SourcePos(1, 2, 1), "write.sql")
SCHEMA = Schema((
    ColumnDef("id", TypeSpec(DataType.INT)),
    ColumnDef("value", TypeSpec(DataType.INT)),
))
TABLE = TableDef(TableRef(1, "items", 3), SCHEMA)
INDEX = IndexDef(1, "ix_items_id", 1, 0, 4, False, IndexOrigin.USER)


class _Scan:
    def __init__(self, rows):
        self._rows = iter(rows)
        self.closed = False

    def __iter__(self):
        return self

    def __next__(self):
        return next(self._rows)

    def close(self):
        self.closed = True


class _Catalog:
    def __init__(self):
        self.generation = 7
        self.indexes = (INDEX,)

    def indexes_for_table(self, table_id):
        if table_id != TABLE.ref.table_id:
            raise AssertionError("unexpected table")
        return self.indexes


class _Storage:
    def __init__(self):
        self.rows = (StoredRow(RowId(3, 0), (1, 2)),)
        self.scans = []
        self.write_calls = []

    def scan_rows(self, table):
        if table != TABLE:
            raise AssertionError("unexpected table")
        scan = _Scan(self.rows)
        self.scans.append(scan)
        return scan

    def encoded_size(self, table, row):
        if table != TABLE:
            raise AssertionError("unexpected table")
        return len(row) * 8

    def insert_row(self, table, row, token):
        self.write_calls.append(("insert", table, row, token))
        return RowMovement(None, StoredRow(RowId(3, 1), row))

    def update_rows(self, table, batch, token):
        self.write_calls.append(("update", table, batch, token))
        return tuple(
            RowMovement(
                StoredRow(item.row_id, item.expected_old),
                StoredRow(item.row_id, item.new_row),
            )
            for item in batch.items
        )

    def delete_rows(self, table, rows, token):
        self.write_calls.append(("delete", table, rows, token))
        return tuple(RowMovement(row, None) for row in rows)


class _Validator:
    def __init__(self, session_id, catalog):
        self.session_id = session_id
        self.catalog = catalog
        self.calls = []

    def _token(self, prepared_id):
        return _issue_validated_write_token(
            self.session_id, self.catalog.generation, prepared_id
        )

    def validate_insert(self, table, lookup, row, prepared_id):
        self.calls.append(("insert", table, lookup, row, prepared_id))
        return SimpleNamespace(row=row, token=self._token(prepared_id))

    def validate_update(self, table, lookup, batch, prepared_id):
        self.calls.append(("update", table, lookup, batch, prepared_id))
        return SimpleNamespace(batch=batch, token=self._token(prepared_id))


class _Indexes:
    def __init__(self):
        self.applied = []

    def probe(self, index, key):
        return iter(())

    def apply_movements(self, indexes, movements):
        self.applied.append((indexes, movements))


class ExecutorPreparedWriteTests(unittest.TestCase):
    def setUp(self):
        self.session_id = uuid4()
        self.catalog = _Catalog()
        self.storage = _Storage()
        self.indexes = _Indexes()
        self.validator = _Validator(self.session_id, self.catalog)
        self.context = ExecutionContext(
            self.catalog,
            self.storage,
            self.indexes,
            self.validator,
            self.session_id,
        )
        self.executor = Executor()

    def test_prepare_insert_is_read_only_and_carries_current_indexes(self):
        prepared = self.executor.prepare_write(
            InsertPlan(TABLE, (1, 2), SPAN), self.context
        )

        self.assertIs(prepared.kind, WriteKind.INSERT)
        self.assertEqual(prepared.insert_row, (1, 2))
        self.assertEqual(prepared.affected_indexes, (INDEX,))
        self.assertEqual(prepared.encoded_candidate_bytes, 16)
        self.assertEqual(prepared.validation_token.prepared_id, prepared.prepared_id)
        self.assertEqual(self.storage.write_calls, [])
        self.assertEqual(self.validator.calls[0][0:4], (
            "insert", TABLE, self.indexes, (1, 2)
        ))

    def test_prepare_update_uses_one_old_row_for_all_assignments(self):
        plan = UpdatePlan(
            TABLE,
            SeqScanPlan(TABLE, SPAN),
            (
                BoundAssignment(
                    0, BoundColumn(1, TypeSpec(DataType.INT), SPAN, True), SPAN
                ),
                BoundAssignment(
                    1, BoundColumn(0, TypeSpec(DataType.INT), SPAN, True), SPAN
                ),
            ),
            SPAN,
        )

        prepared = self.executor.prepare_write(plan, self.context)

        self.assertIs(prepared.kind, WriteKind.UPDATE)
        self.assertEqual(prepared.updates[0].expected_old, (1, 2))
        self.assertEqual(prepared.updates[0].new_row, (2, 1))
        self.assertEqual(prepared.encoded_candidate_bytes, 32)
        self.assertTrue(self.storage.scans[0].closed)
        self.assertEqual(self.storage.write_calls, [])

    def test_apply_insert_passes_token_and_synchronizes_indexes(self):
        prepared = self.executor.prepare_write(
            InsertPlan(TABLE, (1, 2), SPAN), self.context
        )

        result = self.executor.apply_write(prepared, self.context)

        self.assertEqual(result.affected_rows, 1)
        self.assertEqual(self.storage.write_calls, [
            ("insert", TABLE, (1, 2), prepared.validation_token)
        ])
        movements = self.indexes.applied[0][1]
        self.assertEqual(self.indexes.applied[0][0], (INDEX,))
        self.assertEqual(movements[0].new.values, (1, 2))

    def test_apply_rejects_stale_or_cross_session_before_storage(self):
        prepared = self.executor.prepare_write(
            InsertPlan(TABLE, (1, 2), SPAN), self.context
        )

        self.catalog.generation += 1
        with self.assertRaisesRegex(RuntimeError, "old catalog"):
            self.executor.apply_write(prepared, self.context)
        self.catalog.generation -= 1
        self.context.session_id = uuid4()
        with self.assertRaisesRegex(RuntimeError, "another session"):
            self.executor.apply_write(prepared, self.context)
        self.assertEqual(self.storage.write_calls, [])

    def test_missing_index_manager_is_rejected_before_table_write(self):
        prepared = self.executor.prepare_write(
            InsertPlan(TABLE, (1, 2), SPAN), self.context
        )
        self.context.index_manager = None

        with self.assertRaisesRegex(RuntimeError, "IndexManager.apply_movements"):
            self.executor.apply_write(prepared, self.context)
        self.assertEqual(self.storage.write_calls, [])

    def test_apply_update_uses_formal_update_batch(self):
        plan = UpdatePlan(
            TABLE,
            SeqScanPlan(TABLE, SPAN),
            (
                BoundAssignment(
                    1, BoundColumn(0, TypeSpec(DataType.INT), SPAN, True), SPAN
                ),
            ),
            SPAN,
        )
        prepared = self.executor.prepare_write(plan, self.context)

        result = self.executor.apply_write(prepared, self.context)

        self.assertEqual(result.affected_rows, 1)
        self.assertIsInstance(self.storage.write_calls[0][2], UpdateBatch)
        self.assertEqual(self.storage.write_calls[0][2].items, prepared.updates)


if __name__ == "__main__":
    unittest.main()
