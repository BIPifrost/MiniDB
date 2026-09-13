"""真实文件上的旧页副本保护及 StorageEngine 交接验证。"""
from tests.fakes.file_bytes import read_file_bytes
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import patch

from minidb.core import errors
from minidb.core.disk_types import PAGE_SIZE, PageSnapshot
from minidb.storage.buffer_pool import BufferPool
from minidb.storage.file_manager import FileManager
from minidb.storage.data_page import DataPage
from minidb.cli.session import Session


class PageSnapshotTests(unittest.TestCase):
    def test_core_and_storage_imports_share_one_snapshot_type(self):
        from minidb.storage.buffer_pool import PageSnapshot as StorageSnapshot
        self.assertIs(StorageSnapshot, PageSnapshot)

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / 'snapshots.db'
        self.fm = FileManager.open(str(self.path))
        self.addCleanup(self.fm.close)
        self.a = self.fm.allocate_page()
        self.b = self.fm.allocate_page()
        self.pool = BufferPool(self.fm, capacity=1)

    def stale_without_side_effects(self, snapshot):
        stats = self.pool.stats()
        order = self.pool._replacement.snapshot()
        frames = {p: (f.data, f.dirty) for p, f in self.pool._frames.items()}
        disk = read_file_bytes(self.fm)
        with patch.object(self.fm, 'write_page', side_effect=AssertionError('unexpected I/O')):
            with self.assertRaises(errors.DbError) as caught:
                self.pool.write_if_current(snapshot, b'Z' * PAGE_SIZE)
        self.assertEqual(caught.exception.code, errors.STALE_PAGE)
        self.assertEqual(self.pool.stats(), stats)
        self.assertEqual(self.pool._replacement.snapshot(), order)
        self.assertEqual({p: (f.data, f.dirty) for p, f in self.pool._frames.items()}, frames)
        self.assertEqual(read_file_bytes(self.fm), disk)

    def test_snapshot_is_immutable_and_read_counts_once(self):
        snapshot = self.pool.get_snapshot(self.a)
        self.assertEqual(snapshot.data, bytes(PAGE_SIZE))
        self.assertEqual(self.pool.stats().requests, 1)
        with self.assertRaises(FrozenInstanceError):
            snapshot.revision = 2
        self.pool.write_if_current(snapshot, b'A' * PAGE_SIZE)
        self.assertEqual(self.pool.stats().requests, 1)
        self.assertEqual(self.pool.get_page(self.a), b'A' * PAGE_SIZE)

    def test_two_copies_cannot_overwrite_each_other(self):
        first = self.pool.get_snapshot(self.a)
        second = self.pool.get_snapshot(self.a)
        self.pool.write_if_current(first, b'A' * PAGE_SIZE)
        self.stale_without_side_effects(second)
        self.assertEqual(self.pool.get_page(self.a), b'A' * PAGE_SIZE)

    def test_unchanged_snapshot_survives_eviction(self):
        snapshot = self.pool.get_snapshot(self.a)
        self.pool.get_page(self.b)
        self.pool.write_if_current(snapshot, b'A' * PAGE_SIZE)
        self.pool.flush_all()
        self.assertEqual(self.fm.read_page(self.a), b'A' * PAGE_SIZE)

    def test_changed_snapshot_stays_stale_after_eviction(self):
        snapshot = self.pool.get_snapshot(self.a)
        self.pool.write_if_current(snapshot, b'A' * PAGE_SIZE)
        self.pool.write_page(self.b, b'B' * PAGE_SIZE)
        self.stale_without_side_effects(snapshot)
        self.assertEqual(self.fm.read_page(self.a), b'A' * PAGE_SIZE)

    def test_free_and_reuse_reject_old_identity(self):
        snapshot = self.pool.get_snapshot(self.a)
        self.pool.free_page(self.a)
        self.stale_without_side_effects(snapshot)
        self.assertEqual(self.pool.new_page(), self.a)
        fresh = self.pool.get_snapshot(self.a)
        self.assertGreater(fresh.revision, snapshot.revision)
        self.stale_without_side_effects(snapshot)
        self.pool.write_if_current(fresh, b'R' * PAGE_SIZE)

    def test_compatibility_write_invalidates_existing_snapshot(self):
        snapshot = self.pool.get_snapshot(self.a)
        self.pool.write_page(self.a, b'C' * PAGE_SIZE)
        self.stale_without_side_effects(snapshot)

    def test_flush_does_not_invalidate_snapshot(self):
        self.pool.write_page(self.a, b'A' * PAGE_SIZE)
        snapshot = self.pool.get_snapshot(self.a)
        self.pool.flush_all()
        self.pool.write_if_current(snapshot, b'B' * PAGE_SIZE)
        self.assertEqual(self.pool.get_page(self.a), b'B' * PAGE_SIZE)

    def test_invalidate_all_discards_dirty_data_without_writeback(self):
        self.pool.write_page(self.a, b'A' * PAGE_SIZE)
        snapshot = self.pool.get_snapshot(self.a)
        disk = read_file_bytes(self.fm)
        stats = self.pool.stats()
        self.pool.invalidate_all()
        self.assertEqual(read_file_bytes(self.fm), disk)
        self.assertEqual(self.pool.stats(), stats)
        self.stale_without_side_effects(snapshot)
        fresh = self.pool.get_snapshot(self.a)
        self.assertEqual(fresh.data, bytes(PAGE_SIZE))
        self.assertGreater(fresh.revision, snapshot.revision)
        self.stale_without_side_effects(snapshot)

    def test_foreign_and_manually_created_snapshots_rejected(self):
        other = BufferPool(self.fm)
        foreign = other.get_snapshot(self.a)
        own = self.pool.get_snapshot(self.a)
        self.stale_without_side_effects(foreign)
        self.stale_without_side_effects(PageSnapshot(own.page_id, own.data, own.revision))

    def test_invalid_write_preserves_revision_and_valid_snapshot(self):
        snapshot = self.pool.get_snapshot(self.a)
        for data in (b'short', bytearray(PAGE_SIZE), None):
            with self.assertRaises(errors.DbError) as caught:
                self.pool.write_if_current(snapshot, data)
            self.assertEqual(caught.exception.code, errors.INVALID_ARGUMENT)
        self.pool.write_if_current(snapshot, b'V' * PAGE_SIZE)

    def test_snapshot_api_rejects_invalid_snapshot_and_closed_pool(self):
        with self.assertRaises(errors.DbError) as caught:
            self.pool.write_if_current(None, bytes(PAGE_SIZE))
        self.assertEqual(caught.exception.code, errors.INVALID_ARGUMENT)
        snapshot = self.pool.get_snapshot(self.a)
        self.fm.close()
        with self.assertRaises(errors.DbError) as caught:
            self.pool.write_if_current(snapshot, bytes(PAGE_SIZE))
        self.assertEqual(caught.exception.code, errors.CLOSED)

    def test_failed_eviction_does_not_publish_new_version(self):
        snapshot = self.pool.get_snapshot(self.a)
        self.pool.write_page(self.b, b'B' * PAGE_SIZE)
        revision = dict(self.pool._versions._revisions)
        failure = errors.DbError(errors.ErrorStage.STORAGE, errors.IO_WRITE_FAILED, 'injected')
        with patch.object(self.fm, 'write_page', side_effect=failure):
            with self.assertRaises(errors.DbError) as caught:
                self.pool.write_if_current(snapshot, b'A' * PAGE_SIZE)
        self.assertIs(caught.exception, failure)
        self.assertEqual(self.pool._versions._revisions, revision)


