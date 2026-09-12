import unittest

from minidb.core.records import RowId, RowScan, RowSlot, StoredRow
from minidb.core.result import ExecRecord, QueryResult, ResultColumn
from minidb.engine.context import ExecutionContext
from minidb.storage.storage_engine import StorageEngine
from minidb.storage.data_page import DataPageHeader, RecordSlot, SlotState


class ListRowScan:
    def __init__(self, rows):
        self._rows = iter(rows)
        self.closed = False

    def __iter__(self):
        return self

    def __next__(self):
        if self.closed:
            raise StopIteration
        return next(self._rows)

    def close(self):
        self.closed = True


class ExecutionTypeTests(unittest.TestCase):
    def test_record_types_are_immutable_and_scan_is_structural(self):
        row_id = RowId(page_id=2, slot_id=0, generation=7)
        stored = StoredRow(row_id=row_id, values=(1, "Alice", 20))
        scan = ListRowScan([stored])

        self.assertIsInstance(scan, RowScan)
        self.assertEqual(next(scan), stored)
        scan.close()
        with self.assertRaises(StopIteration):
            next(scan)
        with self.assertRaises(AttributeError):
            row_id.slot_id = 1
        self.assertEqual(row_id.generation, 7)

    def test_exec_record_may_drop_row_id_after_projection(self):
        record = ExecRecord(values=(1, "Alice"), row_id=None)
        self.assertIsNone(record.row_id)

    def test_row_slot_captures_slot_and_generation(self):
        self.assertEqual(RowSlot(4, 9), RowSlot(slot_id=4, generation=9))

    def test_query_result_copies_lists_and_allows_duplicate_column_names(self):
        columns = [ResultColumn("id", "INT"), ResultColumn("id", "INT")]
        rows = [(1, 1)]

        result = QueryResult(columns, rows, None, "1 rows selected")
        columns.clear()
        rows.clear()

        self.assertEqual([column.name for column in result.columns], ["id", "id"])
        self.assertEqual(result.rows, [(1, 1)])

    def test_query_result_rejects_inconsistent_row_width(self):
        with self.assertRaises(ValueError):
            QueryResult([ResultColumn("id", "INT")], [(1, "extra")])

    def test_execution_context_uses_the_agreed_field_names(self):
        catalog = object()
        storage = object()
        context = ExecutionContext(catalog=catalog, storage=storage)

        self.assertIs(context.catalog, catalog)
        self.assertIs(context.storage, storage)

    def test_storage_engine_requires_its_three_runtime_dependencies(self):
        with self.assertRaises(TypeError):
            StorageEngine()

    def test_data_page_value_types_capture_header_and_slot_fields(self):
        header = DataPageHeader(
            table_id=1,
            next_page_id=0xFFFFFFFF,
            slot_count=1,
            free_start=47,
            free_end=4088,
            live_count=1,
        )
        slot = RecordSlot(offset=32, length=15, state=SlotState.LIVE, generation=1)

        self.assertEqual(header.live_count, 1)
        self.assertEqual(slot.state, SlotState.LIVE)
        self.assertEqual(slot.generation, 1)

    def test_data_page_value_types_reject_inconsistent_counts(self):
        with self.assertRaises(ValueError):
            DataPageHeader(
                table_id=1,
                next_page_id=0xFFFFFFFF,
                slot_count=0,
                free_start=32,
                free_end=4096,
                live_count=1,
            )


if __name__ == "__main__":
    unittest.main()
