import hashlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

from minidb.core import errors
from minidb.core.disk_types import PAGE_SIZE, SnapshotInfo
from minidb.storage import recovery, snapshot_journal as journal, page_v2
from minidb.storage.file_lock import DatabaseLock
from minidb.storage.file_manager import FileManager

DB = UUID('00112233-4455-4677-8899-aabbccddeeff')
TX = UUID('10213243-5465-4787-98a9-bacbdcedfe0f')
ORIGINAL = page_v2.encode_file_header(page_v2.FileHeaderV2(DB, 4)) + bytes(2*PAGE_SIZE) + b'A'*PAGE_SIZE


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name)/'recover.db'
        self.path.write_bytes(ORIGINAL)
        self.formal = Path(str(self.path)+'.mdb2-journal')
        self.temporary = Path(str(self.formal)+'.tmp')

    def lock(self):
        lock = DatabaseLock.acquire(str(self.path))
        self.addCleanup(lock.close)
        return lock

    def log(self, tail=b''):
        info = SnapshotInfo(len(ORIGINAL), DB, TX, hashlib.sha256(ORIGINAL).digest())
        header = journal.encode_header(info, 1)
        self.formal.write_bytes(header+ORIGINAL+tail)
        return header

    def read(self, lock):
        lock.handle.seek(0)
        return lock.handle.read()

    def recover(self, lock):
        return recovery.RecoveryManager.inspect_and_recover(str(self.path), lock)

    def test_tmp_only_is_removed_without_restoring_or_publishing(self):
        self.temporary.write_bytes(b'incomplete')
        lock = self.lock()
        report = self.recover(lock)
        self.assertEqual((report.action, report.restored_bytes), ('NONE', 0))
        self.assertEqual(self.read(lock), ORIGINAL)
        self.assertFalse(self.temporary.exists())
        self.assertFalse(self.formal.exists())

    def test_active_log_restores_corrupt_header_and_truncates(self):
        self.log()
        self.path.write_bytes(b'BROKEN!!'+bytes(5*PAGE_SIZE-8))
        lock = self.lock()
        report = self.recover(lock)
        self.assertEqual((report.action, report.restored_bytes, report.transaction_uuid), ('ROLLED_BACK', len(ORIGINAL), TX))
        self.assertEqual(self.read(lock), ORIGINAL)
        self.assertFalse(self.formal.exists())
        self.assertEqual(self.recover(lock).action, 'NONE')
        fm = FileManager.open_locked(str(self.path), lock)
        self.addCleanup(fm.close)
        self.assertEqual(fm.read_page(3), b'A'*PAGE_SIZE)

    def test_partial_commit_tail_rolls_back(self):
        header = self.log()
        self.log(journal.encode_commit_tail(header, len(ORIGINAL))[:31])
        self.path.write_bytes(ORIGINAL[:-PAGE_SIZE]+b'B'*PAGE_SIZE)
        lock = self.lock()
        self.assertEqual(self.recover(lock).action, 'ROLLED_BACK')
        self.assertEqual(self.read(lock), ORIGINAL)

    def test_committed_log_keeps_new_data(self):
        header = self.log()
        self.log(journal.encode_commit_tail(header, len(ORIGINAL)))
        changed = ORIGINAL[:-PAGE_SIZE]+b'B'*PAGE_SIZE
        self.path.write_bytes(changed)
        lock = self.lock()
        report = self.recover(lock)
        self.assertEqual((report.action, report.restored_bytes), ('COMMITTED_CLEANED', 0))
        self.assertEqual(self.read(lock), changed)
        self.assertFalse(self.formal.exists())

    def test_corrupt_payload_is_preserved_and_does_not_touch_main(self):
        self.log()
        raw = bytearray(self.formal.read_bytes())
        raw[-1] ^= 1
        self.formal.write_bytes(raw)
        lock = self.lock()
        with self.assertRaises(errors.DbError) as caught:
            self.recover(lock)
        self.assertEqual(caught.exception.code, errors.RECOVERY_FAILED)
        self.assertEqual(self.read(lock), ORIGINAL)
        self.assertEqual(self.formal.read_bytes(), raw)
        with self.assertRaises(errors.DbError):
            FileManager.open_locked(str(self.path), lock)

    def test_foreign_main_uuid_even_with_wrong_length_is_rejected(self):
        self.log()
        foreign = page_v2.encode_file_header(page_v2.FileHeaderV2(TX, 4))+bytes(4*PAGE_SIZE)
        self.path.write_bytes(foreign)
        lock = self.lock()
        with self.assertRaises(errors.DbError):
            self.recover(lock)
        self.assertEqual(self.read(lock), foreign)
        self.assertTrue(self.formal.exists())

    def test_committed_length_mismatch_preserves_both_files(self):
        header = self.log()
        self.log(journal.encode_commit_tail(header, 5*PAGE_SIZE))
        lock = self.lock()
        with self.assertRaises(errors.DbError):
            self.recover(lock)
        self.assertEqual(self.read(lock), ORIGINAL)
        self.assertTrue(self.formal.exists())

    def test_failed_restore_sync_can_be_retried(self):
        self.log()
        self.path.write_bytes(b'bad')
        lock = self.lock()
        with patch.object(recovery, '_sync', side_effect=OSError('sync failure')):
            with self.assertRaises(errors.DbError):
                self.recover(lock)
        self.assertTrue(self.formal.exists())
        with self.assertRaises(errors.DbError):
            FileManager.open_locked(str(self.path), lock)
        self.assertEqual(self.recover(lock).action, 'ROLLED_BACK')
        self.assertEqual(self.read(lock), ORIGINAL)

    def test_cleanup_failure_does_not_destroy_recovery_evidence(self):
        self.log()
        lock = self.lock()
        with patch.object(Path, 'unlink', side_effect=OSError('unlink failure')):
            with self.assertRaises(errors.DbError):
                self.recover(lock)
        self.assertTrue(self.formal.exists())
        self.assertEqual(self.recover(lock).action, 'ROLLED_BACK')

    def test_recovery_rejects_wrong_path_and_live_manager(self):
        lock = self.lock()
        with self.assertRaises(errors.DbError):
            recovery.RecoveryManager.inspect_and_recover(str(self.path.with_name('other.db')), lock)
        fm = FileManager.open_locked(str(self.path), lock)
        self.addCleanup(fm.close)
        with self.assertRaises(errors.DbError):
            self.recover(lock)

    def test_new_process_recovers_after_writer_exits_without_cleanup(self):
        code = '''
import os, sys
from uuid import uuid4
from minidb.storage.file_lock import DatabaseLock
from minidb.storage.file_manager import FileManager
from minidb.core.transaction import TransactionGuard, TransactionState as S
from minidb.storage import snapshot_journal
lock = DatabaseLock.acquire(sys.argv[1])
guard = TransactionGuard(S.PREPARING)
fm = FileManager.open_locked(sys.argv[1], lock, guard=guard)
snapshot_journal.prepare(fm, uuid4())
guard.transition_to(S.ACTIVE)
fm.write_page(3, b'B'*4096)
fm._handle.seek(0)
fm._handle.write(b'BROKEN!!')
os._exit(23)
'''
        result = subprocess.run([sys.executable, '-B', '-c', code, str(self.path)],
                                cwd=Path(__file__).resolve().parents[1], capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 23, result.stderr.decode(errors='replace'))
        lock = self.lock()
        self.assertEqual(self.recover(lock).action, 'ROLLED_BACK')
        self.assertEqual(self.read(lock), ORIGINAL)


if __name__ == '__main__':
    unittest.main()
