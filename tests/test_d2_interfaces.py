"""D2 公共接口合同：页快照、事务状态和批量变更值对象。"""

import tempfile
import unittest
from pathlib import Path
import shutil

from minidb.compiler.bound import BoundAssignment, BoundColumn
from minidb.compiler.plan import SeqScanPlan, UpdatePlan, validate_plan
from minidb.core import errors
from minidb.core.records import (
    RowId,
    RowMovement,
    RowUpdate,
    StoredRow,
    UpdateBatch,
    WriteKind,
)
from minidb.core.schema import DataType
from minidb.core.transaction import TransactionGuard, TransactionState
from minidb.storage.buffer_pool import BufferPool, PageSnapshot
from minidb.storage.file_manager import FileManager
from tests.fixtures.contracts import STUDENT_TABLE, span


class D2InterfaceTests(unittest.TestCase):
    def test_page_snapshot_rejects_stale_write_without_side_effects(self):
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        file_manager = FileManager.open(str(Path(directory) / "snapshot.db"))
        self.addCleanup(file_manager.close)
        pool = BufferPool(file_manager, capacity=1)
        page_id = file_manager.allocate_page()
        file_manager.write_page(page_id, bytes(4096))

        snapshot = pool.get_snapshot(page_id)
        before = pool.stats()
        pool.write_page(page_id, b"x" * 4096)
        after_write = pool.stats()

        with self.assertRaises(errors.DbError) as raised:
            pool.write_if_current(snapshot, b"y" * 4096)
        self.assertEqual(raised.exception.code, errors.STALE_PAGE)
        self.assertEqual(pool.stats(), after_write)
        self.assertEqual(pool.get_page(page_id), b"x" * 4096)
        self.assertLessEqual(before.requests, after_write.requests)

    def test_invalidate_all_makes_snapshot_stale_without_flushing(self):
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        file_manager = FileManager.open(str(Path(directory) / "invalidate.db"))
        self.addCleanup(file_manager.close)
        pool = BufferPool(file_manager, capacity=1)
        page_id = file_manager.allocate_page()
        file_manager.write_page(page_id, bytes(4096))
        snapshot = pool.get_snapshot(page_id)
        pool.write_page(page_id, b"z" * 4096)
        pool.invalidate_all()

        with self.assertRaises(errors.DbError) as raised:
            pool.write_if_current(snapshot, b"q" * 4096)
        self.assertEqual(raised.exception.code, errors.STALE_PAGE)
        self.assertEqual(file_manager.read_page(page_id), bytes(4096))

    def test_transaction_guard_exposes_only_explicit_state_transitions(self):
        guard = TransactionGuard(TransactionState.IDLE)
        guard.transition(TransactionState.IDLE, TransactionState.ACTIVE, operation="begin")
        self.assertEqual(guard.state, TransactionState.ACTIVE)
        self.assertEqual(guard.generation, 1)
        with self.assertRaises(errors.DbError):
            guard.close()
        guard.transition(TransactionState.ACTIVE, TransactionState.COMMITTING, operation="commit")
        guard.transition(TransactionState.COMMITTING, TransactionState.IDLE, operation="finish")
        guard.close()
        self.assertEqual(guard.state, TransactionState.CLOSED)

    def test_row_movement_and_empty_update_batch_have_stable_shapes(self):
        old = RowId(2, 0, 1)
        new = RowId(3, 0, 1)
        self.assertEqual(RowMovement(WriteKind.UPDATE, old, new).new, new)
        self.assertEqual(RowMovement(WriteKind.INSERT, None, new).old, None)
        update = RowUpdate(StoredRow(old, (1, "Alice", 20)), (1, "Bob", 21))
        self.assertEqual(UpdateBatch((update,)).updates, (update,))
        self.assertEqual(UpdateBatch(()).updates, ())

    def test_update_plan_rejects_duplicate_assignment_columns(self):
        location = span("UPDATE student SET id = id;")
        assignment = BoundAssignment(
            0,
            BoundColumn(0, DataType.INT, location),
            location,
        )
        plan = UpdatePlan(
            STUDENT_TABLE,
            SeqScanPlan(STUDENT_TABLE, location),
            (assignment, assignment),
            location,
        )
        with self.assertRaises(errors.DbError) as raised:
            validate_plan(plan)
        self.assertEqual(raised.exception.code, errors.INVALID_PLAN)


if __name__ == "__main__":
    unittest.main()
