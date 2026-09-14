"""日志字节合同、发布失败与提交结果未知；不冒充完整事务联调。"""
import hashlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

from minidb.core import errors
from minidb.core.disk_types import SnapshotInfo, PAGE_SIZE
from minidb.core.transaction import TransactionGuard, TransactionState as S
from minidb.storage import snapshot_journal as journal, page_v2
from minidb.storage.file_manager import FileManager
from minidb.storage.file_lock import DatabaseLock
from tests.fakes.file_bytes import read_file_bytes

DB = UUID('00112233-4455-4677-8899-aabbccddeeff')
TX = UUID('10213243-5465-4787-98a9-bacbdcedfe0f')
PAYLOAD = page_v2.encode_file_header(page_v2.FileHeaderV2(DB)) + bytes(2*PAGE_SIZE)


class JournalFormatTests(unittest.TestCase):
    def setUp(self):
        self.info = SnapshotInfo(len(PAYLOAD), DB, TX, hashlib.sha256(PAYLOAD).digest())
        self.header = journal.encode_header(self.info, 7)

    def assert_recovery_failed(self, data):
        with self.assertRaises(errors.DbError) as caught:
            journal.inspect_stream(io.BytesIO(data))
        self.assertEqual(caught.exception.code, errors.RECOVERY_FAILED)

    def test_header_offsets_against_independent_bytes(self):
        prefix = (b'MDB2SNAP' + bytes.fromhex('0100000080000000') + DB.bytes + TX.bytes
                  + bytes.fromhex('0030000000000000') + hashlib.sha256(PAYLOAD).digest()
                  + bytes.fromhex('0700000000000000'))
        self.assertEqual(self.header, prefix + hashlib.sha256(prefix).digest())
        self.assertEqual(len(self.header), 128)
        self.assertEqual(journal.decode_header(self.header), (self.info, 7))

    def test_tail_offsets_and_full_commit(self):
        tail = journal.encode_commit_tail(self.header, 4*PAGE_SIZE)
        prefix = b'MDB2DONE' + TX.bytes + bytes.fromhex('0040000000000000')
        self.assertEqual(tail, prefix + hashlib.sha256(self.header+prefix).digest())
        report = journal.inspect_stream(io.BytesIO(self.header+PAYLOAD+tail))
        self.assertTrue(report.committed)
        self.assertEqual(report.final_length, 4*PAGE_SIZE)

    def test_every_truncated_tail_requires_rollback(self):
        tail = journal.encode_commit_tail(self.header, len(PAYLOAD))
        for length in range(64):
            with self.subTest(length=length):
                report = journal.inspect_stream(io.BytesIO(self.header+PAYLOAD+tail[:length]))
                self.assertFalse(report.committed)
                self.assertIsNone(report.final_length)

    def test_bad_marker_or_hash_is_uncommitted(self):
        tail = journal.encode_commit_tail(self.header, len(PAYLOAD))
        for offset in (0, 32, 63):
            changed = bytearray(tail)
            changed[offset] ^= 1
            self.assertFalse(journal.inspect_stream(io.BytesIO(self.header+PAYLOAD+changed)).committed)

    def test_corrupt_header_payload_and_extra_tail_rejected(self):
        self.assert_recovery_failed(self.header[:-1])
        self.assert_recovery_failed(self.header + PAYLOAD[:-1])
        changed = bytearray(self.header)
        changed[56] ^= 1
        self.assert_recovery_failed(bytes(changed)+PAYLOAD)
        changed = bytearray(PAYLOAD)
        changed[-1] ^= 1
        self.assert_recovery_failed(self.header+changed)
        self.assert_recovery_failed(self.header+PAYLOAD+bytes(65))

    def test_uuid_mismatch_even_with_valid_hashes_is_rejected(self):
        other = SnapshotInfo(len(PAYLOAD), TX, TX, self.info.payload_sha256)
        self.assert_recovery_failed(journal.encode_header(other, 0)+PAYLOAD)
        prefix = b'MDB2DONE' + DB.bytes + len(PAYLOAD).to_bytes(8, 'little')
        tail = prefix + hashlib.sha256(self.header+prefix).digest()
        self.assert_recovery_failed(self.header+PAYLOAD+tail)


class JournalPublicationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name)/'journal.db'
        self.path.write_bytes(PAYLOAD)
        self.lock = DatabaseLock.acquire(str(self.path))
        self.guard = TransactionGuard(S.PREPARING)
        self.fm = FileManager.open_locked(str(self.path), self.lock, guard=self.guard)
        self.addCleanup(self.fm.close)
        self.formal = Path(self.lock.path+'.mdb2-journal')
        self.temporary = Path(self.lock.path+'.mdb2-journal.tmp')

    def test_prepare_and_commit_keep_main_bytes_and_log(self):
        info = journal.prepare(self.fm, TX, 7)
        self.assertFalse(self.temporary.exists())
        self.assertEqual(read_file_bytes(self.fm), PAYLOAD)
        report = journal.inspect_journal(self.lock)
        self.assertFalse(report.committed)
        self.assertEqual(report.snapshot, info)
        self.guard.transition_to(S.ACTIVE)
        self.fm.allocate_page()
        self.guard.transition_to(S.COMMITTING)
        report = journal.mark_committed(self.fm, TX)
        self.assertTrue(report.committed)
        self.assertEqual(report.final_length, 4*PAGE_SIZE)
        self.assertTrue(journal.inspect_journal(self.lock).committed)
        self.assertTrue(self.formal.exists())

    def test_tmp_only_never_becomes_active_and_is_not_overwritten(self):
        self.temporary.write_bytes(b'not ready')
        self.assertIsNone(journal.inspect_journal(self.lock))
        with self.assertRaises(errors.DbError):
            journal.prepare(self.fm, TX)
        self.assertEqual(self.temporary.read_bytes(), b'not ready')
        self.assertFalse(self.formal.exists())

    def test_formal_journal_never_overwritten(self):
        journal.prepare(self.fm, TX)
        before = self.formal.read_bytes()
        with self.assertRaises(errors.DbError):
            journal.prepare(self.fm, DB)
        self.assertEqual(self.formal.read_bytes(), before)

    def test_prepare_sync_failure_leaves_only_tmp_and_main_unchanged(self):
        with patch.object(journal, '_sync', side_effect=OSError('injected fsync failure')):
            with self.assertRaises(errors.DbError) as caught:
                journal.prepare(self.fm, TX)
        self.assertEqual(caught.exception.code, errors.IO_WRITE_FAILED)
        self.assertFalse(self.formal.exists())
        self.assertTrue(self.temporary.exists())
        self.assertEqual(read_file_bytes(self.fm), PAYLOAD)

    def test_commit_sync_failure_reports_unknown_and_preserves_evidence(self):
        journal.prepare(self.fm, TX)
        self.guard.transition_to(S.ACTIVE)
        self.guard.transition_to(S.COMMITTING)
        with patch.object(journal, '_sync', side_effect=OSError('injected commit sync failure')):
            with self.assertRaises(errors.DbError) as caught:
                journal.mark_committed(self.fm, TX)
        self.assertEqual(caught.exception.code, errors.COMMIT_OUTCOME_UNKNOWN)
        self.assertTrue(self.formal.exists())
        # 完整尾可能已经写入；返回未知不等于承诺回滚。
        self.assertTrue(journal.inspect_journal(self.lock).committed)

    def test_wrong_transaction_cannot_write_commit_marker(self):
        journal.prepare(self.fm, TX)
        before = self.formal.read_bytes()
        self.guard.transition_to(S.ACTIVE)
        self.guard.transition_to(S.COMMITTING)
        with self.assertRaises(errors.DbError):
            journal.mark_committed(self.fm, DB)
        self.assertEqual(self.formal.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
