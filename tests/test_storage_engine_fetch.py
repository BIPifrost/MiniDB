"""StorageEngine 定点读取的 generation 和页归属合同测试。"""

import unittest
from uuid import uuid4

from minidb.catalog.catalog import SYSTEM_CATALOG_TABLE, SYSTEM_INDEXES_TABLE
from minidb.core import errors
from minidb.core.records import RowId, StoredRow, _issue_validated_write_token
from minidb.core.transaction import TransactionGuard, TransactionState
from minidb.storage.row_codec import RowCodec
from minidb.storage.storage_engine import StorageEngine
from tests.fakes.in_memory_buffer_pool import InMemoryBufferPool, InMemoryFileManager
from tests.fixtures.contracts import STUDENT_TABLE


class StorageEngineFetchTests(unittest.TestCase):
    def setUp(self):
        self.guard = TransactionGuard(TransactionState.BOOTSTRAP)
        self.file_manager = InMemoryFileManager(guard=self.guard)
        self.buffer = InMemoryBufferPool(self.file_manager, capacity=2)
        self.storage = StorageEngine(
            self.buffer, RowCodec(), self.file_manager, self.guard
        )
        self.session_id = uuid4()
        self.authorized_tokens = []
        self.storage.bind_write_authorizer(
            session_id=self.session_id,
            catalog_generation=lambda: 0,
            token_is_authorized=lambda candidate: any(
                candidate is token for token in self.authorized_tokens
            ),
        )
        self.storage.initialize_reserved_heap(SYSTEM_CATALOG_TABLE)
        self.storage.initialize_reserved_heap(SYSTEM_INDEXES_TABLE)
        self.guard.transition_to(TransactionState.IDLE)
        self.guard.transition_to(TransactionState.PREPARING)
        self.guard.transition_to(TransactionState.ACTIVE)
        self.assertEqual(
            self.storage.create_heap(STUDENT_TABLE.ref.table_id),
            STUDENT_TABLE.ref.root_page_id,
        )
        self.table = STUDENT_TABLE

    def tearDown(self):
        if not self.storage.is_closed:
            self.storage.abort()

    def assert_error(self, code, action):
        with self.assertRaises(errors.DbError) as raised:
            action()
        self.assertEqual(raised.exception.code, code)

    def insert(self, row):
        token = _issue_validated_write_token(self.session_id, 0, uuid4())
        self.authorized_tokens.append(token)
        try:
            return self.storage.insert_row(self.table, row, token).new.row_id
        finally:
            self.authorized_tokens.remove(token)

    def delete(self, row_id):
        expected = self.storage.fetch_row(self.table, row_id)
        token = _issue_validated_write_token(self.session_id, 0, uuid4())
        self.authorized_tokens.append(token)
        try:
            return self.storage.delete_rows(self.table, (expected,), token)
        finally:
            self.authorized_tokens.remove(token)

    def test_fetch_row_returns_decoded_row_with_original_identity(self):
        row = (1, "Alice", 20)
        row_id = self.insert(row)

        self.assertEqual(
            self.storage.fetch_row(self.table, row_id),
            StoredRow(row_id, row),
        )
        self.assertEqual(self.storage.active_scan_count, 0)

    def test_fetch_row_rejects_deleted_row_and_wrong_page(self):
        row_id = self.insert((1, "Alice", 20))
        self.assertTrue(self.delete(row_id))
        self.assert_error(
            errors.STALE_ROW,
            lambda: self.storage.fetch_row(self.table, row_id),
        )
        self.assert_error(
            errors.INVALID_ARGUMENT,
            lambda: self.storage.fetch_row(self.table, RowId(99, 0)),
        )

    def test_fetch_row_rejects_non_row_id_without_reading_pages(self):
        before = self.buffer.stats()
        self.assert_error(
            errors.INVALID_ARGUMENT,
            lambda: self.storage.fetch_row(self.table, (2, 0)),
        )
        self.assertEqual(self.buffer.stats(), before)


if __name__ == "__main__":
    unittest.main()
