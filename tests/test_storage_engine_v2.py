"""StorageEngine 的 v2 装配、保留页和事务守卫合同。"""

import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from minidb.catalog.catalog import SYSTEM_CATALOG_TABLE, SYSTEM_INDEXES_TABLE
from minidb.catalog.catalog_manager import CatalogManager
from minidb.core import errors
from minidb.core.disk_types import PAGE_SIZE, V2_FORMAT_VERSION
from minidb.core.records import (
    RowMovement,
    RowUpdate,
    StoredRow,
    UpdateBatch,
    _issue_validated_write_token,
)
from minidb.core.schema import (
    ColumnDef,
    DataType,
    Schema,
    TableDef,
    TableRef,
    TypeSpec,
)
from minidb.core.transaction import TransactionGuard, TransactionState
from minidb.storage import page_v2
from minidb.storage.buffer_pool import BufferPool
from minidb.storage.data_page import DataPage
from minidb.storage.file_lock import DatabaseLock
from minidb.storage.file_manager import FileManager
from minidb.storage.row_codec import RowCodec
from minidb.storage.storage_engine import StorageEngine


class StorageEngineV2Tests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "storage-v2.db"
        image = (
            page_v2.encode_file_header(page_v2.FileHeaderV2(uuid4()))
            + bytes(2 * PAGE_SIZE)
        )
        self.path.write_bytes(image)
        lock = DatabaseLock.acquire(str(self.path))
        self.guard = TransactionGuard(TransactionState.BOOTSTRAP)
        self.file_manager = FileManager.open_locked(
            str(self.path), lock, guard=self.guard
        )
        self.pool = BufferPool(self.file_manager, capacity=2)
        self.storage = StorageEngine(
            self.pool, RowCodec(), self.file_manager, self.guard
        )
        self.session_id = uuid4()
        self.catalog_generation = 0
        self.authorized_tokens = []
        self.storage.bind_write_authorizer(
            session_id=self.session_id,
            catalog_generation=lambda: self.catalog_generation,
            token_is_authorized=lambda candidate: any(
                candidate is token for token in self.authorized_tokens
            ),
        )
        self.addCleanup(self._close)

    def _close(self):
        if not self.file_manager._closed:
            self.storage.abort()

    def _bind_catalog(self):
        self.storage.bind_catalog_services(
            write_catalog_rows=lambda table, rows: None,
            validate_index_root=lambda index, table: None,
        )

    def _enter_active(self):
        self.guard.transition_to(TransactionState.IDLE)
        self.guard.transition_to(TransactionState.PREPARING)
        self.guard.transition_to(TransactionState.ACTIVE)

    def _create_wide_table(self):
        self._enter_active()
        root_page_id = self.storage.create_heap(1)
        varchar = TypeSpec(DataType.VARCHAR, length=1024)
        return TableDef(
            TableRef(1, "student", root_page_id),
            Schema((
                ColumnDef("id", DataType.INT),
                ColumnDef("first", varchar),
                ColumnDef("second", varchar),
                ColumnDef("third", varchar),
            )),
        )

    def _token(self):
        token = _issue_validated_write_token(
            self.session_id, self.catalog_generation, uuid4()
        )
        self.authorized_tokens.append(token)
        return token

    def assert_code(self, code, action):
        with self.assertRaises(errors.DbError) as raised:
            action()
        self.assertEqual(raised.exception.code, code)

    def test_bootstrap_initializes_both_reserved_heaps_as_v2_pages(self):
        self._bind_catalog()
        catalog = CatalogManager.bootstrap_or_load(self.storage, True)

        self.assertEqual(catalog.generation, 0)
        for table in (SYSTEM_CATALOG_TABLE, SYSTEM_INDEXES_TABLE):
            page = DataPage(
                self.pool.get_page(table.ref.root_page_id),
                page_id=table.ref.root_page_id,
                expected_table_id=table.ref.table_id,
            )
            self.assertEqual(page.header.version, V2_FORMAT_VERSION)

    def test_user_heap_requires_active_and_starts_at_page_three(self):
        header_before = self.file_manager.read_page(0)
        stats_before = self.pool.stats()
        self.assert_code(
            errors.INVALID_TRANSACTION_STATE,
            lambda: self.storage.create_heap(1),
        )
        self.assertEqual(self.file_manager.read_page(0), header_before)
        self.assertEqual(self.pool.stats(), stats_before)

        self._enter_active()
        root_page_id = self.storage.create_heap(1)
        table = TableDef(
            TableRef(1, "student", root_page_id),
            Schema((ColumnDef("id", DataType.INT),)),
        )

        self.assertEqual(root_page_id, 3)
        self.storage.validate_table_root(table)
        page = DataPage(self.pool.get_page(root_page_id), page_id=root_page_id)
        self.assertEqual(page.header.version, V2_FORMAT_VERSION)

    def test_catalog_services_are_bound_once_to_the_same_guard_and_codec(self):
        with self.assertRaisesRegex(NotImplementedError, "Session"):
            _ = self.storage.catalog_services

        self._bind_catalog()
        services = self.storage.catalog_services
        self.assertIs(services.guard, self.guard)
        self.assertIs(services.codec, self.storage._codec)
        self.assertEqual(services.format_version(), V2_FORMAT_VERSION)
        self.assert_code(
            errors.INVALID_ARGUMENT,
            self._bind_catalog,
        )

    def test_constructor_rejects_a_different_file_guard(self):
        self.assert_code(
            errors.INVALID_ARGUMENT,
            lambda: StorageEngine(
                self.pool,
                RowCodec(),
                self.file_manager,
                TransactionGuard(TransactionState.BOOTSTRAP),
            ),
        )

    def test_batch_update_migrates_and_delete_reclaims_after_full_preflight(self):
        table = self._create_wide_table()
        first = (1, "a" * 600, "b" * 600, "c" * 600)
        second = (2, "d" * 600, "e" * 600, "f" * 600)
        expanded = (1, "x" * 1000, "y" * 1000, "z" * 1000)

        first_insert = self.storage.insert_row(table, first, self._token())
        second_insert = self.storage.insert_row(table, second, self._token())
        self.assertIsInstance(first_insert, RowMovement)
        self.assertIsNone(first_insert.old)
        self.assertEqual(first_insert.new.values, first)

        batch = UpdateBatch((
            RowUpdate(first_insert.new.row_id, first, expanded),
        ))
        movements = self.storage.update_rows(table, batch, self._token())

        self.assertEqual(len(movements), 1)
        movement = movements[0]
        self.assertEqual(movement.old, first_insert.new)
        self.assertEqual(movement.new.values, expanded)
        self.assertNotEqual(movement.new.row_id, movement.old.row_id)
        self.assertEqual(movement.new.row_id.page_id, 4)
        self.assertEqual(self.storage.fetch_row(table, movement.new.row_id), movement.new)
        self.assert_code(
            errors.STALE_ROW,
            lambda: self.storage.fetch_row(table, movement.old.row_id),
        )

        deleted = self.storage.delete_rows(
            table,
            (second_insert.new, movement.new),
            self._token(),
        )
        self.assertEqual(deleted, (
            RowMovement(second_insert.new, None),
            RowMovement(movement.new, None),
        ))
        self.assertEqual(list(self.storage.scan_rows(table)), [])
        root = DataPage(self.pool.get_page(3), page_id=3)
        self.assertEqual((root.header.slot_count, root.header.live_count), (0, 0))

    def test_update_and_delete_validate_every_expected_row_before_writing(self):
        table = self._create_wide_table()
        first = (1, "a", "b", "c")
        second = (2, "d", "e", "f")
        first_insert = self.storage.insert_row(table, first, self._token()).new
        second_insert = self.storage.insert_row(table, second, self._token()).new
        page_before = self.pool.get_page(3)
        header_before = self.file_manager._header

        bad_update = UpdateBatch((
            RowUpdate(first_insert.row_id, first, (1, "changed", "b", "c")),
            RowUpdate(second_insert.row_id, (999, "wrong", "row", "value"), second),
        ))
        self.assert_code(
            errors.STALE_ROW,
            lambda: self.storage.update_rows(table, bad_update, self._token()),
        )
        self.assertEqual(self.pool.get_page(3), page_before)
        self.assertEqual(self.file_manager._header, header_before)

        bad_delete = (
            first_insert,
            StoredRow(second_insert.row_id, (999, "wrong", "row", "value")),
        )
        self.assert_code(
            errors.STALE_ROW,
            lambda: self.storage.delete_rows(table, bad_delete, self._token()),
        )
        self.assertEqual(self.pool.get_page(3), page_before)
        self.assertEqual(self.file_manager._header, header_before)
        self.assertEqual(
            [record.values for record in self.storage.scan_rows(table)],
            [first, second],
        )

    def test_v2_row_writes_reject_missing_token_before_page_access(self):
        table = self._create_wide_table()
        stats_before = self.pool.stats()
        row = (1, "a", "b", "c")
        self.assert_code(
            errors.TRANSACTION_REQUIRED,
            lambda: self.storage.insert_row(table, row),
        )
        self.assertEqual(self.pool.stats(), stats_before)

    def test_token_identity_session_and_catalog_generation_are_rechecked(self):
        table = self._create_wide_table()
        row = (1, "a", "b", "c")
        prepared_id = uuid4()
        accepted = _issue_validated_write_token(
            self.session_id, self.catalog_generation, prepared_id
        )
        self.authorized_tokens.append(accepted)
        copied_fields = _issue_validated_write_token(
            self.session_id, self.catalog_generation, prepared_id
        )
        cross_session = _issue_validated_write_token(
            uuid4(), self.catalog_generation, uuid4()
        )
        stale_catalog = _issue_validated_write_token(
            self.session_id, self.catalog_generation + 1, uuid4()
        )
        self.authorized_tokens.extend((cross_session, stale_catalog))
        stats_before = self.pool.stats()

        for token in (copied_fields, cross_session, stale_catalog):
            with self.subTest(token=token):
                self.assert_code(
                    errors.INVALID_ARGUMENT,
                    lambda token=token: self.storage.insert_row(table, row, token),
                )
        self.assertEqual(self.pool.stats(), stats_before)

        movement = self.storage.insert_row(table, row, accepted)
        self.assertEqual(movement.new.values, row)
        self.authorized_tokens.remove(accepted)
        self.assert_code(
            errors.INVALID_ARGUMENT,
            lambda: self.storage.insert_row(table, row, accepted),
        )

    def test_migrated_update_survives_flush_close_and_reopen(self):
        table = self._create_wide_table()
        first = (1, "a" * 600, "b" * 600, "c" * 600)
        second = (2, "d" * 600, "e" * 600, "f" * 600)
        expanded = (1, "x" * 1000, "y" * 1000, "z" * 1000)
        first_insert = self.storage.insert_row(table, first, self._token()).new
        second_insert = self.storage.insert_row(table, second, self._token()).new
        movement = self.storage.update_rows(
            table,
            UpdateBatch((RowUpdate(first_insert.row_id, first, expanded),)),
            self._token(),
        )[0]

        self.guard.transition_to(TransactionState.COMMITTING)
        self.storage.flush_for_commit()
        self.file_manager.close()

        lock = DatabaseLock.acquire(str(self.path))
        reopened_guard = TransactionGuard(TransactionState.IDLE)
        reopened_file = FileManager.open_locked(
            str(self.path), lock, guard=reopened_guard
        )
        reopened_pool = BufferPool(reopened_file, capacity=1)
        reopened = StorageEngine(
            reopened_pool, RowCodec(), reopened_file, reopened_guard
        )
        self.addCleanup(reopened.abort)

        self.assertEqual(
            [row.values for row in reopened.scan_rows(table)],
            [second, expanded],
        )
        self.assertEqual(reopened.fetch_row(table, movement.new.row_id), movement.new)
        self.assert_code(
            errors.STALE_ROW,
            lambda: reopened.fetch_row(table, movement.old.row_id),
        )


if __name__ == "__main__":
    unittest.main()
