import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

from minidb.core import errors
from minidb.core.transaction import TransactionGuard, TransactionState as S
from minidb.core.disk_types import PAGE_SIZE
from minidb.storage import page_v2, snapshot_journal as journal
from minidb.storage.file_manager import FileManager
from minidb.storage.file_lock import DatabaseLock
from minidb.storage.buffer_pool import BufferPool
from minidb.storage.transaction import TransactionManager
from tests.fakes.transaction_participants import StorageParticipant, CatalogParticipant, IndexParticipant
from tests.fakes.file_bytes import read_file_bytes


class TransactionManagerTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name)/'txn.db'
        identity = UUID('00112233-4455-4677-8899-aabbccddeeff')
        self.original = page_v2.encode_file_header(page_v2.FileHeaderV2(identity, 4)) + bytes(2*PAGE_SIZE) + b'A'*PAGE_SIZE
        self.path.write_bytes(self.original)
        self.lock = DatabaseLock.acquire(str(self.path))
        self.guard = TransactionGuard(S.IDLE)
        self.fm = FileManager.open_locked(str(self.path), self.lock, guard=self.guard)
        self.addCleanup(self.fm.close)
        self.pool = BufferPool(self.fm, capacity=1)
        self.events = []
        self.storage = StorageParticipant(self.fm, self.pool, self.guard, self.events)
        self.catalog = CatalogParticipant(self.storage, self.events)
        self.indexes = IndexParticipant(self.pool, self.storage, self.catalog, self.guard, self.events)
        self.manager = self.construct()
        self.log = Path(self.lock.path+'.mdb2-journal')

    def construct(self):
        return TransactionManager(self.fm, self.pool, self.storage, self.catalog,
                                  self.indexes, self.lock, self.guard,
                                  invalidate_prepared=lambda: self.events.append('invalidate_prepared'))

    def assert_code(self, code, fn, *args):
        with self.assertRaises(errors.DbError) as caught:
            fn(*args)
        self.assertEqual(caught.exception.code, code)

    def test_commit_flushes_dirty_page_and_returns_after_durable_tail(self):
        txn = self.manager.begin_statement()
        self.pool.write_page(3, b'B'*PAGE_SIZE)
        with patch.object(journal, '_sync', wraps=journal._sync) as sync:
            result = self.manager.commit()
        self.assertTrue(result.committed)
        self.assertEqual(result.transaction_uuid, txn)
        self.assertEqual(sync.call_count, 1)
        self.assertIsNone(result.cleanup_warning)
        self.assertEqual(self.fm.read_page(3), b'B'*PAGE_SIZE)
        self.assertFalse(self.log.exists())
        self.assertEqual(self.manager.state, S.IDLE)

    def test_rollback_restores_growth_cache_and_participants_in_order(self):
        old = self.pool.get_snapshot(3)
        self.manager.begin_statement()
        self.pool.write_page(3, b'B'*PAGE_SIZE)
        self.pool.new_page()  # 容量 1，触发旧脏页真实落盘。
        self.manager.rollback()
        self.assertEqual(read_file_bytes(self.fm), self.original)
        self.assertEqual(self.events[-3:], ['catalog_reload', 'index_reload', 'invalidate_prepared'])
        self.assertEqual(self.catalog.generation, 1)
        self.assert_code(errors.STALE_PAGE, self.pool.write_if_current, old, b'X'*PAGE_SIZE)
        self.assertFalse(self.log.exists())
        self.assertEqual(self.manager.state, S.IDLE)

    def test_rejects_nested_begin_idle_rollback_and_duplicate_manager(self):
        self.assert_code(errors.INVALID_TRANSACTION_STATE, self.manager.rollback)
        self.assert_code(errors.INVALID_ARGUMENT, self.construct)
        self.manager.begin_statement()
        before = self.log.read_bytes()
        self.assert_code(errors.INVALID_TRANSACTION_STATE, self.manager.begin_statement)
        self.assertEqual(self.log.read_bytes(), before)

    def test_active_scan_rejects_begin_without_creating_log(self):
        self.storage.active_scan_count = 1
        self.assert_code(errors.ACTIVE_SCAN, self.manager.begin_statement)
        self.assertFalse(self.log.exists())
        self.assertEqual(self.manager.state, S.IDLE)

    def test_prepare_failure_closes_and_preserves_tmp(self):
        with patch.object(journal, '_sync', side_effect=OSError('prepare sync failed')):
            self.assert_code(errors.IO_WRITE_FAILED, self.manager.begin_statement)
        self.assertEqual(self.manager.state, S.CLOSED)
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertTrue(Path(str(self.log)+'.tmp').exists())

    def test_commit_flush_failure_is_unknown_and_does_not_delete_log(self):
        self.manager.begin_statement()
        self.pool.write_page(3, b'B'*PAGE_SIZE)
        failure = self.fm._error(errors.IO_WRITE_FAILED, 'injected_flush')
        with patch.object(self.fm, '_writeback_page', side_effect=failure):
            self.assert_code(errors.COMMIT_OUTCOME_UNKNOWN, self.manager.commit)
        self.assertEqual(self.manager.state, S.CLOSED)
        self.assertTrue(self.log.exists())

    def test_cleanup_failure_after_commit_returns_success_warning(self):
        self.manager.begin_statement()
        self.pool.write_page(3, b'B'*PAGE_SIZE)
        with patch.object(Path, 'unlink', side_effect=OSError('cleanup failed')):
            result = self.manager.commit()
        self.assertTrue(result.committed)
        self.assertIn('cleanup failed', result.cleanup_warning)
        self.assertEqual(self.manager.state, S.CLOSED)
        self.assertTrue(self.log.exists())
        with self.log.open('rb') as stream:
            self.assertTrue(journal.inspect_stream(stream).committed)

    def test_catalog_reload_failure_keeps_log_and_closes(self):
        self.manager.begin_statement()
        self.pool.write_page(3, b'B'*PAGE_SIZE)
        with patch.object(self.catalog, 'reload_from_storage', side_effect=RuntimeError('catalog failure')):
            with self.assertRaisesRegex(RuntimeError, 'catalog failure'):
                self.manager.rollback()
        self.assertTrue(self.log.exists())
        self.assertEqual(self.manager.state, S.CLOSED)
        self.assertEqual(self.path.read_bytes(), self.original)

    def test_business_cache_writes_rejected_during_idle_and_committing(self):
        self.assert_code(errors.INVALID_TRANSACTION_STATE, self.pool.write_page, 3, b'B'*PAGE_SIZE)
        self.manager.begin_statement()
        self.pool.write_page(3, b'B'*PAGE_SIZE)
        self.guard.transition_to(S.COMMITTING)
        self.assert_code(errors.INVALID_TRANSACTION_STATE, self.pool.write_page, 3, b'C'*PAGE_SIZE)
        self.pool.flush_all()
        self.assertEqual(self.fm.read_page(3), b'B'*PAGE_SIZE)

    def test_real_remote_catalog_participates_in_rollback(self):
        from dataclasses import replace
        from minidb.catalog.catalog import Catalog
        from minidb.catalog.catalog_manager import CatalogManager
        from tests.fakes.v2_catalog_contracts import CatalogStorage

        # 新建独立的物理对象链，避免在同一 FileManager 上替换事务管理者。
        path = self.path.with_name('real_catalog.db')
        path.write_bytes(self.original)
        lock = DatabaseLock.acquire(str(path))
        guard = TransactionGuard(S.IDLE)
        fm = FileManager.open_locked(str(path), lock, guard=guard)
        self.addCleanup(fm.close)
        pool = BufferPool(fm)
        events = []
        storage = StorageParticipant(fm, pool, guard, events)
        rows = CatalogStorage(state=S.IDLE)
        storage.catalog_services = replace(rows.catalog_services, guard=guard)
        storage.validate_table_root = rows.validate_table_root
        storage.scan_rows = rows.scan_rows
        catalog = CatalogManager(storage, Catalog())
        indexes = IndexParticipant(pool, storage, catalog, guard, events)
        manager = TransactionManager(fm, pool, storage, catalog, indexes, lock, guard,
                                     invalidate_prepared=lambda: events.append('invalidate_prepared'))
        manager.begin_statement()
        pool.write_page(3, b'B'*PAGE_SIZE)
        manager.rollback()
        self.assertEqual(catalog.generation, 1)
        self.assertEqual(catalog.list_tables(), [])
        self.assertTrue(all(scan.closed for scan in rows.scans))
        self.assertEqual(fm.read_page(3), b'A'*PAGE_SIZE)
        self.assertEqual(manager.state, S.IDLE)

    def test_close_active_rolls_back_and_is_idempotent(self):
        self.manager.begin_statement()
        self.pool.write_page(3, b'B'*PAGE_SIZE)
        self.manager.close()
        self.manager.close()
        self.assertEqual(self.manager.state, S.CLOSED)
        self.assertEqual(self.path.read_bytes(), self.original)


if __name__ == '__main__':
    unittest.main()
