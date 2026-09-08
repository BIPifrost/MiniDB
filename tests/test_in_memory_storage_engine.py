import unittest

from minidb.core.records import RowId, RowScan, StoredRow
from tests.fakes.in_memory_storage_engine import InMemoryStorageEngine
from tests.fakes.schema import make_table


class InMemoryStorageEngineTests(unittest.TestCase):
    def setUp(self):
        self.storage = InMemoryStorageEngine()
        self.root_page_id = self.storage.create_heap(table_id=1)
        self.table = make_table(table_id=1, root_page_id=self.root_page_id)

    def test_create_insert_and_streaming_scan(self):
        first = self.storage.insert_row(self.table, (1, "Alice", 20))
        second = self.storage.insert_row(self.table, (2, "Bob", 17))

        scan = self.storage.scan_rows(self.table)

        self.assertIsInstance(scan, RowScan)
        self.assertEqual(
            list(scan),
            [
                StoredRow(first, (1, "Alice", 20)),
                StoredRow(second, (2, "Bob", 17)),
            ],
        )
        self.assertEqual(self.storage.active_scan_count, 0)

    def test_explicit_scan_close_is_idempotent(self):
        self.storage.insert_row(self.table, (1, "Alice", 20))
        scan = self.storage.scan_rows(self.table)

        scan.close()
        scan.close()

        self.assertEqual(self.storage.active_scan_count, 0)
        with self.assertRaises(StopIteration):
            next(scan)

    def test_active_scan_blocks_mutation_and_normal_close(self):
        row_id = self.storage.insert_row(self.table, (1, "Alice", 20))
        scan = self.storage.scan_rows(self.table)

        for operation in (
            lambda: self.storage.create_heap(2),
            lambda: self.storage.insert_row(self.table, (2, "Bob", 17)),
            lambda: self.storage.delete_row(self.table, row_id),
            lambda: self.storage.reclaim_empty_pages(self.table),
            self.storage.close,
        ):
            with self.assertRaisesRegex(RuntimeError, "ACTIVE_SCAN"):
                operation()

        scan.close()

    def test_delete_is_idempotent_and_scan_skips_deleted_rows(self):
        row_id = self.storage.insert_row(self.table, (1, "Alice", 20))

        self.assertTrue(self.storage.delete_row(self.table, row_id))
        self.assertFalse(self.storage.delete_row(self.table, row_id))
        self.assertEqual(list(self.storage.scan_rows(self.table)), [])

    def test_row_id_must_belong_to_target_table(self):
        self.storage.insert_row(self.table, (1, "Alice", 20))

        with self.assertRaises(ValueError):
            self.storage.delete_row(self.table, RowId(page_id=3, slot_id=0))
        with self.assertRaises(IndexError):
            self.storage.delete_row(
                self.table, RowId(page_id=self.root_page_id, slot_id=99)
            )

    def test_table_root_is_checked_on_every_table_operation(self):
        wrong_table = make_table(table_id=1, root_page_id=99)

        with self.assertRaises(ValueError):
            self.storage.validate_table_root(wrong_table)
        with self.assertRaises(ValueError):
            self.storage.insert_row(wrong_table, (1, "Alice", 20))

    def test_row_shape_and_exact_python_types_are_checked(self):
        with self.assertRaises(ValueError):
            self.storage.insert_row(self.table, (1, "Alice"))
        with self.assertRaises(TypeError):
            self.storage.insert_row(self.table, (True, "Alice", 20))
        with self.assertRaises(TypeError):
            self.storage.insert_row(self.table, (1, 123, 20))

    def test_reserved_catalog_heap_uses_table_zero_and_page_one(self):
        catalog = make_table(
            table_id=0,
            root_page_id=1,
            name="_sys_catalog",
            columns=(("table_id", "INT"),),
        )

        self.storage.initialize_reserved_heap(catalog)
        self.storage.validate_table_root(catalog)
        row_id = self.storage.insert_row(catalog, (1,))

        self.assertEqual(row_id, RowId(page_id=1, slot_id=0))

    def test_sync_close_and_abort_follow_lifecycle_contract(self):
        self.storage.sync()
        self.assertEqual(self.storage.sync_count, 1)

        self.storage.close()
        self.storage.close()
        self.assertTrue(self.storage.is_closed)
        self.assertEqual(self.storage.sync_count, 2)
        with self.assertRaisesRegex(RuntimeError, "CLOSED"):
            self.storage.scan_rows(self.table)

        aborted = InMemoryStorageEngine()
        aborted_root = aborted.create_heap(1)
        aborted_table = make_table(1, aborted_root)
        scan = aborted.scan_rows(aborted_table)
        aborted.abort()
        aborted.abort()

        self.assertTrue(aborted.is_closed)
        self.assertEqual(aborted.sync_count, 0)
        self.assertEqual(aborted.active_scan_count, 0)
        with self.assertRaises(StopIteration):
            next(scan)

    def test_reclaim_keeps_the_single_simulated_root_page(self):
        self.assertEqual(self.storage.reclaim_empty_pages(self.table), 0)


if __name__ == "__main__":
    unittest.main()
