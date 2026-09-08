"""diagnostics.py 的单元测试。

验证仅语法检查结果的固定字段，以及 Token、位置和 DbError 的稳定 JSON
trace 格式。运行方式：python -m unittest tests.test_diagnostics -v。
"""

from dataclasses import dataclass
import json
from pathlib import Path
import unittest

from minidb.core.diagnostics import SyntaxCheckResult, format_trace
from minidb.core.errors import DbError, ErrorStage, INVALID_CHARACTER
from minidb.core.source import SourcePos, SourceSpan
from minidb.core.tokens import Token, TokenKind


def _span() -> SourceSpan:
    return SourceSpan(
        start=SourcePos(line=1, column=1, offset=0),
        end=SourcePos(line=1, column=7, offset=6),
        source_name="<test>",
    )


class SyntaxCheckResultTests(unittest.TestCase):
    """测试 diagnostics.py 的 SyntaxCheckResult 固定字段和不可变性。"""

    def test_valid_result_is_immutable(self) -> None:
        error = DbError(ErrorStage.LEXICAL, INVALID_CHARACTER, "非法字符", _span())
        result = SyntaxCheckResult(1, (error,), False)
        self.assertEqual(result.valid_statement_count, 1)
        self.assertEqual(result.errors, (error,))
        with self.assertRaises(Exception):
            result.valid_statement_count = 2  # type: ignore[misc]

    def test_rejects_invalid_public_fields(self) -> None:
        error = DbError(ErrorStage.LEXICAL, INVALID_CHARACTER, "非法字符", _span())
        with self.assertRaises(TypeError):
            SyntaxCheckResult(True, (), False)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            SyntaxCheckResult(-1, (), False)
        with self.assertRaises(TypeError):
            SyntaxCheckResult(0, [error], False)  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            SyntaxCheckResult(0, (ValueError("x"),), False)  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            SyntaxCheckResult(0, (), 0)  # type: ignore[arg-type]


class FormatTraceTests(unittest.TestCase):
    """测试 diagnostics.py 的 format_trace 稳定 JSON 转换规则。"""

    def test_token_matches_the_fixed_contract_fixture(self) -> None:
        # 测试 diagnostics.py 对 Token、TokenKind、SourceSpan、SourcePos 的
        # 递归转换，必须与工作计划定义的 token_trace_v1.json 保持一致。
        token = Token(TokenKind.KW_SELECT, "SELECT", None, _span())
        fixture_path = Path(__file__).parent / "fixtures" / "token_trace_v1.json"
        self.assertEqual(format_trace(token), fixture_path.read_text(encoding="utf-8").strip())

    def test_db_error_uses_public_fields_not_internal_state(self) -> None:
        error = DbError(
            ErrorStage.LEXICAL,
            INVALID_CHARACTER,
            "中文错误",
            _span(),
            {"character": "@"},
        )
        data = json.loads(format_trace(error))
        self.assertEqual(data["node_type"], "DbError")
        self.assertEqual(data["stage"], "LEXICAL")
        self.assertEqual(data["context"], {"character": "@"})
        self.assertNotIn("_sealed", data)

    def test_sorting_unicode_and_tuple_conversion_are_stable(self) -> None:
        value = {"中文": ("第二", "第一"), "a": True}
        self.assertEqual(format_trace(value), '{"a":true,"中文":["第二","第一"]}')

    def test_rejects_unsupported_or_cyclic_values(self) -> None:
        with self.assertRaises(TypeError):
            format_trace({"set": {1, 2}})
        cyclic: list[object] = []
        cyclic.append(cyclic)
        with self.assertRaises(ValueError):
            format_trace(cyclic)

    def test_dataclass_includes_its_type_name(self) -> None:
        @dataclass(frozen=True)
        class Example:
            value: int

        self.assertEqual(format_trace(Example(3)), '{"node_type":"Example","value":3}')
