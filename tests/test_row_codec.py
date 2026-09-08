"""RowCodec 的固定字节格式、边界与错误契约测试。"""

import unittest
import json
from pathlib import Path

from minidb.core.errors import (
    INT_OUT_OF_RANGE,
    ROW_CORRUPTED,
    ROW_ENCODING_ERROR,
    ROW_TOO_LARGE,
    ROW_TYPE_MISMATCH,
    ROW_VALUE_COUNT_MISMATCH,
    DbError,
    ErrorStage,
)
from minidb.core.schema import ColumnDef, DataType, Schema
from minidb.storage.row_codec import INT64_MAX, INT64_MIN, MAX_ROW_SIZE, RowCodec


class RowCodecTests(unittest.TestCase):
    """测试 minidb/storage/row_codec.py 与 Schema、Row 的共享字节接口。"""

    def setUp(self) -> None:
        self.codec = RowCodec()
        self.schema = Schema((
            ColumnDef("id", DataType.INT),
            ColumnDef("name", DataType.VARCHAR),
        ))

    def assert_error(self, code: str, action) -> DbError:
        with self.assertRaises(DbError) as raised:
            action()
        self.assertEqual(raised.exception.code, code)
        return raised.exception

    def test_shared_utf8_fixture_round_trips_to_fixed_bytes(self) -> None:
        # 测试 row_codec.py 的跨成员固定格式：StorageEngine 可依此读写同一行字节。
        fixture = json.loads(
            (Path(__file__).parent / "fixtures" / "row_codec_v1.json").read_text(encoding="utf-8")
        )
        encoded = self.codec.encode((1, "中"), self.schema)
        self.assertEqual(encoded, bytes.fromhex(fixture["encoded_hex"]))
        self.assertEqual(len(encoded), fixture["encoded_size"])
        self.assertEqual(self.codec.encoded_size((1, "中"), self.schema), fixture["encoded_size"])
        self.assertEqual(self.codec.decode(encoded, self.schema), (1, "中"))

    def test_int64_boundaries_and_empty_string_have_fixed_encoding(self) -> None:
        self.assertEqual(self.codec.encode((INT64_MIN, ""), self.schema)[:8].hex(), "0000000000000080")
        self.assertEqual(self.codec.encode((INT64_MAX, ""), self.schema)[:8].hex(), "ffffffffffffff7f")
        self.assertEqual(self.codec.encode((0, ""), self.schema)[8:], bytes.fromhex("00000000"))

    def test_rejects_wrong_value_count_and_exact_python_types(self) -> None:
        self.assert_error(ROW_VALUE_COUNT_MISMATCH, lambda: self.codec.encode((1,), self.schema))
        error = self.assert_error(ROW_TYPE_MISMATCH, lambda: self.codec.encode((True, "name"), self.schema))
        self.assertEqual(error.context["column_index"], 0)
        self.assertEqual(error.context["expected"], "INT")
        self.assert_error(ROW_TYPE_MISMATCH, lambda: self.codec.encoded_size((1, 2), self.schema))

    def test_direct_int_range_check_uses_shared_code(self) -> None:
        error = self.assert_error(INT_OUT_OF_RANGE, lambda: self.codec.encode((INT64_MAX + 1, ""), self.schema))
        self.assertIs(error.stage, ErrorStage.SYNTAX)
        self.assertEqual(error.context["column_index"], 0)

    def test_surrogate_string_becomes_row_encoding_error(self) -> None:
        self.assert_error(ROW_ENCODING_ERROR, lambda: self.codec.encode((1, "\ud800"), self.schema))

    def test_row_size_limit_is_checked_before_any_page_write(self) -> None:
        one_string = Schema((ColumnDef("value", DataType.VARCHAR),))
        row = ("a" * (MAX_ROW_SIZE - 4),)
        self.assertEqual(self.codec.encoded_size(row, one_string), MAX_ROW_SIZE)
        self.assertEqual(len(self.codec.encode(row, one_string)), MAX_ROW_SIZE)
        self.assert_error(ROW_TOO_LARGE, lambda: self.codec.encode(("a" * (MAX_ROW_SIZE - 3),), one_string))

    def test_decode_rejects_truncated_invalid_or_trailing_data(self) -> None:
        self.assert_error(ROW_CORRUPTED, lambda: self.codec.decode(b"\x01", self.schema))
        self.assert_error(ROW_CORRUPTED, lambda: self.codec.decode(
            bytes.fromhex("010000000000000002000000fffe"), self.schema,
        ))
        complete = self.codec.encode((1, "x"), self.schema)
        self.assert_error(ROW_CORRUPTED, lambda: self.codec.decode(complete + b"\x00", self.schema))

    def test_decode_rejects_a_record_larger_than_one_page_slot(self) -> None:
        # 测试 row_codec.py 的统一行大小上限：损坏页不能通过 decode 绕过 4056 字节限制。
        large_schema = Schema((ColumnDef("value", DataType.VARCHAR),))
        data = (MAX_ROW_SIZE - 4 + 1).to_bytes(4, "little") + b"a" * (MAX_ROW_SIZE - 4 + 1)
        self.assert_error(ROW_CORRUPTED, lambda: self.codec.decode(data, large_schema))


if __name__ == "__main__":
    unittest.main()
