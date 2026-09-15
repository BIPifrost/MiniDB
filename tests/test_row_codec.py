"""RowCodec 的 v2 固定字节格式、边界与错误契约测试。

固定样例来自工作计划第 8.2、12.1、12.2 节，golden 字节不调用被测实现生成。
"""

import json
import unittest
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from minidb.core.errors import (
    INVALID_ARGUMENT,
    NOT_NULL_VIOLATION,
    NUMERIC_OUT_OF_RANGE,
    NUMERIC_SCALE_MISMATCH,
    ROW_CORRUPTED,
    ROW_ENCODING_ERROR,
    ROW_TOO_LARGE,
    ROW_TYPE_MISMATCH,
    ROW_VALUE_COUNT_MISMATCH,
    VALUE_TOO_LONG,
    DbError,
)
from minidb.core.schema import ColumnDef, DataType, Schema, TypeSpec
from minidb.storage.row_codec import (
    INT64_MAX,
    INT64_MIN,
    MAX_ROW_SIZE,
    ROW_PREFIX_SIZE,
    ROW_VERSION_V2,
    RowCodec,
)


FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "row_codec_v2.json").read_text(encoding="utf-8")
)


def schema_of(columns) -> Schema:
    return Schema(tuple(
        ColumnDef(
            column["name"],
            TypeSpec(
                DataType[column["kind"]],
                length=column.get("length"),
                precision=column.get("precision"),
                scale=column.get("scale"),
            ),
            nullable=column["nullable"],
            primary_key=column.get("primary_key", False),
            unique=column.get("unique", False),
        )
        for column in columns
    ))


def row_of(values) -> tuple:
    built = []
    for item in values:
        kind = item["type"]
        if kind == "NULL":
            built.append(None)
        elif kind in ("INT", "BOOL"):
            built.append(item["value"])
        elif kind == "VARCHAR":
            raw = item["value"]
            built.append(raw["repeat"] * raw["count"] if isinstance(raw, dict) else raw)
        elif kind == "DATE":
            built.append(date.fromisoformat(item["value"]))
        else:
            built.append(Decimal(item["value"]))
    return tuple(built)


def case_schema(case) -> Schema:
    return schema_of(case["columns"])


def case_row(case) -> tuple:
    return row_of(case["values"])


