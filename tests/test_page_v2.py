"""v2 文件页独立字节合同；不以编解码往返代替 golden 验证。"""
import unittest
from uuid import UUID

from minidb.core import errors
from minidb.core.disk_types import PAGE_SIZE, INVALID_PAGE_ID
from minidb.storage import page, page_v2 as v2

IDENTITY = UUID('00112233-4455-4677-8899-aabbccddeeff')
GOLDEN = bytes.fromhex(
    '4d494e4944423032' '02000000' '00100000' '01000000'
    '03000000' 'ffffffff' '00112233445546778899aabbccddeeff' '02000000'
) + bytes(4048)


class FilePageV2Tests(unittest.TestCase):
    def assert_code(self, code, fn, *args, **kwargs):
        with self.assertRaises(errors.DbError) as caught:
            fn(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)

    def test_fixed_header_bytes_and_uuid_order(self):
        header = v2.FileHeaderV2(IDENTITY)
        self.assertEqual(v2.encode_file_header(header), GOLDEN)
        self.assertEqual(v2.decode_file_header(GOLDEN, file_size=12288), header)
        self.assertEqual(GOLDEN[28:44], IDENTITY.bytes)

    def test_largest_file_and_highest_ordinary_page(self):
        header = v2.FileHeaderV2(IDENTITY, 16384, 16383)
        raw = v2.encode_file_header(header)
        self.assertEqual(v2.decode_file_header(raw, file_size=67108864), header)
        free = v2.encode_free_page(next_page_id=16384)
        self.assertEqual(v2.decode_free_page(free, page_id=16383, next_page_id=16384), INVALID_PAGE_ID)

    def test_v1_and_v2_are_not_interchangeable(self):
        self.assert_code(errors.DB_FORMAT_MISMATCH, page.decode_file_header,
                         GOLDEN, file_size=12288)
        self.assert_code(errors.DB_FORMAT_MISMATCH, v2.decode_file_header,
                         page.initial_file_header_page(), file_size=8192)

    def test_corrupt_fixed_fields_uuid_and_reserved_bytes(self):
        for offset in (0, 8, 12, 16, 20, 24, 34, 36, 44, 48, 4095):
            with self.subTest(offset=offset):
                raw = bytearray(GOLDEN)
                raw[offset] ^= 0x10 if offset == 34 else 0x80
                if offset == 20:
                    raw[20:24] = bytes(4)
                self.assert_code(errors.DB_FORMAT_MISMATCH, v2.decode_file_header,
                                 bytes(raw), file_size=12288)

    def test_short_long_and_misaligned_lengths(self):
        for size in (0, 8192, 12287):
            self.assert_code(errors.DB_FILE_TRUNCATED, v2.decode_file_header, GOLDEN, file_size=size)
        for size in (12289, 16384, 67108865):
            self.assert_code(errors.DB_FORMAT_MISMATCH, v2.decode_file_header, GOLDEN, file_size=size)
        self.assert_code(errors.DB_FILE_TRUNCATED, v2.decode_file_header, GOLDEN[:-1], file_size=12288)
        self.assert_code(errors.DB_FORMAT_MISMATCH, v2.decode_file_header, GOLDEN+b'x', file_size=12288)

    def test_invalid_encoder_inputs(self):
        for boundary in (True, 2, 16385):
            self.assert_code(errors.INVALID_ARGUMENT, v2.encode_file_header,
                             v2.FileHeaderV2(IDENTITY, boundary))
        for head in (True, 0, 1, 2, 3):
            self.assert_code(errors.INVALID_ARGUMENT, v2.encode_file_header,
                             v2.FileHeaderV2(IDENTITY, 3, head))
        for identity in (None, str(IDENTITY), UUID(int=0)):
            self.assert_code(errors.INVALID_ARGUMENT, v2.encode_file_header, v2.FileHeaderV2(identity))

    def test_free_page_golden_and_reserved_page_rejection(self):
        raw = bytes.fromhex('04000000') + bytes(4092)
        self.assertEqual(v2.encode_free_page(4, next_page_id=5), raw)
        self.assertEqual(v2.decode_free_page(raw, page_id=3, next_page_id=5), 4)
        for reserved in (0, 1, 2):
            self.assert_code(errors.INVALID_ARGUMENT, v2.encode_free_page, reserved, next_page_id=5)
            self.assert_code(errors.INVALID_ARGUMENT, v2.decode_free_page, raw, page_id=reserved, next_page_id=5)

    def test_free_page_rejects_self_link_out_of_range_and_payload(self):
        for successor in (2, 3, 5):
            raw = successor.to_bytes(4, 'little') + bytes(4092)
            self.assert_code(errors.DB_FORMAT_MISMATCH, v2.decode_free_page,
                             raw, page_id=3, next_page_id=5)
        raw = bytes.fromhex('ffffffff') + b'x' + bytes(4091)
        self.assert_code(errors.DB_FORMAT_MISMATCH, v2.decode_free_page,
                         raw, page_id=3, next_page_id=5)


if __name__ == '__main__':
    unittest.main()
