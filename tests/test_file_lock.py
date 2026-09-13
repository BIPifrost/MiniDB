"""Windows 真实句柄与独立进程验收，不用内存假锁代替。"""
import os
import queue
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from minidb.core import errors
from minidb.storage.file_manager import FileManager
from minidb.storage.file_lock import DatabaseLock, _open_exclusive
from minidb.cli.session import Session

ROOT = Path(__file__).resolve().parents[1]
ENV = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONIOENCODING': 'utf-8'}


@unittest.skipUnless(os.name == 'nt', 'Windows 独占实现')
class DatabaseLockTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / 'database.db'

    def open_db(self, path=None):
        manager = FileManager.open(str(path or self.path))
        self.addCleanup(manager.close)
        return manager

    def busy(self, path=None):
        with self.assertRaises(errors.DbError) as caught:
            FileManager.open(str(path or self.path))
        self.assertEqual(caught.exception.code, errors.DATABASE_BUSY)
        self.assertIn('lock_identity', caught.exception.context)

    def test_same_process_busy_and_reopen_after_close(self):
        first = self.open_db()
        before = first.read_page(0)
        self.busy()
        self.assertEqual(first.read_page(0), before)
        first.close()
        first.close()
        self.assertFalse(self.open_db().is_new)

    def test_case_and_relative_aliases(self):
        self.open_db()
        self.busy(str(self.path).upper())
        previous = Path.cwd()
        try:
            os.chdir(self.path.parent)
            self.busy(self.path.name)
        finally:
            os.chdir(previous)

    def test_hardlink_alias_uses_same_file_identity(self):
        initial = self.open_db()
        identity = initial._lock.lock_identity
        initial.close()
        alias = self.path.with_name('hardlink.db')
        os.link(self.path, alias)
        first = self.open_db()
        self.busy(alias)
        first.close()
        second = self.open_db(alias)
        self.assertEqual(second._lock.lock_identity, identity)

    def test_symlink_alias(self):
        self.open_db().close()
        alias = self.path.with_name('symlink.db')
        try:
            os.symlink(self.path, alias)
        except OSError as exc:
            if getattr(exc, 'winerror', None) == 1314:
                self.skipTest('当前账号无创建符号链接权限')
            raise
        self.open_db()
        self.busy(alias)

    def test_unrelated_databases_can_open_together(self):
        first = self.open_db()
        second = self.open_db(self.path.with_name('other.db'))
        self.assertNotEqual(first._lock.lock_identity, second._lock.lock_identity)
        self.assertEqual(first.allocate_page(), 2)
        self.assertEqual(second.allocate_page(), 2)

    def test_main_handle_is_reused_and_external_access_denied(self):
        manager = self.open_db()
        self.assertIs(manager._handle, manager._lock.handle)
        self.assertFalse(os.get_inheritable(manager._handle.fileno()))
        for mode in ('rb', 'r+b'):
            with self.assertRaises(PermissionError):
                with self.path.open(mode):
                    pass
        with self.assertRaises(PermissionError):
            self.path.unlink()

    def test_path_lock_blocks_creation_and_residual_carrier_is_harmless(self):
        carrier = str(self.path)+'.mdb2-lock'
        handle, _ = _open_exclusive(carrier)
        try:
            self.busy()
            self.assertFalse(self.path.exists())
        finally:
            handle.close()
        self.assertTrue(Path(carrier).exists())
        self.assertTrue(self.open_db().is_new)

    def test_bad_file_open_releases_both_locks(self):
        self.path.write_bytes(b'bad')
        for _ in range(2):
            with self.assertRaises(errors.DbError) as caught:
                FileManager.open(str(self.path))
            self.assertEqual(caught.exception.code, errors.DB_FILE_TRUNCATED)
        self.assertEqual(self.path.read_bytes(), b'bad')

    def test_main_open_failure_releases_path_lock(self):
        self.path.mkdir()
        for _ in range(2):
            with self.assertRaises(errors.DbError) as caught:
                FileManager.open(str(self.path))
            self.assertEqual(caught.exception.code, errors.IO_OPEN_FAILED)

    def test_close_failure_retains_lock_until_retry(self):
        manager = self.open_db()
        fake = Mock()
        fake.close.side_effect = OSError('injected')
        with patch.object(manager, '_handle', fake):
            with self.assertRaises(errors.DbError) as caught:
                manager.close()
            self.assertEqual(caught.exception.code, errors.IO_CLOSE_FAILED)
            self.busy()
        manager.close()
        self.open_db()

    def test_abort_releases_lock(self):
        session = Session.open(str(self.path))
        session.execute_text('CREATE TABLE student(id INT);')
        session.abort()
        second = Session.open(str(self.path))
        try:
            self.assertEqual(second.execute_text('SELECT * FROM student;')[0].rows, [])
        finally:
            second.close()

    def test_cli_busy_exit_one_and_then_succeeds(self):
        manager = Session.open(str(self.path))
        self.addCleanup(manager.abort)
        sql = self.path.with_suffix('.sql')
        sql.write_text('CREATE TABLE student(id INT);', encoding='utf-8')
        args = [sys.executable, '-B', '-m', 'minidb', '--db', str(self.path), '--file', str(sql)]
        failed = subprocess.run(args, cwd=ROOT, env=ENV, capture_output=True, text=True, encoding='utf-8', timeout=15)
        self.assertEqual(failed.returncode, 1)
        self.assertIn('DATABASE_BUSY', failed.stderr)
        self.assertEqual(failed.stdout, '')
        manager.close()
        succeeded = subprocess.run(args, cwd=ROOT, env=ENV, capture_output=True, text=True, encoding='utf-8', timeout=15)
        self.assertEqual(succeeded.returncode, 0, succeeded.stderr)

    def test_process_termination_releases_kernel_lock(self):
        code = '''import sys
from minidb.storage.file_manager import FileManager
fm=FileManager.open(sys.argv[1])
fm.sync()
print('READY', flush=True)
sys.stdin.readline()
'''
        process = subprocess.Popen([sys.executable, '-B', '-c', code, str(self.path)], cwd=ROOT,
            env=ENV, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8')
        try:
            ready = queue.Queue()
            threading.Thread(target=lambda: ready.put(process.stdout.readline()), daemon=True).start()
            self.assertEqual(ready.get(timeout=15).strip(), 'READY')
            self.busy()
            process.kill()
            process.wait(timeout=15)
            self.assertFalse(self.open_db().is_new)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=15)
            for stream in (process.stdin, process.stdout, process.stderr):
                stream.close()
