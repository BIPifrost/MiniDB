import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from minidb.core import errors
from minidb.core.transaction import TransactionGuard, TransactionState as S
from minidb.storage.file_manager import FileManager
from minidb.storage.file_lock import DatabaseLock
from minidb.storage import database_creation as creation
from minidb.storage.buffer_pool import BufferPool
from minidb.storage.row_codec import RowCodec
from minidb.storage.storage_engine import StorageEngine
from minidb.catalog.catalog import SYSTEM_CATALOG_TABLE, SYSTEM_INDEXES_TABLE
from minidb.catalog.catalog_manager import CatalogManager


class DatabaseCreationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/'new.db'

    def assert_clean_failure(self):
        self.assertFalse(self.path.exists())
        self.assertEqual(list(self.path.parent.glob('*.creating-*')), [])
        # 失败后可以重新创建，证明正式路径锁已经释放。
        FileManager.create_v2(str(self.path))

    def test_create_reopen_and_load_real_empty_catalog(self):
        identity = FileManager.create_v2(str(self.path))
        self.assertEqual(self.path.stat().st_size, 3*4096)
        self.assertEqual(identity.version, 4)
        guard = TransactionGuard(S.READ_ONLY_STARTUP)
        fm = FileManager.open_locked(str(self.path), DatabaseLock.acquire(str(self.path)), guard=guard)
        try:
            self.assertFalse(fm.is_new)
            self.assertEqual(fm.database_uuid, identity)
            pool = BufferPool(fm)
            storage = StorageEngine(pool, RowCodec(), fm, guard)
            for table in (SYSTEM_CATALOG_TABLE, SYSTEM_INDEXES_TABLE):
                storage.validate_table_root(table)
            def unexpected(*args):
                raise AssertionError('空目录加载不应写目录或验证索引根')
            storage.bind_catalog_services(write_catalog_rows=unexpected, validate_index_root=unexpected)
            catalog = CatalogManager.bootstrap_or_load(storage, fm.is_new)
            self.assertEqual(catalog.list_tables(), [])
            self.assertEqual(catalog.indexes_for_table(1), ())
            self.assertIsNone(catalog.find_index('missing'))
        finally:
            fm.close()

    def test_existing_files_untouched(self):
        for contents in (b'', b'old database', bytes(12288)):
            self.path.write_bytes(contents)
            with self.assertRaises(errors.DbError):
                FileManager.create_v2(str(self.path))
            self.assertEqual(self.path.read_bytes(), contents)

    def test_existing_valid_database_untouched(self):
        FileManager.create_v2(str(self.path))
        contents = self.path.read_bytes()
        with self.assertRaises(errors.DbError):
            FileManager.create_v2(str(self.path))
        self.assertEqual(self.path.read_bytes(), contents)

    def test_path_lock_conflict(self):
        lock, _ = creation._open_exclusive(str(self.path)+'.mdb2-lock')
        try:
            with self.assertRaises(errors.DbError) as caught:
                FileManager.create_v2(str(self.path))
            self.assertEqual(caught.exception.code, errors.DATABASE_BUSY)
            self.assertFalse(self.path.exists())
        finally:
            lock.close()

    def test_recovery_evidence_prevents_creation(self):
        for suffix in ('.mdb2-journal', '.mdb2-journal.tmp'):
            journal = Path(str(self.path)+suffix)
            journal.write_bytes(b'evidence')
            with self.assertRaises(errors.DbError) as caught:
                FileManager.create_v2(str(self.path))
            self.assertEqual(caught.exception.code, errors.RECOVERY_FAILED)
            self.assertFalse(self.path.exists())
            self.assertEqual(journal.read_bytes(), b'evidence')
            journal.unlink()

    def test_initial_header_write_failure(self):
        with patch.object(creation, 'write_all', side_effect=OSError('header write')):
            with self.assertRaises(errors.DbError):
                FileManager.create_v2(str(self.path))
        self.assert_clean_failure()

    def test_second_directory_initialization_failure(self):
        real = StorageEngine.initialize_reserved_heap
        def fail_second(storage, table):
            if table.ref.root_page_id == 2:
                raise RuntimeError('second root')
            return real(storage, table)
        with patch.object(StorageEngine, 'initialize_reserved_heap', fail_second):
            with self.assertRaises(RuntimeError):
                FileManager.create_v2(str(self.path))
        self.assert_clean_failure()

    def test_sync_failure_never_publishes(self):
        with patch.object(FileManager, 'sync', side_effect=OSError('sync')):
            with self.assertRaises(errors.DbError):
                FileManager.create_v2(str(self.path))
        self.assert_clean_failure()

    def test_publish_failure(self):
        with patch.object(creation.os, 'rename', side_effect=OSError('rename')):
            with self.assertRaises(errors.DbError):
                FileManager.create_v2(str(self.path))
        self.assert_clean_failure()

    def test_destination_appears_before_publish_is_not_overwritten(self):
        rename = os.rename
        def race(source, target):
            self.path.write_bytes(b'other file')
            return rename(source, target)
        with patch.object(creation.os, 'rename', side_effect=race):
            with self.assertRaises(errors.DbError):
                FileManager.create_v2(str(self.path))
        self.assertEqual(self.path.read_bytes(), b'other file')
        self.assertEqual(list(self.path.parent.glob('*.creating-*')), [])

    def test_main_path_absent_during_initialization(self):
        real = StorageEngine.initialize_reserved_heap
        def check(storage, table):
            self.assertFalse(self.path.exists())
            return real(storage, table)
        with patch.object(StorageEngine, 'initialize_reserved_heap', check):
            FileManager.create_v2(str(self.path))

    def test_invalid_path(self):
        for path in ('', None, 'a\0b'):
            with self.assertRaises(errors.DbError) as caught:
                FileManager.create_v2(path)
            self.assertEqual(caught.exception.code, errors.INVALID_ARGUMENT)

    def test_process_exit_before_and_after_publication(self):
        import subprocess
        import sys
        script = '''
import os, sys
from minidb.storage.file_manager import FileManager
from minidb.storage import database_creation as c
real = os.rename
def terminate(source, target):
    if sys.argv[2] == 'after':
        real(source, target)
    os._exit(23)
c.os.rename = terminate
FileManager.create_v2(sys.argv[1])
'''
        for phase in ('before', 'after'):
            path = self.path.with_name(phase+'.db')
            with self.subTest(phase=phase):
                result = subprocess.run([sys.executable, '-c', script, str(path), phase],
                                        capture_output=True, timeout=15)
                self.assertEqual(result.returncode, 23, result.stderr)
                if phase == 'before':
                    self.assertFalse(path.exists())
                    # 遗留临时文件不自动发布，新一次创建使用自己的随机文件。
                    FileManager.create_v2(str(path))
                fm = FileManager.open_locked(str(path), DatabaseLock.acquire(str(path)))
                try:
                    self.assertEqual(path.stat().st_size, 12288)
                    self.assertEqual(fm.database_uuid.version, 4)
                finally:
                    fm.close()


if __name__ == '__main__':
    unittest.main()
