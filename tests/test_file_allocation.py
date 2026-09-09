"""页分配、回收、复用及独立进程重开测试；仅操作临时数据库。"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from minidb.core import errors
from minidb.core.disk_types import PAGE_SIZE, MAX_PAGE_ID, INVALID_PAGE_ID
from minidb.storage.file_manager import FileManager
from minidb.storage.page import FileHeader, decode_file_header, encode_free_page


class FileAllocationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'allocation.db'
        self.fm = FileManager.open(str(self.path))
        self.addCleanup(self.fm.close)

    def header(self):
        return decode_file_header(self.fm.read_page(0), file_size=self.path.stat().st_size)

    def assert_code(self, code, fn, *args):
        with self.assertRaises(errors.DbError) as caught:
            fn(*args)
        self.assertEqual(caught.exception.code, code)
        self.assertIs(caught.exception.stage, errors.ErrorStage.STORAGE)
        return caught.exception

    def test_append_zero_pages_and_header(self):
        self.assertEqual([self.fm.allocate_page() for _ in range(3)], [2, 3, 4])
        self.assertEqual(self.path.stat().st_size, 5 * PAGE_SIZE)
        self.assertEqual(self.header(), FileHeader(5, INVALID_PAGE_ID))
        for page in (2, 3, 4):
            self.assertEqual(self.fm.read_page(page), bytes(PAGE_SIZE))
        self.assertEqual(self.fm.read_page(1), bytes(PAGE_SIZE))

    def test_release_reuse_lifo_clear_old_bytes(self):
        a, b, c = [self.fm.allocate_page() for _ in range(3)]
        self.fm.write_page(a, b'A' * PAGE_SIZE)
        self.fm.write_page(b, b'B' * PAGE_SIZE)
        self.fm.write_page(c, b'C' * PAGE_SIZE)
        size = self.path.stat().st_size
        self.fm.release_page(a)
        self.fm.release_page(b)
        raw = self.path.read_bytes()
        self.assertEqual(raw[b*PAGE_SIZE:(b+1)*PAGE_SIZE], encode_free_page(a, next_page_id=5))
        self.assertEqual(raw[a*PAGE_SIZE:(a+1)*PAGE_SIZE], encode_free_page(next_page_id=5))
        self.assertEqual(self.fm.allocate_page(), b)
        self.assertEqual(self.fm.read_page(b), bytes(PAGE_SIZE))
        self.assertEqual(self.fm.allocate_page(), a)
        self.assertEqual(self.fm.read_page(a), bytes(PAGE_SIZE))
        self.assertEqual(self.fm.read_page(c), b'C' * PAGE_SIZE)
        self.assertEqual(self.path.stat().st_size, size)
        self.assertEqual(self.fm.allocate_page(), 5)

    def test_invalid_release_changes_nothing(self):
        page = self.fm.allocate_page(); self.fm.release_page(page)
        for value, code in ((0, errors.RESERVED_PAGE), (1, errors.RESERVED_PAGE),
                            (True, errors.PAGE_ID_INVALID), (-1, errors.PAGE_ID_INVALID),
                            (INVALID_PAGE_ID, errors.PAGE_ID_INVALID),
                            (3, errors.PAGE_NOT_ALLOCATED), (page, errors.PAGE_ALREADY_FREE)):
            before = self.path.read_bytes()
            old_header = self.fm._header
            free = set(self.fm._free_pages)
            with self.subTest(value=value):
                self.assert_code(code, self.fm.release_page, value)
                self.assertEqual(self.path.read_bytes(), before)
                self.assertEqual(self.fm._header, old_header)
                self.assertEqual(self.fm._free_pages, free)

    def test_released_page_cannot_be_accessed(self):
        page = self.fm.allocate_page(); self.fm.release_page(page)
        self.assert_code(errors.PAGE_NOT_ALLOCATED, self.fm.read_page, page)
        self.assert_code(errors.PAGE_NOT_ALLOCATED, self.fm.write_page, page, bytes(PAGE_SIZE))
        self.assertEqual(self.fm.allocate_page(), page)
        self.fm.validate_page_id(page)

    def test_reopen_new_process_reuses_and_preserves_live_page(self):
        a, b = self.fm.allocate_page(), self.fm.allocate_page()
        self.fm.write_page(b, b'B' * PAGE_SIZE)
        self.fm.release_page(a); self.fm.sync(); self.fm.close()
        script = '''import json,sys
from minidb.storage.file_manager import FileManager
fm=FileManager.open(sys.argv[1])
try:
 p=fm.allocate_page()
 result=[p, fm.read_page(p)==bytes(4096), fm.read_page(3)==b'B'*4096, fm.allocate_page()]
 fm.sync()
 print(json.dumps(result))
finally:
 fm.close()
'''
        result = subprocess.run([sys.executable, '-B', '-c', script, str(self.path)],
                                cwd=Path(__file__).resolve().parents[1],
                                capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout), [2, True, True, 4])
        reopened = FileManager.open(str(self.path))
        try:
            self.assertEqual(reopened.allocate_page(), 5)
        finally:
            reopened.close()

    def test_page_number_exhaustion_before_write(self):
        with patch.object(self.fm, '_header', FileHeader(INVALID_PAGE_ID)), \
             patch.object(self.fm, '_write_raw') as write:
            exc = self.assert_code(errors.ID_EXHAUSTED, self.fm.allocate_page)
            self.assertEqual(exc.context['id_kind'], 'page')
            write.assert_not_called()

    def test_last_page_number_does_not_wrap(self):
        # Simulated boundary avoids creating a 16 TiB fixture file.
        with patch.object(self.fm, '_header', FileHeader(MAX_PAGE_ID)), \
             patch.object(self.fm, '_write_raw') as write:
            self.assertEqual(self.fm.allocate_page(), MAX_PAGE_ID)
            self.assertEqual(self.fm._header.next_page_id, INVALID_PAGE_ID)
            self.assertEqual(write.call_args_list[0].args[0], MAX_PAGE_ID)
            self.assert_code(errors.ID_EXHAUSTED, self.fm.allocate_page)

    def test_exhausted_append_still_allows_free_page_reuse(self):
        with patch.object(self.fm, '_header', FileHeader(INVALID_PAGE_ID, 2)), \
             patch.object(self.fm, '_free_pages', {2}), \
             patch.object(self.fm, '_read_raw', return_value=encode_free_page(next_page_id=INVALID_PAGE_ID)), \
             patch.object(self.fm, '_write_raw'):
            self.assertEqual(self.fm.allocate_page(), 2)
            self.assertEqual(self.fm._header, FileHeader(INVALID_PAGE_ID))
            self.assertEqual(self.fm._free_pages, set())

    def test_write_failure_does_not_publish_allocation_or_release(self):
        page = self.fm.allocate_page()
        for method, args in ((self.fm.allocate_page, ()), (self.fm.release_page, (page,))):
            for failure_index in (0, 1):
                original = self.fm._header
                free = set(self.fm._free_pages)
                failure = self.fm._error(errors.IO_WRITE_FAILED, 'write_page', cause='injected')
                effects = [failure] if failure_index == 0 else [None, failure]
                with self.subTest(method=method.__name__, fail=failure_index), \
                     patch.object(self.fm, '_write_raw', side_effect=effects):
                    self.assert_code(errors.IO_WRITE_FAILED, method, *args)
                self.assertEqual(self.fm._header, original)
                self.assertEqual(self.fm._free_pages, free)

    def test_failed_reuse_does_not_remove_free_membership(self):
        page = self.fm.allocate_page(); self.fm.release_page(page)
        failure = self.fm._error(errors.IO_WRITE_FAILED, 'write_page', cause='injected')
        with patch.object(self.fm, '_write_raw', side_effect=failure):
            self.assert_code(errors.IO_WRITE_FAILED, self.fm.allocate_page)
        self.assertEqual(self.fm._free_pages, {page})
        self.assertEqual(self.fm._header.free_head, page)

    def test_closed_manager_rejects_allocation_and_release(self):
        self.fm.close()
        self.assert_code(errors.CLOSED, self.fm.allocate_page)
        self.assert_code(errors.CLOSED, self.fm.release_page, 2)


if __name__ == '__main__':
    unittest.main()
