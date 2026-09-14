"""IndexManager tree algorithms against the formal IndexPage contract."""

import random
import unittest

from minidb.core import errors
from minidb.compiler.bound import BoundLiteral
from minidb.compiler.plan import IndexScanPlan, ProjectPlan
from minidb.core.disk_types import PAGE_SIZE, PageSnapshot
from minidb.core.records import RowId, RowMovement, StoredRow
from minidb.core.result import ResultColumn
from minidb.core.schema import (
    ColumnDef,
    DataType,
    IndexBounds,
    IndexDef,
    IndexOrigin,
    Schema,
    TableDef,
    TableRef,
    TypeSpec,
)
from minidb.core.transaction import TransactionGuard, TransactionState
from minidb.core.source import SourcePos, SourceSpan
from minidb.engine.context import ExecutionContext
from minidb.engine.executor import Executor
from minidb.storage.index_manager import IndexManager
from minidb.storage.index_page import IndexKeyCodec, IndexPage, IndexPageType


class MemoryPages:
    """Small v2 page allocator; it enforces snapshot revisions used by writes."""

    def __init__(self):
        self.pages = {0: bytes(PAGE_SIZE), 1: bytes(PAGE_SIZE), 2: bytes(PAGE_SIZE)}
        self.revisions = {page_id: 1 for page_id in self.pages}
        self.next_page_id = 3
        self.free = set()
        self.writes = []

    def new_page(self):
        if self.free:
            page_id = min(self.free)
            self.free.remove(page_id)
        else:
            page_id = self.next_page_id
            self.next_page_id += 1
        self.pages[page_id] = bytes(PAGE_SIZE)
        self.revisions[page_id] = self.revisions.get(page_id, 0) + 1
        return page_id

    def get_snapshot(self, page_id):
        if page_id in self.free or page_id not in self.pages:
            raise KeyError(page_id)
        return PageSnapshot(page_id, self.pages[page_id], self.revisions[page_id])

    def write_if_current(self, snapshot, data):
        if snapshot.revision != self.revisions.get(snapshot.page_id):
            raise errors.DbError(errors.ErrorStage.STORAGE, errors.STALE_PAGE, "stale")
        if type(data) is not bytes or len(data) != PAGE_SIZE:
            raise ValueError("full immutable page required")
        self.pages[snapshot.page_id] = data
        self.revisions[snapshot.page_id] += 1
        self.writes.append(snapshot.page_id)

    def free_page(self, page_id):
        if page_id < 3 or page_id in self.free:
            raise ValueError("invalid free")
        self.pages[page_id] = bytes(PAGE_SIZE)
        self.revisions[page_id] += 1
        self.free.add(page_id)


class FakeStorage:
    def __init__(self, buffer_pool, guard):
        self.buffer_pool = buffer_pool
        self.guard = guard
        self.scans = set()
        self.rows = {}

    def register_external_scan(self, scan):
        self.scans.add(scan)

    def unregister_external_scan(self, scan):
        self.scans.discard(scan)

    def scan_rows(self, table):
        return iter(self.rows.values())

    def fetch_row(self, table, row_id):
        return self.rows[row_id]


class FakeCatalog:
    def __init__(self, storage, tables):
        self._storage = storage
        self.tables = tables
        self.indexes = ()

    def list_tables(self):
        return list(self.tables.values())

    def indexes_for_table(self, table_id):
        return tuple(index for index in self.indexes if index.table_id == table_id)


def bounds(lower=None, upper=None, *, lower_inclusive=True, upper_inclusive=True):
    return IndexBounds(
        lower is not None,
        lower,
        lower_inclusive if lower is not None else False,
        upper is not None,
        upper,
        upper_inclusive if upper is not None else False,
        False,
    )


