"""v2 原始镜像与恢复故障；使用物理测试文件，不代表完整 SQL 事务。"""
import hashlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

from minidb.core import errors
from minidb.core.disk_types import PAGE_SIZE
from minidb.core.transaction import TransactionGuard, TransactionState as S
from minidb.storage.file_lock import DatabaseLock
from minidb.storage.file_manager import FileManager
from minidb.storage.buffer_pool import BufferPool
from minidb.storage import page_v2 as v2, file_snapshot
from tests.fakes.file_bytes import read_file_bytes

IDENTITY = UUID('00112233-4455-4677-8899-aabbccddeeff')


class ShortStream(io.BytesIO):
    def read(self, size=-1):
        return super().read(min(size, 113) if size >= 0 else 113)

    def write(self, data):
        return super().write(data[:127])


class FileSnapshotTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / 'image.db'
        self.path.write_bytes(v2.encode_file_header(v2.FileHeaderV2(IDENTITY, 4)) + bytes(2*PAGE_SIZE) + b'A'*PAGE_SIZE)
        lock = DatabaseLock.acquire(str(self.path))
        self.guard = TransactionGuard(S.IDLE)
        self.fm = FileManager.open_locked(str(self.path), lock, guard=self.guard)
        self.addCleanup(self.fm.close)

    def snapshot(self, stream=None):
        stream = io.BytesIO() if stream is None else stream
        self.guard.transition_to(S.PREPARING)
        info = self.fm.export_consistent_snapshot(stream)
        self.guard.transition_to(S.ACTIVE)
        return stream.getvalue(), info

    def assert_code(self, code, fn, *args, **kwargs):
        with self.assertRaises(errors.DbError) as caught:
            fn(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)

    def test_export_has_exact_bytes_digest_and_no_cache_requests(self):
        pool = BufferPool(self.fm, capacity=1)
        pool.get_page(3)
        stats = pool.stats()
        original = read_file_bytes(self.fm)
        raw, info = self.snapshot(ShortStream())
        self.assertEqual(raw, original)
        self.assertEqual(info.original_length, len(original))
        self.assertEqual(info.database_uuid, IDENTITY)
        self.assertEqual(info.payload_sha256, hashlib.sha256(original).digest())
        self.assertEqual(pool.stats(), stats)
        self.assertEqual(read_file_bytes(self.fm), original)

    def test_restore_truncates_and_invalidates_all_dirty_caches(self):
        for policy in ('lru', 'fifo'):
            with self.subTest(policy=policy):
                pools = [BufferPool(self.fm, capacity=1, policy=policy), BufferPool(self.fm, capacity=2, policy=policy)]
                snapshots = [p.get_snapshot(3) for p in pools]
                raw, info = self.snapshot()
                self.fm.allocate_page()
                self.fm.release_page(3)
                self.fm.allocate_page()
                self.fm.write_page(3, b'B'*PAGE_SIZE)
                for pool in pools:
                    pool.write_page(3, b'C'*PAGE_SIZE)
                stats = [p.stats() for p in pools]
                self.guard.transition_to(S.ROLLING_BACK)
                self.fm.restore_snapshot(ShortStream(raw), info.original_length, expected_sha256=info.payload_sha256)
                self.assertEqual(read_file_bytes(self.fm), raw)
                self.assertEqual(self.path.stat().st_size, len(raw))
                self.assertEqual(self.fm._free_pages, set())
                for pool, old, before in zip(pools, snapshots, stats):
                    self.assert_code(errors.STALE_PAGE, pool.write_if_current, old, b'X'*PAGE_SIZE)
                    self.assertEqual(pool.stats(), before)
                    self.assertFalse(pool.has_dirty_pages)
                    self.assertEqual(pool.get_page(3), b'A'*PAGE_SIZE)
                self.guard.transition_to(S.IDLE)

    def test_restore_restores_free_list_and_next_allocation(self):
        self.guard.transition_to(S.PREPARING)
        self.guard.transition_to(S.ACTIVE)
        self.fm.release_page(3)
        self.guard.transition_to(S.COMMITTING)
        self.guard.transition_to(S.IDLE)
        raw, info = self.snapshot()
        self.fm.allocate_page()
        self.fm.allocate_page()
        self.guard.transition_to(S.ROLLING_BACK)
        self.fm.restore_snapshot(io.BytesIO(raw), len(raw), expected_sha256=info.payload_sha256)
        self.assertEqual(self.fm._free_pages, {3})
        self.guard.transition_to(S.IDLE)
        self.guard.transition_to(S.PREPARING)
        self.guard.transition_to(S.ACTIVE)
        self.assertEqual(self.fm.allocate_page(), 3)

    def test_digest_identity_and_truncation_fail_before_file_or_cache_changes(self):
        raw, info = self.snapshot()
        pool = BufferPool(self.fm)
        pool.write_page(3, b'B'*PAGE_SIZE)
        before = read_file_bytes(self.fm)
        stats = pool.stats()
        self.guard.transition_to(S.ROLLING_BACK)
        corrupt = bytearray(raw)
        corrupt[-1] ^= 1
        foreign = v2.encode_file_header(v2.FileHeaderV2(UUID('00112233-4455-4677-8899-aabbccddee00'), 4)) + raw[PAGE_SIZE:]
        for data, digest in ((bytes(corrupt), info.payload_sha256), (raw[:-1], info.payload_sha256),
                             (foreign, hashlib.sha256(foreign).digest())):
            self.assert_code(errors.RECOVERY_FAILED, self.fm.restore_snapshot, io.BytesIO(data), len(raw), expected_sha256=digest)
            self.assertEqual(read_file_bytes(self.fm), before)
            self.assertTrue(pool.has_dirty_pages)
            self.assertEqual(pool.stats(), stats)

    def test_export_rejects_dirty_cache_without_writing_destination(self):
        pool = BufferPool(self.fm)
        self.guard.transition_to(S.PREPARING)
        self.guard.transition_to(S.ACTIVE)
        pool.write_page(3, b'B'*PAGE_SIZE)
        # 注入上一语句未清理的脏页状态，验证导出不会偷偷 flush。
        self.guard.transition_to(S.COMMITTING)
        self.guard.transition_to(S.IDLE)
        self.guard.transition_to(S.PREPARING)
        out = io.BytesIO()
        self.assert_code(errors.INVALID_TRANSACTION_STATE, self.fm.export_consistent_snapshot, out)
        self.assertEqual(out.getvalue(), b'')
        self.assertEqual(self.fm.read_page(3), b'A'*PAGE_SIZE)

    def test_state_and_alias_checks(self):
        self.assert_code(errors.INVALID_TRANSACTION_STATE, self.fm.export_consistent_snapshot, io.BytesIO())
        self.assert_code(errors.INVALID_TRANSACTION_STATE, self.fm.restore_snapshot, io.BytesIO(), 4*PAGE_SIZE)
        self.guard.transition_to(S.PREPARING)
        self.assert_code(errors.INVALID_ARGUMENT, self.fm.export_consistent_snapshot, self.fm._handle)
        self.guard.transition_to(S.ACTIVE)
        self.guard.transition_to(S.ROLLING_BACK)
        self.assert_code(errors.INVALID_ARGUMENT, self.fm.restore_snapshot, self.fm._handle, 4*PAGE_SIZE)

    def test_stream_offsets_preserve_journal_header_and_tail(self):
        stream = io.BytesIO(b'header')
        stream.seek(6)
        raw, info = self.snapshot(stream)
        self.assertEqual(raw[:6], b'header')
        source = io.BytesIO(raw+b'tail')
        source.seek(6)
        self.guard.transition_to(S.ROLLING_BACK)
        self.fm.restore_snapshot(source, info.original_length, expected_sha256=info.payload_sha256)
        self.assertEqual(source.read(), b'tail')

    def test_restore_write_failure_blocks_file_and_cache_access(self):
        raw, info = self.snapshot()
        pool = BufferPool(self.fm)
        pool.write_page(3, b'B'*PAGE_SIZE)
        self.guard.transition_to(S.ROLLING_BACK)
        original_write = file_snapshot.write_all
        def fail_main(destination, data):
            if destination is self.fm._handle:
                destination.write(data[:17])
                raise OSError('injected partial restore')
            return original_write(destination, data)
        with patch.object(file_snapshot, 'write_all', side_effect=fail_main):
            self.assert_code(errors.RECOVERY_FAILED, self.fm.restore_snapshot, io.BytesIO(raw), len(raw), expected_sha256=info.payload_sha256)
        self.assertEqual(self.guard.state, S.FAILED)
        self.assert_code(errors.RECOVERY_FAILED, self.fm.read_page, 3)
        self.assert_code(errors.RECOVERY_FAILED, pool.get_page, 3)
        self.assertFalse(pool.has_dirty_pages)

    def test_sync_failure_is_not_reported_as_success(self):
        raw, info = self.snapshot()
        self.guard.transition_to(S.ROLLING_BACK)
        failure = self.fm._error(errors.IO_SYNC_FAILED, 'sync')
        with patch.object(self.fm, 'sync', side_effect=failure):
            self.assert_code(errors.IO_SYNC_FAILED, self.fm.restore_snapshot, io.BytesIO(raw), len(raw), expected_sha256=info.payload_sha256)
        self.assertEqual(self.guard.state, S.FAILED)
        self.assert_code(errors.IO_SYNC_FAILED, self.fm.reload_metadata)

    def test_interrupt_after_restore_started_blocks_future_access(self):
        raw, info = self.snapshot()
        self.guard.transition_to(S.ROLLING_BACK)
        with patch.object(self.fm, 'sync', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.fm.restore_snapshot(io.BytesIO(raw), len(raw), expected_sha256=info.payload_sha256)
        self.assertEqual(self.guard.state, S.FAILED)
        self.assert_code(errors.RECOVERY_FAILED, self.fm.read_page, 3)

    def test_failed_output_and_source_reads_do_not_modify_main_file(self):
        class NoProgress(io.BytesIO):
            def write(self, data):
                return 0
        before = read_file_bytes(self.fm)
        self.guard.transition_to(S.PREPARING)
        self.assert_code(errors.IO_WRITE_FAILED, self.fm.export_consistent_snapshot, NoProgress())
        self.assertEqual(read_file_bytes(self.fm), before)
        self.guard.transition_to(S.ACTIVE)
        self.guard.transition_to(S.ROLLING_BACK)
        class BrokenRead(io.BytesIO):
            def read(self, count):
                raise OSError('injected read failure')
        self.assert_code(errors.IO_READ_FAILED, self.fm.restore_snapshot, BrokenRead(), len(before))
        self.assertEqual(read_file_bytes(self.fm), before)

    def test_large_snapshot_uses_spooled_file(self):
        # 物理测试文件扩展到超过暂存内存阈值；不走业务初始化。
        self.fm._handle.seek(0)
        self.fm._handle.write(v2.encode_file_header(v2.FileHeaderV2(IDENTITY, 300)))
        self.fm._handle.truncate(300*PAGE_SIZE)
        self.fm.reload_metadata()
        raw, info = self.snapshot()
        self.guard.transition_to(S.ROLLING_BACK)
        self.fm.restore_snapshot(io.BytesIO(raw), len(raw), expected_sha256=info.payload_sha256)
        self.assertEqual(hashlib.sha256(read_file_bytes(self.fm)).digest(), info.payload_sha256)


if __name__ == '__main__':
    unittest.main()
