"""v2 物理文件路径；目录页是测试占位，不能冒充完整数据库初始化。"""
import tempfile
import unittest
from pathlib import Path
from uuid import UUID

from minidb.core import errors
from minidb.core.disk_types import PAGE_SIZE, V2_MAX_FILE_SIZE
from minidb.core.transaction import TransactionGuard, TransactionState as S
from minidb.storage.file_lock import DatabaseLock
from minidb.storage.file_manager import FileManager
from minidb.storage import page_v2 as v2, page
from tests.fakes.file_bytes import read_file_bytes

IDENTITY = UUID('00112233-4455-4677-8899-aabbccddeeff')


class FileManagerV2Tests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / 'v2.db'
        self.path.write_bytes(v2.encode_file_header(v2.FileHeaderV2(IDENTITY)) + bytes(2 * PAGE_SIZE))
        self.lock = DatabaseLock.acquire(str(self.path))
        self.addCleanup(self.lock.close)

    def open(self, state=S.ACTIVE):
        guard = TransactionGuard(state)
        fm = FileManager.open_locked(str(self.path), self.lock, guard=guard)
        self.addCleanup(fm.close)
        return fm, guard

    def assert_code(self, code, fn, *args, **kwargs):
        with self.assertRaises(errors.DbError) as caught:
            fn(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)

    def test_reuses_same_handle_and_preserves_uuid_on_allocate_release(self):
        fm, _ = self.open()
        self.assertIs(fm._handle, self.lock.handle)
        self.assertEqual(fm.database_uuid, IDENTITY)
        a, b = fm.allocate_page(), fm.allocate_page()
        self.assertEqual((a, b), (3, 4))
        fm.write_page(a, b'A' * PAGE_SIZE)
        fm.release_page(a)
        fm.release_page(b)
        self.assertEqual((fm.allocate_page(), fm.allocate_page()), (b, a))
        self.assertEqual(fm.read_page(a), bytes(PAGE_SIZE))
        fm.reload_metadata()
        self.assertEqual(fm.database_uuid, IDENTITY)
        fm.sync()
        fm.close()
        lock = DatabaseLock.acquire(str(self.path))
        self.addCleanup(lock.close)
        reopened = FileManager.open_locked(str(self.path), lock)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.database_uuid, IDENTITY)
        self.assertEqual(reopened.read_page(a), bytes(PAGE_SIZE))

    def test_three_reserved_pages_cannot_be_released(self):
        fm, _ = self.open()
        before = read_file_bytes(fm)
        for page_id in (0, 1, 2):
            self.assert_code(errors.RESERVED_PAGE, fm.release_page, page_id)
        self.assertEqual(read_file_bytes(fm), before)

    def test_missing_guard_is_read_only_and_cannot_be_replaced(self):
        fm = FileManager.open_locked(str(self.path), self.lock)
        self.addCleanup(fm.close)
        self.assertEqual(len(fm.read_page(0)), PAGE_SIZE)
        self.assert_code(errors.TRANSACTION_REQUIRED, fm.allocate_page)
        guard = TransactionGuard(S.IDLE)
        fm.bind_guard(guard)
        fm.bind_guard(guard)
        self.assert_code(errors.INVALID_ARGUMENT, fm.bind_guard, TransactionGuard(S.ACTIVE))
        guard.transition_to(S.PREPARING)
        guard.transition_to(S.ACTIVE)
        self.assertEqual(fm.allocate_page(), 3)

    def test_disallowed_states_reject_all_business_writes_without_change(self):
        fm, guard = self.open(S.IDLE)
        before = read_file_bytes(fm)
        for action, args in ((fm.allocate_page, ()), (fm.release_page, (2,)),
                             (fm.write_page, (1, b'X' * PAGE_SIZE))):
            self.assert_code(errors.INVALID_TRANSACTION_STATE, action, *args)
        guard.transition_to(S.PREPARING)
        self.assert_code(errors.INVALID_TRANSACTION_STATE, fm.allocate_page)
        guard.fail(operation='test')
        self.assert_code(errors.INVALID_TRANSACTION_STATE, fm.allocate_page)
        self.assertEqual(read_file_bytes(fm), before)

    def test_mismatched_path_and_duplicate_owner_rejected(self):
        self.assert_code(errors.INVALID_ARGUMENT, FileManager.open_locked,
                         str(self.path.with_name('other.db')), self.lock)
        fm, _ = self.open()
        self.assert_code(errors.INVALID_ARGUMENT, FileManager.open_locked, str(self.path), self.lock)
        self.assertFalse(fm._handle.closed)

    def test_v1_rejected_without_changing_or_closing_caller_lock(self):
        original = page.initial_file_header_page() + bytes(PAGE_SIZE)
        self.lock.handle.seek(0)
        self.lock.handle.write(original)
        self.lock.handle.truncate(len(original))
        self.assert_code(errors.FORMAT_VERSION_UNSUPPORTED, FileManager.open_locked,
                         str(self.path), self.lock)
        self.assertFalse(self.lock.handle.closed)
        self.lock.handle.seek(0)
        self.assertEqual(self.lock.handle.read(), original)

    def test_capacity_limit_precedes_growth_but_free_page_is_reusable(self):
        self.lock.handle.seek(0)
        self.lock.handle.write(v2.encode_file_header(v2.FileHeaderV2(IDENTITY, 16384)))
        self.lock.handle.truncate(V2_MAX_FILE_SIZE)
        fm, _ = self.open()
        before = fm.read_page(0)
        self.assert_code(errors.RESOURCE_LIMIT, fm.allocate_page)
        self.assertEqual(self.path.stat().st_size, V2_MAX_FILE_SIZE)
        self.assertEqual(fm.read_page(0), before)
        fm.release_page(16383)
        self.assertEqual(fm.allocate_page(), 16383)
        self.assertEqual(self.path.stat().st_size, V2_MAX_FILE_SIZE)

    def test_reload_rejects_changed_database_identity(self):
        fm, _ = self.open()
        old = fm._header
        other = UUID('00112233-4455-4677-8899-aabbccddee00')
        self.lock.handle.seek(0)
        self.lock.handle.write(v2.encode_file_header(v2.FileHeaderV2(other)))
        self.assert_code(errors.DB_FORMAT_MISMATCH, fm.reload_metadata)
        self.assertEqual(fm._header, old)

    def test_corrupt_free_chain_rejected_before_publication(self):
        self.lock.handle.seek(0)
        self.lock.handle.write(v2.encode_file_header(v2.FileHeaderV2(IDENTITY, 5, 3)))
        self.lock.handle.seek(3 * PAGE_SIZE)
        self.lock.handle.write(v2.encode_free_page(4, next_page_id=5))
        self.lock.handle.write(v2.encode_free_page(3, next_page_id=5))
        self.assert_code(errors.DB_FORMAT_MISMATCH, FileManager.open_locked, str(self.path), self.lock)
        self.assertFalse(self.lock.handle.closed)


if __name__ == '__main__':
    unittest.main()
