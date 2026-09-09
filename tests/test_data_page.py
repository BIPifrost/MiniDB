"""周升荣负责的数据页格式与页内记录操作测试。"""

import hashlib
import struct
import unittest
from pathlib import Path

from minidb.core.disk_types import INVALID_PAGE_ID, PAGE_SIZE
from minidb.core.errors import PAGE_CORRUPTED, ROW_TOO_LARGE, SLOT_ID_INVALID, DbError
from minidb.storage.data_page import (
    DATA_PAGE_HEADER_SIZE,
    MAX_RECORD_SIZE,
    DataPage,
    SlotState,
    parse_page,
)


FIXED_ROW = bytes.fromhex("010000000000000003000000e4b8ad")
FIXED_PAGE = bytes.fromhex(
    (Path(__file__).parent / "fixtures" / "data_page_v1.hex").read_text(
        encoding="ascii"
    )
)


class DataPageTests(unittest.TestCase):
    def assert_error(self, code, action):
        with self.assertRaises(DbError) as raised:
            action()
        self.assertEqual(raised.exception.code, code)
        return raised.exception

    def test_empty_page_has_fixed_header_and_full_size(self):
        page = DataPage.empty(1, page_id=2)

        self.assertEqual(len(page.to_bytes()), PAGE_SIZE)
        self.assertEqual(page.header.table_id, 1)
        self.assertEqual(page.header.next_page_id, INVALID_PAGE_ID)
        self.assertEqual(page.header.slot_count, 0)
        self.assertEqual(page.header.free_start, DATA_PAGE_HEADER_SIZE)
        self.assertEqual(page.header.free_end, PAGE_SIZE)
        self.assertEqual(page.header.live_count, 0)

    def test_fixed_record_matches_the_independent_golden_page(self):
        page = DataPage.empty(1, page_id=2)
        self.assertEqual(page.insert(FIXED_ROW), 0)

        actual = page.to_bytes()
        self.assertEqual(actual, FIXED_PAGE)
        self.assertEqual(
            hashlib.sha256(actual).hexdigest(),
            "d21d4e75e485ebde191e0d0d3409390daf70f5ebae38a7d6977d0ac13c051950",
        )
        self.assertEqual(page.record(0), FIXED_ROW)

    def test_exact_fit_and_one_byte_too_large_do_not_overflow(self):
        exact = DataPage.empty(1, page_id=2)
        self.assertEqual(exact.insert(b"x" * MAX_RECORD_SIZE), 0)
        self.assertEqual(exact.available_space(), 0)

        before = DataPage.empty(1, page_id=3).to_bytes()
        page = DataPage(before, page_id=3)
        self.assert_error(ROW_TOO_LARGE, lambda: page.insert(b"x" * (MAX_RECORD_SIZE + 1)))
        self.assertEqual(page.to_bytes(), before)

    def test_delete_is_idempotent_and_new_insert_does_not_reuse_slot(self):
        page = DataPage.empty(1, page_id=2)
        self.assertEqual(page.insert(b"first"), 0)
        self.assertEqual(page.insert(b"second"), 1)

        self.assertTrue(page.delete(0))
        self.assertFalse(page.delete(0))
        self.assertIsNone(page.record(0))
        self.assertEqual(page.insert(b"third"), 2)
        self.assertEqual(
            [(slot_id, value) for slot_id, value, _ in page.iter_records()],
            [(1, b"second"), (2, b"third")],
        )
        self.assertEqual(page.header.live_count, 2)

    def test_reset_keeps_table_and_next_page_but_removes_all_slots(self):
        page = DataPage.empty(1, next_page_id=9, page_id=2)
        page.insert(b"record")
        page.delete(0)

        page.reset_records()

        self.assertEqual(page.header.table_id, 1)
        self.assertEqual(page.header.next_page_id, 9)
        self.assertEqual(page.header.slot_count, 0)
        self.assertEqual(page.header.live_count, 0)
        self.assertEqual(page.to_bytes()[DATA_PAGE_HEADER_SIZE:], bytes(PAGE_SIZE - 32))

    def test_slot_range_and_page_identity_errors_are_structured(self):
        page = DataPage.empty(1, page_id=2)
        page.insert(FIXED_ROW)
        self.assert_error(SLOT_ID_INVALID, lambda: page.record(1))
        error = self.assert_error(
            PAGE_CORRUPTED,
            lambda: DataPage(page.to_bytes(), page_id=2, expected_table_id=7),
        )
        self.assertEqual(error.context["page_id"], 2)

    def test_corrupt_header_slot_overlap_and_live_count_are_rejected(self):
        corruptions = []

        bad_magic = bytearray(FIXED_PAGE)
        bad_magic[0] = 0
        corruptions.append(bytes(bad_magic))

        bad_reserved = bytearray(FIXED_PAGE)
        bad_reserved[22] = 1
        corruptions.append(bytes(bad_reserved))

        bad_slot_reserved = bytearray(FIXED_PAGE)
        bad_slot_reserved[PAGE_SIZE - 3] = 1
        corruptions.append(bytes(bad_slot_reserved))

        bad_count = bytearray(FIXED_PAGE)
        struct.pack_into("<I", bad_count, 24, 0)
        corruptions.append(bytes(bad_count))

        # 构造两个槽指向同一段记录；即使其中一个已删除，也属于损坏页。
        overlapping = DataPage.empty(1, page_id=2)
        overlapping.insert(b"aaaa")
        overlapping.insert(b"bbbb")
        bad_overlap = bytearray(overlapping.to_bytes())
        bad_overlap[PAGE_SIZE - 16:PAGE_SIZE - 8] = bad_overlap[PAGE_SIZE - 8:]
        corruptions.append(bytes(bad_overlap))

        for index, data in enumerate(corruptions):
            with self.subTest(index=index):
                self.assert_error(PAGE_CORRUPTED, lambda data=data: parse_page(data, page_id=2))

    def test_short_and_long_pages_are_not_silently_padded_or_truncated(self):
        for data in (FIXED_PAGE[:-1], FIXED_PAGE + b"x"):
            with self.subTest(length=len(data)):
                self.assert_error(PAGE_CORRUPTED, lambda data=data: DataPage(data, page_id=2))

    def test_page_cannot_point_to_itself(self):
        page = DataPage.empty(1, page_id=2)
        self.assert_error(PAGE_CORRUPTED, lambda: page.set_next_page_id(2))

    def test_header_value_object_rejects_reserved_successor_page(self):
        """公开值对象和 bytes 解析必须对 page 1 使用同一套规则。"""
        from minidb.storage.data_page import DataPageHeader

        with self.assertRaises(ValueError):
            DataPageHeader(1, 1, 0, DATA_PAGE_HEADER_SIZE, PAGE_SIZE, 0)


if __name__ == "__main__":
    unittest.main()
