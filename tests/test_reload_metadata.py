"""恢复前置接口：重载只读、完整校验后发布元数据。"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from minidb.core import errors
from minidb.core.disk_types import PAGE_SIZE, INVALID_PAGE_ID
from minidb.storage.file_manager import FileManager
from minidb.storage.page import FileHeader, encode_file_header, encode_free_page
from tests.fakes.file_bytes import read_file_bytes


class ReloadMetadataTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.fm = FileManager.open(str(Path(temp.name) / 'reload.db'))
        self.addCleanup(self.fm.close)

    def test_reloads_restored_boundary_and_free_chain_without_writing(self):
        fm = self.fm
        first, second = fm.allocate_page(), fm.allocate_page()
        fm.release_page(first)
        original = read_file_bytes(fm)
        fm.allocate_page()
        fm.allocate_page()
        # 模拟恢复层通过同一持锁句柄恢复字节和长度，不提供生产恢复接口。
        fm._handle.seek(0)
        fm._handle.write(original)
        fm._handle.truncate(len(original))
        with patch.object(fm, '_write_raw', side_effect=AssertionError('reload must not write')):
            fm.reload_metadata()
        self.assertEqual(read_file_bytes(fm), original)
        self.assertEqual(fm._header, FileHeader(second + 1, first))
        self.assertEqual(fm._free_pages, {first})
        self.assertEqual(fm.allocate_page(), first)
        self.assertEqual(fm.allocate_page(), second + 1)

    def test_corrupt_free_chain_does_not_publish_partial_metadata(self):
        fm = self.fm
        first, second = fm.allocate_page(), fm.allocate_page()
        previous = (fm._header, set(fm._free_pages))
        fm._write_raw(0, encode_file_header(FileHeader(4, first)))
        fm._write_raw(first, encode_free_page(second, next_page_id=4))
        fm._write_raw(second, encode_free_page(first, next_page_id=4))
        before = read_file_bytes(fm)
        with self.assertRaises(errors.DbError) as caught:
            fm.reload_metadata()
        self.assertEqual(caught.exception.code, errors.DB_FORMAT_MISMATCH)
        self.assertEqual((fm._header, fm._free_pages), previous)
        self.assertEqual(read_file_bytes(fm), before)

    def test_read_failure_preserves_metadata_and_closed_reload_is_rejected(self):
        fm = self.fm
        fm.allocate_page()
        previous = (fm._header, set(fm._free_pages))
        failure = fm._error(errors.IO_READ_FAILED, 'read_page')
        with patch.object(fm, '_read_raw', side_effect=failure):
            with self.assertRaises(errors.DbError) as caught:
                fm.reload_metadata()
        self.assertIs(caught.exception, failure)
        self.assertEqual((fm._header, fm._free_pages), previous)
        fm.close()
        with self.assertRaises(errors.DbError) as caught:
            fm.reload_metadata()
        self.assertEqual(caught.exception.code, errors.CLOSED)
