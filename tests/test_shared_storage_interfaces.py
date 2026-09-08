"""Integration checks for the execution contracts and local disk definitions."""
import struct
import tempfile
import unittest
from pathlib import Path

from minidb.core.disk_types import PAGE_SIZE, FORMAT_VERSION, INVALID_PAGE_ID, MAX_PAGE_ID
from minidb.core.records import RowId
from minidb.storage.data_page import DataPageHeader, DATA_PAGE_VERSION
from minidb.storage.file_manager import FileManager
from minidb.storage.page import FileHeader, encode_file_header


class SharedStorageInterfaceTests(unittest.TestCase):
    def test_empty_header_uses_common_page_size(self):
        header = DataPageHeader(1, INVALID_PAGE_ID, 0, 32, PAGE_SIZE, 0)
        self.assertEqual(header.free_end, 4096)
        self.assertEqual(DATA_PAGE_VERSION, FORMAT_VERSION)
        self.assertEqual(RowId(MAX_PAGE_ID, 0).page_id, MAX_PAGE_ID)
        with self.assertRaises(ValueError):
            RowId(INVALID_PAGE_ID, 0)

    def test_header_cannot_extend_beyond_physical_page(self):
        for start, end in ((32, 4097), (4097, 4097)):
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                DataPageHeader(1, INVALID_PAGE_ID, 0, start, end, 0)

    def test_header_slot_boundary_must_agree(self):
        for count, end in ((1, 4096), (0, 4088), (509, 24)):
            with self.subTest(count=count, end=end), self.assertRaises(ValueError):
                DataPageHeader(1, INVALID_PAGE_ID, count, 32, end, 0)

    def test_data_page_fixture_survives_file_io(self):
        # Spec 17.2: one 15-byte row and slot 0, not a production DataPage codec.
        prefix = bytes.fromhex('4d4450470100010001000000ffffffff01002f00f80f00000100000000000000')
        row = bytes.fromhex('010000000000000003000000e4b8ad')
        slot = bytes.fromhex('20000f0001000000')
        page = prefix + row + bytes(4041) + slot
        self.assertEqual(len(page), PAGE_SIZE)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'fixture.db'
            # Preallocate fixture page 2; allocate_page is not implemented yet.
            path.write_bytes(encode_file_header(FileHeader(3)) + bytes(PAGE_SIZE * 2))
            fm = FileManager.open(str(path))
            try:
                fm.write_page(2, page)
                fm.sync()
            finally:
                fm.close()
            fm = FileManager.open(str(path))
            try:
                actual = fm.read_page(2)
            finally:
                fm.close()
        self.assertEqual(actual, page)
        fields = struct.unpack_from('<4sHHIIHHHHII', actual)
        header = DataPageHeader(fields[3], fields[4], fields[5], fields[6], fields[7], fields[9])
        self.assertEqual(header.live_count, 1)
        self.assertEqual(RowId(2, 0).page_id, 2)