class RowCodecTests(unittest.TestCase):
    """测试 minidb/storage/row_codec.py 与 Schema、Row 的共享字节接口。"""

    def setUp(self) -> None:
        self.codec = RowCodec()
        self.schema = Schema((
            ColumnDef("id", DataType.INT, nullable=False, primary_key=True, unique=True),
            ColumnDef("name", TypeSpec(DataType.VARCHAR, length=64)),
        ))

    def assert_error(self, code: str, action) -> DbError:
        with self.assertRaises(DbError) as raised:
            action()
        self.assertEqual(raised.exception.code, code)
        return raised.exception

    # ---- 第 12.1 节固定 golden 字节 ----

    def test_fixed_golden_rows_encode_to_plan_bytes(self) -> None:
        for case in FIXTURE["cases"]:
            if "encoded_hex" not in case:
                continue
            with self.subTest(case=case["name"]):
                schema, row = case_schema(case), case_row(case)
                encoded = self.codec.encode(row, schema)
                self.assertEqual(encoded.hex(), case["encoded_hex"])
                self.assertEqual(len(encoded), case["encoded_size"])
                self.assertEqual(self.codec.encoded_size(row, schema), case["encoded_size"])
                self.assertEqual(self.codec.decode(encoded, schema), row)

    def test_v2_prefix_carries_version_flags_and_column_count(self) -> None:
        encoded = self.codec.encode((1, "中"), self.schema)
        self.assertEqual(encoded[0], ROW_VERSION_V2)
        self.assertEqual(encoded[1], 0)
        self.assertEqual(int.from_bytes(encoded[2:ROW_PREFIX_SIZE], "little"), 2)
        # 两列非空时空值位图必须为 0，且位图紧跟在 4 字节前缀之后。
        self.assertEqual(encoded[ROW_PREFIX_SIZE], 0)

    # ---- 长度一致性、位图与最大行边界 ----

    def test_encoded_size_matches_encode_for_every_fixture_case(self) -> None:
        for case in FIXTURE["cases"]:
            if "encoded_hex" in case or "expect_error" in case:
                continue
            with self.subTest(case=case["name"]):
                schema, row = case_schema(case), case_row(case)
                self.assertEqual(self.codec.encoded_size(row, schema), case["encoded_size"])
                self.assertEqual(len(self.codec.encode(row, schema)), case["encoded_size"])
                self.assertEqual(self.codec.decode(self.codec.encode(row, schema), schema), row)

    def test_maximum_row_is_4056_and_one_more_byte_is_rejected(self) -> None:
        accepted = next(c for c in FIXTURE["cases"] if c["name"] == "maximum_row_four_varchars")
        rejected = next(c for c in FIXTURE["cases"] if c["name"] == "maximum_row_plus_one_byte")
        for case in (accepted, rejected):
            with self.subTest(case=case["name"]):
                schema, row = case_schema(case), case_row(case)
                if "expect_error" in case:
                    self.assert_error(case["expect_error"], lambda: self.codec.encoded_size(row, schema))
                    self.assert_error(case["expect_error"], lambda: self.codec.encode(row, schema))
                else:
                    self.assertEqual(self.codec.encoded_size(row, schema), MAX_ROW_SIZE)
                    self.assertEqual(len(self.codec.encode(row, schema)), MAX_ROW_SIZE)

    def test_null_bitmap_marks_exactly_the_null_columns(self) -> None:
        nullable = Schema((
            ColumnDef("a", DataType.INT),
            ColumnDef("b", DataType.BOOL),
            ColumnDef("c", TypeSpec(DataType.VARCHAR, length=8)),
        ))
        encoded = self.codec.encode((1, None, None), nullable)
        self.assertEqual(encoded[ROW_PREFIX_SIZE], 0b110)
        self.assertEqual(self.codec.decode(encoded, nullable), (1, None, None))
        self.assertEqual(self.codec.encoded_size((1, None, None), nullable),
                         ROW_PREFIX_SIZE + 1 + 8)

    def test_non_nullable_column_rejects_null_on_encode(self) -> None:
        error = self.assert_error(NOT_NULL_VIOLATION, lambda: self.codec.encode((None, "x"), self.schema))
        self.assertEqual(error.context["column"], "id")
        self.assert_error(NOT_NULL_VIOLATION, lambda: self.codec.encoded_size((None, "x"), self.schema))
        self.assertEqual(self.codec.decode(self.codec.encode((1, None), self.schema), self.schema),
                         (1, None))

    # ---- 类型与取值规则（复核 normalize_value，不新建第二套规则）----

    def test_exact_python_types_are_required(self) -> None:
        error = self.assert_error(ROW_TYPE_MISMATCH, lambda: self.codec.encode((True, "name"), self.schema))
        self.assertEqual(error.context["column_index"], 0)
        self.assertEqual(error.context["expected"], "INT")
        self.assert_error(ROW_TYPE_MISMATCH, lambda: self.codec.encoded_size((1, 2), self.schema))
        bool_schema = Schema((ColumnDef("flag", DataType.BOOL),))
        self.assert_error(ROW_TYPE_MISMATCH, lambda: self.codec.encode((1,), bool_schema))
        date_schema = Schema((ColumnDef("day", DataType.DATE),))
        # datetime 是 date 的子类，但工作计划禁止把它当作 DATE 值。
        self.assert_error(ROW_TYPE_MISMATCH,
                          lambda: self.codec.encode((datetime(2026, 9, 14),), date_schema))
        self.assert_error(ROW_VALUE_COUNT_MISMATCH, lambda: self.codec.encode((1,), self.schema))

    def test_value_limits_use_shared_error_codes(self) -> None:
        self.assert_error(NUMERIC_OUT_OF_RANGE,
                          lambda: self.codec.encode((INT64_MAX + 1, ""), self.schema))
        self.assert_error(NUMERIC_OUT_OF_RANGE,
                          lambda: self.codec.encode((INT64_MIN - 1, ""), self.schema))
        decimal_schema = Schema((ColumnDef("amount", TypeSpec(DataType.DECIMAL, precision=5, scale=2)),))
        self.assert_error(NUMERIC_SCALE_MISMATCH,
                          lambda: self.codec.encode((Decimal("1.231"),), decimal_schema))
        self.assert_error(NUMERIC_OUT_OF_RANGE,
                          lambda: self.codec.encode((Decimal("1000.00"),), decimal_schema))
        self.assert_error(VALUE_TOO_LONG, lambda: self.codec.encode((1, "中" * 65), self.schema))
        self.assert_error(ROW_ENCODING_ERROR, lambda: self.codec.encode((1, "\ud800"), self.schema))

    def test_int64_boundaries_encode_as_little_endian(self) -> None:
        encoded_min = self.codec.encode((INT64_MIN, ""), self.schema)
        encoded_max = self.codec.encode((INT64_MAX, ""), self.schema)
        self.assertEqual(encoded_min[ROW_PREFIX_SIZE + 1:ROW_PREFIX_SIZE + 9].hex(),
                         "0000000000000080")
        self.assertEqual(encoded_max[ROW_PREFIX_SIZE + 1:ROW_PREFIX_SIZE + 9].hex(),
                         "ffffffffffffff7f")
        self.assertEqual(self.codec.decode(encoded_min, self.schema), (INT64_MIN, ""))

    def test_decimal_never_uses_float_and_normalizes_scale(self) -> None:
        schema = Schema((ColumnDef("amount", TypeSpec(DataType.DECIMAL, precision=5, scale=2)),))
        self.assertEqual(self.codec.decode(self.codec.encode((Decimal("1.2"),), schema), schema),
                         (Decimal("1.20"),))
        self.assertEqual(self.codec.decode(self.codec.encode((Decimal("1.2300"),), schema), schema),
                         (Decimal("1.23"),))
        self.assertEqual(self.codec.encode((Decimal("1.2"),), schema),
                         self.codec.encode((Decimal("1.20"),), schema))
        # -0.00 归一为 0.00，不保存负零。
        zero = self.codec.encode((Decimal("-0.00"),), schema)
        self.assertEqual(zero[ROW_PREFIX_SIZE + 1:], b"\x00" * 8)
        self.assertEqual(self.codec.decode(self.codec.encode((Decimal("-0.00"),), schema), schema),
                         (Decimal("0.00"),))

    def test_characters_and_utf8_bytes_are_checked_separately(self) -> None:
        one_column = Schema((ColumnDef("text", TypeSpec(DataType.VARCHAR, length=1024), nullable=False),))
        # 1024 个三字节字符满足字符上限，单列行仍远小于一页。
        encoded = self.codec.encode(("中" * 1024,), one_column)
        self.assertEqual(len(encoded), ROW_PREFIX_SIZE + 1 + 4 + 3072)
        # 字符上限单独执行：1025 个字符既超字符数，也超字节容量，但必须先报字符错误。
        self.assert_error(VALUE_TOO_LONG, lambda: self.codec.encode(("中" * 1025,), one_column))
        # 字节容量单独执行：四列各 1024 个中文字符，字符数合法而总行超限。
        wide = Schema(tuple(
            ColumnDef(name, TypeSpec(DataType.VARCHAR, length=1024))
            for name in ("a", "b", "c", "d")
        ))
        self.assert_error(ROW_TOO_LARGE,
                          lambda: self.codec.encode(("中" * 1024,) * 4, wide))

    # ---- decode 的损坏拒绝 ----

    def test_decode_rejects_corrupted_prefix_and_bitmap(self) -> None:
        good = self.codec.encode((1, "中"), self.schema)
        self.assert_error(ROW_CORRUPTED, lambda: self.codec.decode(b"\x01", self.schema))
        bad_version = bytes([1]) + good[1:2] + good[2:]
        self.assert_error(ROW_CORRUPTED, lambda: self.codec.decode(bad_version, self.schema))
        bad_flags = good[:1] + b"\x01" + good[2:]
        self.assert_error(ROW_CORRUPTED, lambda: self.codec.decode(bad_flags, self.schema))
        bad_count = good[:2] + (3).to_bytes(2, "little") + good[4:]
        self.assert_error(ROW_CORRUPTED, lambda: self.codec.decode(bad_count, self.schema))
        # 两列只使用 bitmap 的低两位，高位非零即为损坏。
        high_bit = good[:ROW_PREFIX_SIZE] + b"\x80" + good[ROW_PREFIX_SIZE + 1:]
        self.assert_error(ROW_CORRUPTED, lambda: self.codec.decode(high_bit, self.schema))

    def test_decode_rejects_null_in_non_nullable_column(self) -> None:
        null_on_non_nullable = bytes.fromhex("02000200010100000000000000")
        self.assert_error(ROW_CORRUPTED,
                          lambda: self.codec.decode(null_on_non_nullable, self.schema))

    def test_decode_rejects_truncated_trailing_and_oversized_data(self) -> None:
        complete = self.codec.encode((1, "x"), self.schema)
        self.assert_error(ROW_CORRUPTED, lambda: self.codec.decode(complete[:-1], self.schema))
        self.assert_error(ROW_CORRUPTED, lambda: self.codec.decode(complete + b"\x00", self.schema))
        self.assert_error(ROW_CORRUPTED, lambda: self.codec.decode(b"\x00" * (MAX_ROW_SIZE + 1),
                                                                   self.schema))

    def test_decode_rejects_invalid_bool_utf8_and_date(self) -> None:
        bool_schema = Schema((ColumnDef("flag", DataType.BOOL),))
        self.assert_error(ROW_CORRUPTED, lambda: self.codec.decode(bytes.fromhex("020001000002"), bool_schema))
        self.assert_error(ROW_CORRUPTED, lambda: self.codec.decode(bytes.fromhex("0200010000"), bool_schema))
        text_schema = Schema((ColumnDef("text", TypeSpec(DataType.VARCHAR, length=8)),))
        self.assert_error(ROW_CORRUPTED,
                          lambda: self.codec.decode(bytes.fromhex("020001000001000000ff"), text_schema))
        date_schema = Schema((ColumnDef("day", DataType.DATE),))
        self.assert_error(ROW_CORRUPTED,
                          lambda: self.codec.decode(bytes.fromhex("0200010000ffffff7f"), date_schema))
        # 0x00000000 是 1970-01-01，必须正常读回。
        self.assertEqual(self.codec.decode(bytes.fromhex("020001000000000000"), date_schema),
                         (date(1970, 1, 1),))

    def test_decode_rejects_non_canonical_values(self) -> None:
        short_text = Schema((ColumnDef("text", TypeSpec(DataType.VARCHAR, length=2)),))
        # VARCHAR(2) 的行里出现 3 个字符：decode 不能绕过字符上限。
        overlong = bytes.fromhex("020001000003000000") + "abc".encode("utf-8")
        self.assert_error(ROW_CORRUPTED, lambda: self.codec.decode(overlong, short_text))
        decimal_schema = Schema((ColumnDef("amount", TypeSpec(DataType.DECIMAL, precision=5, scale=2)),))
        # 缩放整数 1000000 表示 10000.00，超出 DECIMAL(5,2) 精度。
        too_precise = bytes.fromhex("02000100" + "00" + "40" + "42" + "0f000000000000")
        self.assert_error(ROW_CORRUPTED, lambda: self.codec.decode(too_precise, decimal_schema))

    def test_decode_requires_formal_schema_and_bytes(self) -> None:
        self.assert_error(INVALID_ARGUMENT, lambda: self.codec.decode(b"\x00", object()))
        self.assert_error(INVALID_ARGUMENT, lambda: self.codec.decode(bytearray(b"\x00"), self.schema))
        self.assert_error(INVALID_ARGUMENT, lambda: self.codec.encode([1, "x"], self.schema))
        self.assert_error(INVALID_ARGUMENT, lambda: self.codec.encoded_size((1, "x"), object()))

    def test_round_trip_covers_all_five_types_with_nulls(self) -> None:
        schema = Schema((
            ColumnDef("id", DataType.INT, nullable=False, primary_key=True, unique=True),
            ColumnDef("name", TypeSpec(DataType.VARCHAR, length=64)),
            ColumnDef("active", DataType.BOOL),
            ColumnDef("birthday", DataType.DATE),
            ColumnDef("balance", TypeSpec(DataType.DECIMAL, precision=18, scale=4)),
        ))
        row = (7, "赵凯航", True, date(2000, 2, 29), Decimal("-1234.5000"))
        self.assertEqual(self.codec.decode(self.codec.encode(row, schema), schema), row)
        empty = (8, None, None, None, None)
        self.assertEqual(self.codec.decode(self.codec.encode(empty, schema), schema), empty)
        self.assertEqual(self.codec.encoded_size(empty, schema), ROW_PREFIX_SIZE + 1 + 8)


if __name__ == "__main__":
    unittest.main()