class IndexManagerTests(unittest.TestCase):
    def setUp(self):
        self.pages = MemoryPages()
        self.guard = TransactionGuard(TransactionState.ACTIVE)
        self.table = TableDef(
            TableRef(1, "student", 100),
            Schema((ColumnDef("name", TypeSpec(DataType.VARCHAR, length=1024)),)),
        )
        self.tables = {1: self.table}
        self.storage = FakeStorage(self.pages, self.guard)
        self.catalog = FakeCatalog(self.storage, self.tables)
        self.manager = IndexManager(
            self.pages, self.storage, self.catalog, self.guard
        )
        self.span = SourceSpan(
            SourcePos(1, 1, 0), SourcePos(1, 2, 1), "index-test.sql"
        )
        anchor = self.manager.reserve_anchor()
        self.index = IndexDef(1, "name_idx", 1, 0, anchor, False, IndexOrigin.USER)

    @staticmethod
    def row(value, number):
        return StoredRow(RowId(100 + number // 60000, number % 60000, 1), (value,))

    def create(self, pairs=()):
        self.manager.create(self.index, tuple(pairs))

    def search(self, selected):
        cursor = self.manager.search(self.index, selected)
        try:
            return list(cursor)
        finally:
            cursor.close()

    def test_create_empty_tree_has_stable_anchor_and_empty_leaf(self):
        self.create()
        report = self.manager.validate(self.index)
        self.assertEqual((report.page_count, report.leaf_count, report.entry_count, report.height),
                         (2, 1, 0, 1))
        self.assertEqual(self.search(bounds()), [])

    def test_create_requires_prepared_sorted_unique_entries_without_writing(self):
        before = dict(self.pages.pages)
        duplicate = (("b", RowId(100, 2, 1)), ("a", RowId(100, 1, 1)))
        with self.assertRaises(errors.DbError) as caught:
            self.manager.create(self.index, duplicate)
        self.assertEqual(caught.exception.code, errors.INVALID_ARGUMENT)
        self.assertEqual(self.pages.pages, before)
        self.assertEqual(self.pages.writes, [])

    def test_exact_range_and_null_search_across_split_leaves(self):
        entries = [(None, RowId(100, 0, 1))]
        entries.extend((f"{number:03d}" + "x" * 390, RowId(100, number + 1, 1))
                       for number in range(40))
        self.create(entries)
        self.assertGreater(self.manager.validate(self.index).leaf_count, 1)

        nulls = IndexBounds(False, None, False, False, None, False, True)
        self.assertEqual(self.search(nulls), [RowId(100, 0, 1)])
        key = "020" + "x" * 390
        self.assertEqual(self.search(bounds(key, key)), [RowId(100, 21, 1)])
        lower = "010" + "x" * 390
        upper = "014" + "x" * 390
        self.assertEqual(self.search(bounds(lower, upper, lower_inclusive=False)),
                         [RowId(100, number + 1, 1) for number in range(11, 15)])

    def test_repeated_values_are_ordered_and_selected_by_row_id(self):
        entries = [("same", RowId(100, number, 1)) for number in range(20)]
        self.create(entries)
        self.assertEqual(self.search(bounds("same", "same")),
                         [RowId(100, number, 1) for number in range(20)])

    def test_apply_movements_handles_insert_in_place_update_and_migration(self):
        self.create()
        first = self.row("alpha", 1)
        self.manager.apply_movements((self.index,), (RowMovement(None, first),))
        self.assertEqual(self.search(bounds("alpha", "alpha")), [first.row_id])

        renamed = StoredRow(first.row_id, ("beta",))
        migrated = self.row("gamma", 2)
        self.manager.apply_movements(
            (self.index,),
            (RowMovement(first, renamed), RowMovement(None, migrated)),
        )
        self.assertEqual(self.search(bounds("alpha", "alpha")), [])
        self.assertEqual(self.search(bounds("beta", "gamma")),
                         [renamed.row_id, migrated.row_id])

        moved_again = StoredRow(RowId(101, 3, 2), migrated.values)
        self.manager.apply_movements((self.index,), (RowMovement(migrated, moved_again),))
        self.assertEqual(self.search(bounds("gamma", "gamma")), [moved_again.row_id])

    def test_active_cursor_blocks_writes_until_closed(self):
        self.create((("a", RowId(100, 1, 1)),))
        cursor = self.manager.search(self.index, bounds())
        self.assertIn(cursor, self.storage.scans)
        with self.assertRaises(errors.DbError) as caught:
            self.manager.apply_movements(
                (self.index,),
                (RowMovement(None, self.row("b", 2)),),
            )
        self.assertEqual(caught.exception.code, errors.ACTIVE_SCAN)
        cursor.close()
        self.assertNotIn(cursor, self.storage.scans)
        self.manager.apply_movements(
            (self.index,),
            (RowMovement(None, self.row("b", 2)),),
        )

    def test_writes_require_active_transaction_before_page_access(self):
        idle_pages = MemoryPages()
        idle_guard = TransactionGuard(TransactionState.IDLE)
        idle_storage = FakeStorage(idle_pages, idle_guard)
        idle_catalog = FakeCatalog(idle_storage, self.tables)
        manager = IndexManager(idle_pages, idle_storage, idle_catalog, idle_guard)
        with self.assertRaises(errors.DbError) as caught:
            manager.reserve_anchor()
        self.assertEqual(caught.exception.code, errors.INVALID_TRANSACTION_STATE)
        self.assertEqual(idle_pages.next_page_id, 3)

    def test_many_large_keys_create_multiple_levels_and_validate(self):
        entries = [(f"{number:03d}" + "z" * 500, RowId(100, number, 1))
                   for number in range(90)]
        self.create(entries)
        report = self.manager.validate(self.index)
        self.assertGreaterEqual(report.height, 3)
        self.assertEqual(report.entry_count, 90)
        self.assertEqual(self.search(bounds()), [row_id for _, row_id in entries])

    def test_deleting_every_entry_reclaims_pages_and_keeps_empty_root_leaf(self):
        stored = [self.row(f"{number:03d}" + "q" * 500, number) for number in range(60)]
        self.create(tuple((item.values[0], item.row_id) for item in stored))
        allocated_before = len(self.pages.pages) - len(self.pages.free)
        self.manager.apply_movements(
            (self.index,), tuple(RowMovement(item, None) for item in stored)
        )
        report = self.manager.validate(self.index)
        self.assertEqual((report.page_count, report.leaf_count, report.entry_count, report.height),
                         (2, 1, 0, 1))
        self.assertLess(len(self.pages.pages) - len(self.pages.free), allocated_before)

    def test_validate_detects_broken_leaf_chain(self):
        entries = [(f"{number:03d}" + "x" * 500, RowId(100, number, 1))
                   for number in range(20)]
        self.create(entries)
        codec = IndexKeyCodec(self.table.schema.columns[0].type_spec)
        anchor = IndexPage.decode(self.pages.pages[self.index.root_page_id], codec,
                                  page_id=self.index.root_page_id, expected_index_id=1)
        root = IndexPage.decode(self.pages.pages[anchor.left_child], codec,
                                page_id=anchor.left_child, expected_index_id=1)
        left_id = root.left_child
        left = IndexPage.decode(self.pages.pages[left_id], codec,
                                page_id=left_id, expected_index_id=1)
        damaged = IndexPage(left.page_type, left.index_id, left.parent_page_id,
                            right_sibling=0xFFFFFFFF, entries=left.entries)
        snapshot = self.pages.get_snapshot(left_id)
        self.pages.write_if_current(snapshot, damaged.encode(codec, page_id=left_id))
        with self.assertRaises(errors.DbError) as caught:
            self.manager.validate(self.index)
        self.assertEqual(caught.exception.code, errors.INDEX_CORRUPTED)

    def test_deterministic_random_insert_delete_matches_sorted_model(self):
        self.create()
        generator = random.Random(20260914)
        rows = [self.row(f"{generator.randrange(20):02d}" + "r" * 480, number)
                for number in range(100)]
        insertion_order = rows[:]
        generator.shuffle(insertion_order)
        for item in insertion_order:
            self.manager.apply_movements((self.index,), (RowMovement(None, item),))
        self.manager.validate(self.index)
        expected = sorted(rows, key=lambda item: (
            item.values[0], item.row_id.page_id, item.row_id.slot_id, item.row_id.generation
        ))
        self.assertEqual(self.search(bounds()), [item.row_id for item in expected])

        removed = set(generator.sample(range(100), 65))
        removal_order = [rows[index] for index in removed]
        generator.shuffle(removal_order)
        for item in removal_order:
            self.manager.apply_movements((self.index,), (RowMovement(item, None),))
        report = self.manager.validate(self.index)
        remaining = [item for index, item in enumerate(rows) if index not in removed]
        expected = sorted(remaining, key=lambda item: (
            item.values[0], item.row_id.page_id, item.row_id.slot_id, item.row_id.generation
        ))
        self.assertEqual(report.entry_count, len(expected))
        self.assertEqual(self.search(bounds()), [item.row_id for item in expected])

    def test_check_indexes_compares_complete_table_and_leaf_contents(self):
        stored = [self.row("alpha", 1), self.row("beta", 2)]
        self.create(tuple((item.values[0], item.row_id) for item in stored))
        self.catalog.indexes = (self.index,)
        self.storage.rows = {item.row_id: item for item in stored}
        reports = self.manager.check_indexes(self.table)
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0].entry_count, 2)

        self.storage.rows.pop(stored[1].row_id)
        with self.assertRaises(errors.DbError) as caught:
            self.manager.check_indexes(self.table)
        self.assertEqual(caught.exception.code, errors.INDEX_CORRUPTED)

    def test_executor_index_scan_fetches_rows_and_rejects_wrong_index_key(self):
        stored = [self.row("alpha", 1), self.row("beta", 2), self.row("gamma", 3)]
        self.create(tuple((item.values[0], item.row_id) for item in stored))
        self.catalog.indexes = (self.index,)
        self.storage.rows = {item.row_id: item for item in stored}
        literal = BoundLiteral(
            "beta", self.table.schema.columns[0].type_spec, self.span
        )
        scan = IndexScanPlan(
            self.table, self.index,
            True, literal, True,
            True, literal, True,
            False, self.span,
        )
        project = ProjectPlan(
            scan, (0,), (ResultColumn("name", DataType.VARCHAR),), self.span
        )
        context = ExecutionContext(
            self.catalog, self.storage, self.manager
        )
        cursor = Executor().execute_read(project, context)
        self.assertEqual(list(cursor), [("beta",)])
        self.assertFalse(self.storage.scans)

        self.storage.rows[stored[1].row_id] = StoredRow(
            stored[1].row_id, ("changed",)
        )
        cursor = Executor().execute_read(project, context)
        with self.assertRaises(errors.DbError) as caught:
            list(cursor)
        self.assertEqual(caught.exception.code, errors.INDEX_CORRUPTED)
        self.assertFalse(self.storage.scans)


if __name__ == "__main__":
    unittest.main()
