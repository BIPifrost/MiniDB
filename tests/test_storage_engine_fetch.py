"""StorageEngine 定点读取的 generation 和页归属合同测试。"""

import unittest

from minidb.core import errors
from minidb.core.records import RowId, StoredRow
from minidb.storage.row_codec import RowCodec
from minidb.storage.storage_engine import StorageEngine
from tests.fakes.in_memory_buffer_pool import InMemoryBufferPool, InMemoryFileManager
from tests.fixtures.contracts import STUDENT_TABLE


class StorageEngineFetchTests(unittest.TestCase):
    def setUp(self):
        self.file_manager = InMemoryFileManager()
        self.buffer = InMemoryBufferPool(self.file_manager, capacity=2)
        self.storage = StorageEngine(self.buffer, RowCodec(), self.file_manager)
        self.storage.create_heap(STUDENT_TABLE.ref.table_id)

    def tearDown(self):
        if not self.storage.is_closed:
            self.storage.abort()

    def assert_error(self, code, action):
        with self.assertRaises(errors.DbError) as raised:
            action()
        self.assertEqual(raised.exception.code, code)

    def test_fetch_row_returns_decoded_row_with_original_identity(self):
        row = (1, "Alice", 20)
        row_id = self.storage.insert_row(STUDENT_TABLE, row)

        self.assertEqual(
            self.storage.fetch_row(STUDENT_TABLE, row_id),
            StoredRow(row_id, row),
        )
        self.assertEqual(self.storage.active_scan_count, 0)

    def test_fetch_row_rejects_deleted_row_and_wrong_page(self):
        row_id = self.storage.insert_row(STUDENT_TABLE, (1, "Alice", 20))
        self.assertTrue(self.storage.delete_row(STUDENT_TABLE, row_id))
        self.assert_error(
            errors.STALE_ROW,
            lambda: self.storage.fetch_row(STUDENT_TABLE, row_id),
        )
        self.assert_error(
            errors.INVALID_ARGUMENT,
            lambda: self.storage.fetch_row(STUDENT_TABLE, RowId(99, 0)),
        )

    def test_fetch_row_rejects_non_row_id_without_reading_pages(self):
        before = self.buffer.stats()
        self.assert_error(
            errors.INVALID_ARGUMENT,
            lambda: self.storage.fetch_row(STUDENT_TABLE, (2, 0)),
        )
        self.assertEqual(self.buffer.stats(), before)


if __name__ == "__main__":
    unittest.main()
