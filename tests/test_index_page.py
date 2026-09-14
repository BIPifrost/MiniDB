"""独立 golden、损坏输入、类型顺序与整页快照接入验证。"""
import hashlib
import struct
import unittest
from dataclasses import replace
from datetime import date
from decimal import Decimal, localcontext

from minidb.core import errors
from minidb.core.disk_types import INVALID_PAGE_ID as NONE
from minidb.core.records import RowId
from minidb.core.schema import DataType, TypeSpec
from minidb.storage.index_page import IndexPage, IndexPageType as Kind, IndexEntry, IndexKeyCodec


class IndexPageTests(unittest.TestCase):
    def setUp(self):
        self.codec = IndexKeyCodec(TypeSpec(DataType.INT))
        self.leaf = IndexPage(Kind.LEAF, 1, 3)

    def decode(self, data, codec=None):
        return IndexPage.decode(data, codec or self.codec, page_id=4, expected_index_id=1)

    def assert_corrupt(self, data):
        with self.assertRaises(errors.DbError) as caught:
            self.decode(data)
        self.assertEqual(caught.exception.code, errors.INDEX_CORRUPTED)

    def test_empty_leaf_golden(self):
        raw = self.leaf.encode(self.codec, page_id=4)
        expected = bytes.fromhex('4d494458020002000100000003000000ffffffff0000280000100000ffffffff0000000000000000') + bytes(4056)
        self.assertEqual(raw, expected)
        self.assertEqual(hashlib.sha256(raw).hexdigest(), '0a755a1385d475b41f96221afb0c54514b3e326ebefe0d8195e6c0e870fdf92d')
        self.assertEqual(self.decode(raw), self.leaf)

    def test_anchor_golden(self):
        page = IndexPage(Kind.ANCHOR, 1, NONE, left_child=4)
        raw = page.encode(self.codec, page_id=3)
        self.assertEqual(hashlib.sha256(raw).hexdigest(), 'f459b55efcdf3099c44cda7f7be16952cd983f130ce72463c54f57af2c6fe8b4')
        self.assertEqual(IndexPage.decode(raw, self.codec, page_id=3, expected_index_id=1), page)

    def test_leaf_and_internal_roundtrip(self):
        for kind in (Kind.LEAF, Kind.INTERNAL):
            entries = tuple(IndexEntry(value, RowId(10, i, 1), 20+i if kind is Kind.INTERNAL else NONE)
                            for i, value in enumerate((None, -99, 0, 99)))
            page = IndexPage(kind, 1, 3, left_child=19 if kind is Kind.INTERNAL else NONE, entries=entries)
            self.assertEqual(self.decode(page.encode(self.codec, page_id=4)), page)

    def test_record_wire_format(self):
        page = replace(self.leaf, entries=(IndexEntry(-1, RowId(256, 2, 0x030201)),))
        raw = page.encode(self.codec, page_id=4)
        self.assertEqual(raw[40:60].hex(), '0108007fffffffffffffff000100000200010203')
        self.assertEqual(raw[-8:], struct.pack('<HHHH', 40, 20, 0, 0))

    def test_numeric_row_id_order_not_little_endian_byte_order(self):
        page = replace(self.leaf, entries=tuple(IndexEntry(1, RowId(p, 0, 1)) for p in (255, 256)))
        self.assertEqual(self.decode(page.encode(self.codec, page_id=4)), page)

    def test_duplicate_and_unsorted_rejected(self):
        for values in ((2, 1), (1, 1)):
            page = replace(self.leaf, entries=tuple(IndexEntry(v, RowId(10, 0, 1)) for v in values))
            with self.assertRaises(errors.DbError):
                page.encode(self.codec, page_id=4)

    def test_header_corruption(self):
        original = self.leaf.encode(self.codec, page_id=4)
        for offset in (0, 4, 6, 8, 12, 20, 22, 24, 26, 28, 32, 36):
            with self.subTest(offset=offset):
                data = bytearray(original)
                data[offset] ^= 0x40
                if offset == 12:  # 合法的另一父页无法仅凭单页判断，改为保留页。
                    struct.pack_into('<I', data, 12, 1)
                if offset == 22:
                    struct.pack_into('<H', data, 22, 39)
                self.assert_corrupt(bytes(data))

    def test_short_long_and_wrong_owner(self):
        raw = self.leaf.encode(self.codec, page_id=4)
        for data in (raw[:-1], raw+b'\0'):
            self.assert_corrupt(data)
        with self.assertRaises(errors.DbError):
            IndexPage.decode(raw, self.codec, page_id=4, expected_index_id=2)

    def test_slot_overlap_flags_and_invalid_key(self):
        page = replace(self.leaf, entries=(IndexEntry(1, RowId(10, 0, 1)), IndexEntry(2, RowId(10, 1, 1))))
        raw = page.encode(self.codec, page_id=4)
        mutations = ((4080, '<H', 40), (4092, '<H', 1), (4094, '<H', 1),
                     (4088, '<H', 39), (4090, '<H', 1), (40, '<B', 2),
                     (41, '<H', 9), (57, '<B', 0))
        for offset, fmt, value in mutations:
            with self.subTest(offset=offset):
                data = bytearray(raw)
                struct.pack_into(fmt, data, offset, value)
                self.assert_corrupt(bytes(data))

    def test_links_and_generation(self):
        pages = (replace(self.leaf, parent_page_id=4), replace(self.leaf, right_sibling=4),
                 replace(self.leaf, left_child=5), IndexPage(Kind.ANCHOR, 1, NONE, left_child=4),
                 replace(self.leaf, entries=(IndexEntry(1, RowId(10, 0, 0)),)),
                 IndexPage(Kind.INTERNAL, 1, 3, left_child=5,
                           entries=(IndexEntry(1, RowId(10, 0, 1), 5),)))
        for page in pages:
            with self.assertRaises(errors.DbError):
                page.encode(self.codec, page_id=4)

    def test_capacity_exact_and_overflow_no_mutation(self):
        codec = IndexKeyCodec(TypeSpec(DataType.VARCHAR))
        entries = tuple(IndexEntry('x'*512, RowId(10, i, 1)) for i in range(7))
        page = replace(self.leaf, entries=entries)
        raw = page.encode(codec, page_id=4)
        with self.assertRaises(errors.DbError) as caught:
            replace(page, entries=entries+(IndexEntry('x'*512, RowId(10, 7, 1)),)).encode(codec, page_id=4)
        self.assertEqual(caught.exception.code, errors.RESOURCE_LIMIT)
        self.assertEqual(page.encode(codec, page_id=4), raw)
        exact = replace(page, entries=(IndexEntry('a'*312, RowId(10, 8, 1)),)+entries)
        exact_raw = exact.encode(codec, page_id=4)
        self.assertEqual(struct.unpack_from('<HH', exact_raw, 22), (4032, 4032))
        self.assertEqual(self.decode(exact_raw, codec), exact)

    def test_real_buffer_snapshot_eviction_and_reopen(self):
        import tempfile
        from pathlib import Path
        from uuid import uuid4
        from minidb.storage.page_v2 import FileHeaderV2, encode_file_header
        from minidb.storage.file_lock import DatabaseLock
        from minidb.storage.file_manager import FileManager
        from minidb.storage.buffer_pool import BufferPool
        from minidb.core.transaction import TransactionGuard, TransactionState
        raw = self.leaf.encode(self.codec, page_id=4)
        for policy in ('lru', 'fifo'):
            with self.subTest(policy=policy), tempfile.TemporaryDirectory() as temp:
                path = Path(temp)/'index.db'
                path.write_bytes(encode_file_header(FileHeaderV2(uuid4(), 5)) + bytes(3*4096) + raw)
                fm = FileManager.open_locked(str(path), DatabaseLock.acquire(str(path)),
                                             guard=TransactionGuard(TransactionState.ACTIVE))
                try:
                    pool = BufferPool(fm, capacity=1, policy=policy)
                    snapshot = pool.get_snapshot(4)
                    updated = replace(self.leaf, entries=(IndexEntry(42, RowId(10, 0, 1)),))
                    updated_raw = updated.encode(self.codec, page_id=4)
                    pool.write_if_current(snapshot, updated_raw)
                    pool.get_page(3)  # 脏叶页淘汰并写回。
                    with self.assertRaises(errors.DbError) as caught:
                        pool.write_if_current(snapshot, raw)
                    self.assertEqual(caught.exception.code, errors.STALE_PAGE)
                    self.assertEqual(self.decode(fm.read_page(4)), updated)
                    fm.sync()
                finally:
                    fm.close()
                reopened = FileManager.open_locked(str(path), DatabaseLock.acquire(str(path)))
                try:
                    self.assertEqual(self.decode(reopened.read_page(4)), updated)
                finally:
                    reopened.close()

    def test_all_type_order_and_roundtrip(self):
        cases = ((TypeSpec(DataType.INT), (-(1<<63), -1, 0, (1<<63)-1)),
                 (TypeSpec(DataType.VARCHAR), ('', 'a', 'aa', 'b', '中')),
                 (TypeSpec(DataType.BOOL), (False, True)),
                 (TypeSpec(DataType.DATE), (date.min, date(1970,1,1), date.max)),
                 (TypeSpec(DataType.DECIMAL, precision=18, scale=2), (Decimal('-12.34'), Decimal('0.00'), Decimal('12.34'))))
        for spec, values in cases:
            codec = IndexKeyCodec(spec)
            encoded = [codec.encode(v) for v in (None,)+values]
            self.assertEqual(encoded, sorted(encoded))
            self.assertEqual([codec.decode(*item) for item in encoded], [None]+list(values))

    def test_mutated_pages_never_leak_struct_errors(self):
        import random
        rng = random.Random(20260914)
        raw = replace(self.leaf, entries=(IndexEntry(5, RowId(10, 0, 1)),)).encode(self.codec, page_id=4)
        for _ in range(1000):
            data = bytearray(raw)
            offset = rng.choice(list(range(60))+list(range(4088, 4096)))
            data[offset] ^= rng.randrange(1, 256)
            try:
                self.decode(bytes(data))
            except errors.DbError as exc:
                self.assertEqual(exc.code, errors.INDEX_CORRUPTED)

    def test_decimal_independent_of_global_context(self):
        codec = IndexKeyCodec(TypeSpec(DataType.DECIMAL, precision=18, scale=2))
        value = Decimal('1234567890123456.78')
        with localcontext() as context:
            context.prec = 2
            self.assertEqual(codec.decode(*codec.encode(value)), value)

    def test_key_corruption_and_length_limit(self):
        for spec, tag, payload in ((TypeSpec(DataType.BOOL), 1, b'\x02'),
                                   (TypeSpec(DataType.INT), 1, b'\0'),
                                   (TypeSpec(DataType.INT), 0, b'x'),
                                   (TypeSpec(DataType.VARCHAR), 1, b'\xff'),
                                   (TypeSpec(DataType.DATE), 1, b'\xff'*4),
                                   (TypeSpec(DataType.DECIMAL, precision=2, scale=0), 1, b'\xff'*8)):
            with self.assertRaises(errors.DbError) as caught:
                IndexKeyCodec(spec).decode(tag, payload)
            self.assertEqual(caught.exception.code, errors.INDEX_CORRUPTED)
        codec = IndexKeyCodec(TypeSpec(DataType.VARCHAR))
        self.assertEqual(len(codec.encode('x'*512)[1]), 512)
        with self.assertRaises(errors.DbError) as caught:
            codec.encode('中'*171)
        self.assertEqual(caught.exception.code, errors.INDEX_KEY_TOO_LARGE)


if __name__ == '__main__':
    unittest.main()
