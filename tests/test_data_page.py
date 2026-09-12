"""周升荣负责的数据页格式与页内记录操作测试。"""

import hashlib
import struct
import unittest
from pathlib import Path

from minidb.core.disk_types import INVALID_PAGE_ID, PAGE_SIZE
from minidb.core.records import RowSlot
from minidb.core.errors import (
    PAGE_CORRUPTED,
    ROW_TOO_LARGE,
    SLOT_ID_INVALID,
    STALE_ROW,
    DbError,
)
from minidb.storage.data_page import (
    DATA_PAGE_HEADER_SIZE,
    DATA_PAGE_VERSION_V2,
    MAX_RECORD_SIZE,
    MAX_SLOT_GENERATION,
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
V2_FIXED_ROW = bytes.fromhex("0200020000010000000000000003000000e4b8ad")


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
        self.assertEqual(page.insert(FIXED_ROW), RowSlot(0, 0))

        actual = page.to_bytes()
        self.assertEqual(actual, FIXED_PAGE)
        self.assertEqual(
            hashlib.sha256(actual).hexdigest(),
            "d21d4e75e485ebde191e0d0d3409390daf70f5ebae38a7d6977d0ac13c051950",
        )
        self.assertEqual(page.record(0), FIXED_ROW)

    def test_exact_fit_and_one_byte_too_large_do_not_overflow(self):
        exact = DataPage.empty(1, page_id=2)
        self.assertEqual(exact.insert(b"x" * MAX_RECORD_SIZE), RowSlot(0, 0))
        self.assertEqual(exact.available_space(), 0)

        before = DataPage.empty(1, page_id=3).to_bytes()
        page = DataPage(before, page_id=3)
        self.assert_error(ROW_TOO_LARGE, lambda: page.insert(b"x" * (MAX_RECORD_SIZE + 1)))
        self.assertEqual(page.to_bytes(), before)

    def test_delete_is_idempotent_and_new_insert_does_not_reuse_slot(self):
        page = DataPage.empty(1, page_id=2)
        self.assertEqual(page.insert(b"first"), RowSlot(0, 0))
        self.assertEqual(page.insert(b"second"), RowSlot(1, 0))

        self.assertTrue(page.delete(0))
        self.assertFalse(page.delete(0))
        self.assertIsNone(page.record(0))
        self.assertEqual(page.insert(b"third"), RowSlot(2, 0))
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


class DataPageV2Tests(unittest.TestCase):
    """v2槽代际、压缩和安全复用测试。"""

    def assert_error(self, code, action):
        with self.assertRaises(DbError) as raised:
            action()
        self.assertEqual(raised.exception.code, code)
        return raised.exception

    def new_page(self, page_id=3):
        return DataPage.empty(1, page_id=page_id, version=DATA_PAGE_VERSION_V2)

    def test_v2_fixed_record_matches_design_golden(self):
        page = self.new_page()

        self.assertEqual(page.insert(V2_FIXED_ROW), RowSlot(0, 1))
        actual = page.to_bytes()

        self.assertEqual(page.slots[0].generation, 1)
        self.assertEqual(actual[-8:].hex(), "2000140001010000")
        self.assertEqual(
            hashlib.sha256(actual).hexdigest(),
            "9ea07ca45b8edfa69e358ca39539702a11091458d04136cd55def88fbfb04878",
        )

    def test_deleted_slot_is_reused_with_next_generation(self):
        page = self.new_page()
        self.assertEqual(page.insert(b"first"), RowSlot(0, 1))
        self.assertEqual(page.insert(b"second"), RowSlot(1, 1))

        self.assertTrue(page.delete(0, 1))
        self.assert_error(STALE_ROW, lambda: page.record(0, 1))
        self.assert_error(STALE_ROW, lambda: page.delete(0, 1))
        self.assertEqual(page.insert(b"third"), RowSlot(0, 2))

        self.assertEqual(page.header.slot_count, 2)
        self.assertEqual(page.slots[0].generation, 2)
        self.assertEqual(page.record(0, 2), b"third")
        self.assert_error(STALE_ROW, lambda: page.record(0, 1))
        self.assertEqual(
            [(slot_id, value) for slot_id, value, _ in page.iter_records()],
            [(0, b"third"), (1, b"second")],
        )

    def test_compact_preserves_live_slot_identity_and_clears_deleted_payload(self):
        page = self.new_page()
        for value in (b"a" * 100, b"b" * 120, b"c" * 140):
            page.insert(value)
        identities = {
            slot_id: (slot.generation, page.record(slot_id, slot.generation))
            for slot_id, slot in enumerate(page.slots)
        }
        page.delete(1, 1)

        page.compact()

        self.assertEqual(page.slots[1].offset, 0)
        self.assertEqual(page.slots[1].length, 0)
        self.assertEqual(page.slots[1].generation, 1)
        for slot_id in (0, 2):
            generation, value = identities[slot_id]
            self.assertEqual(page.slots[slot_id].generation, generation)
            self.assertEqual(page.record(slot_id, generation), value)

    def test_insert_compacts_fragmented_page_before_growing_slot_count(self):
        page = self.new_page()
        for value in (b"a" * 1000, b"b" * 1000, b"c" * 1000):
            page.insert(value)
        page.delete(1, 1)
        self.assertLess(page.available_space(), 1500)

        self.assertEqual(page.insert(b"d" * 1500), RowSlot(1, 2))

        self.assertEqual(page.header.slot_count, 3)
        self.assertEqual(page.record(0, 1), b"a" * 1000)
        self.assertEqual(page.record(1, 2), b"d" * 1500)
        self.assertEqual(page.record(2, 1), b"c" * 1000)

    def test_exhausted_generation_is_never_reused(self):
        page = self.new_page()
        page.insert(b"old")
        raw = bytearray(page.to_bytes())
        raw[-3:] = MAX_SLOT_GENERATION.to_bytes(3, "little")
        page = DataPage(bytes(raw), page_id=3)

        page.delete(0, MAX_SLOT_GENERATION)
        self.assertEqual(page.insert(b"new"), RowSlot(1, 1))

        self.assertEqual(page.slots[0].generation, MAX_SLOT_GENERATION)
        self.assertEqual(page.slots[1].generation, 1)
        self.assert_error(
            STALE_ROW,
            lambda: page.record(0, MAX_SLOT_GENERATION),
        )

    def test_repeated_reuse_keeps_slot_count_and_rejects_every_old_identity(self):
        page = self.new_page()
        row_slot = page.insert(b"value-1")
        old_generations = []

        for generation in range(1, 51):
            self.assertEqual(row_slot, RowSlot(0, generation))
            self.assertEqual(page.record(0, generation), f"value-{generation}".encode())
            old_generations.append(generation)
            page.delete(0, generation)
            row_slot = page.insert(f"value-{generation + 1}".encode())

        self.assertEqual(page.header.slot_count, 1)
        self.assertEqual(row_slot, RowSlot(0, 51))
        for generation in old_generations:
            with self.subTest(generation=generation):
                self.assert_error(STALE_ROW, lambda: page.record(0, generation))

    def test_replace_record_is_atomic_and_preserves_generation(self):
        page = self.new_page()
        page.insert(b"a" * 2000)
        page.insert(b"b" * 2000)
        before = page.to_bytes()

        self.assertFalse(page.replace_record(0, 1, b"x" * 2100))
        self.assertEqual(page.to_bytes(), before)
        self.assertTrue(page.replace_record(0, 1, b"short"))
        self.assertEqual(page.record(0, 1), b"short")
        self.assertEqual(page.record(1, 1), b"b" * 2000)
        self.assertEqual([slot.generation for slot in page.slots], [1, 1])

        self.assert_error(STALE_ROW, lambda: page.replace_record(0, 2, b"bad"))
        page.delete(0, 1)
        self.assert_error(STALE_ROW, lambda: page.replace_record(0, 1, b"bad"))

    def test_v2_rejects_zero_generation_and_half_cleared_deleted_slot(self):
        page = self.new_page()
        page.insert(b"record")
        zero_generation = bytearray(page.to_bytes())
        zero_generation[-3:] = b"\x00\x00\x00"
        self.assert_error(
            PAGE_CORRUPTED,
            lambda: DataPage(bytes(zero_generation), page_id=3),
        )

        page.delete(0, 1)
        page.compact()
        half_cleared = bytearray(page.to_bytes())
        struct.pack_into("<H", half_cleared, PAGE_SIZE - 6, 1)
        self.assert_error(
            PAGE_CORRUPTED,
            lambda: DataPage(bytes(half_cleared), page_id=3),
        )


if __name__ == "__main__":
    unittest.main()