class StorageSnapshotIntegrationTests(unittest.TestCase):
    def test_engine_submits_version_from_read_not_from_write_time(self):
        with tempfile.TemporaryDirectory() as directory:
            session = Session.open(str(Path(directory) / 'engine.db'), buffer_pages=1)
            try:
                session.execute_text('CREATE TABLE student(id INT);')
                table = session.catalog.find_table('student')
                buffer = session.storage._buffer
                original = DataPage.insert
                def competing_insert(page, encoded):
                    current = buffer.get_snapshot(page.page_id)
                    competitor = DataPage(current.data, page_id=page.page_id)
                    # 对手先提交一条不同记录，使原插入持有的版本过期。
                    original(competitor, (99).to_bytes(8, 'little', signed=True))
                    buffer.write_if_current(current, competitor.to_bytes())
                    return original(page, encoded)
                with patch.object(DataPage, 'insert', competing_insert):
                    with self.assertRaises(errors.DbError) as caught:
                        session.storage.insert_row(table, (1,))
                self.assertEqual(caught.exception.code, errors.STALE_PAGE)
                scan = session.storage.scan_rows(table)
                records = list(scan)
                self.assertEqual([row.values for row in records], [(99,)])
                self.assertEqual(session.storage.fetch_row(table, records[0].row_id), records[0])
            finally:
                session.abort()
