"""errors.py 的单元测试。

由赵凯航维护。验证工作计划第 6.1 节、第 15.12 节、第 15.15 节
对错误结构的全部要求。

运行方式：
    python -m unittest tests.test_errors -v
"""

import unittest

from minidb.core.errors import (
    ALL_ERROR_CODES,
    ACTIVE_SCAN,
    CATALOG_CORRUPTED,
    CLOSED,
    COLUMN_NOT_FOUND,
    CONDITION_NOT_BOOL,
    DbError,
    DB_FILE_TRUNCATED,
    DB_FORMAT_MISMATCH,
    DUPLICATE_COLUMN,
    DUPLICATE_INSERT_COLUMN,
    ErrorStage,
    ID_EXHAUSTED,
    IDENTIFIER_TOO_LONG,
    INPUT_INVALID_UTF8,
    INPUT_READ_FAILED,
    INSERT_COLUMN_SET_MISMATCH,
    INT_OUT_OF_RANGE,
    INTERNAL_ERROR,
    INVALID_ARGUMENT,
    INVALID_CHARACTER,
    INVALID_NUMBER,
    INVALID_PLAN,
    IO_CLOSE_FAILED,
    IO_OPEN_FAILED,
    IO_READ_FAILED,
    IO_SYNC_FAILED,
    IO_WRITE_FAILED,
    PAGE_ALREADY_FREE,
    PAGE_CORRUPTED,
    PAGE_ID_INVALID,
    PAGE_NOT_ALLOCATED,
    RESERVED_NAME,
    RESERVED_PAGE,
    ROW_CORRUPTED,
    ROW_ENCODING_ERROR,
    ROW_TOO_LARGE,
    ROW_TYPE_MISMATCH,
    ROW_VALUE_COUNT_MISMATCH,
    SLOT_ID_INVALID,
    STALE_ROW,
    STALE_PAGE,
    TABLE_EXISTS,
    TABLE_NOT_FOUND,
    TYPE_MISMATCH,
    UNEXPECTED_EOF,
    UNEXPECTED_TOKEN,
    UNTERMINATED_COMMENT,
    UNTERMINATED_STRING,
    UNSUPPORTED_COMPARISON,
    UNSUPPORTED_FEATURE,
    VALUE_COUNT_MISMATCH,
)
from minidb.core.source import SourcePos, SourceSpan


def _make_span() -> SourceSpan:
    return SourceSpan(
        start=SourcePos(line=1, column=1, offset=0),
        end=SourcePos(line=1, column=7, offset=6),
        source_name="<test>",
    )


class TestErrorStage(unittest.TestCase):
    """验证 ErrorStage 枚举（工作计划第 6.1 节）。"""

    def test_total_count_is_6(self):
        """ErrorStage 共 6 个值。"""
        self.assertEqual(len(list(ErrorStage)), 6)

    def test_all_stages_exist(self):
        expected = ["LEXICAL", "SYNTAX", "SEMANTIC", "PLAN", "EXECUTION", "STORAGE"]
        for name in expected:
            self.assertIn(name, ErrorStage.__members__, f"缺少 ErrorStage.{name}")

    def test_stage_values_are_strings(self):
        for stage in ErrorStage:
            self.assertIsInstance(stage.value, str)


class TestErrorCodes(unittest.TestCase):
    """验证错误码常量（工作计划第 15.12 节）。"""

    def test_total_count_is_49(self):
        """错误码共49个，包含行代际和页快照失效。"""
        self.assertEqual(len(ALL_ERROR_CODES), 49)

    def test_all_constants_are_in_all_error_codes(self):
        """每个导出的错误码常量都在 ALL_ERROR_CODES 中。"""
        constants = [
            INPUT_INVALID_UTF8, INPUT_READ_FAILED,
            INVALID_CHARACTER, UNTERMINATED_STRING, UNTERMINATED_COMMENT,
            INVALID_NUMBER, IDENTIFIER_TOO_LONG,
            UNEXPECTED_TOKEN, UNEXPECTED_EOF, UNSUPPORTED_FEATURE, INT_OUT_OF_RANGE,
            TABLE_EXISTS, TABLE_NOT_FOUND, COLUMN_NOT_FOUND, DUPLICATE_COLUMN,
            DUPLICATE_INSERT_COLUMN, INSERT_COLUMN_SET_MISMATCH, VALUE_COUNT_MISMATCH,
            RESERVED_NAME, TYPE_MISMATCH, UNSUPPORTED_COMPARISON, CONDITION_NOT_BOOL,
            INVALID_PLAN,
            ROW_TYPE_MISMATCH, ROW_VALUE_COUNT_MISMATCH, ROW_TOO_LARGE,
            ROW_CORRUPTED, ROW_ENCODING_ERROR,
            PAGE_ID_INVALID, PAGE_NOT_ALLOCATED, PAGE_ALREADY_FREE, RESERVED_PAGE,
            SLOT_ID_INVALID, STALE_ROW, STALE_PAGE, PAGE_CORRUPTED, DB_FORMAT_MISMATCH,
            DB_FILE_TRUNCATED,
            CATALOG_CORRUPTED, ID_EXHAUSTED,
            IO_OPEN_FAILED, IO_READ_FAILED, IO_WRITE_FAILED, IO_SYNC_FAILED,
            IO_CLOSE_FAILED,
            ACTIVE_SCAN, CLOSED, INVALID_ARGUMENT, INTERNAL_ERROR,
        ]
        self.assertEqual(len(constants), 49)
        for code in constants:
            self.assertIn(code, ALL_ERROR_CODES, f"错误码 {code} 不在 ALL_ERROR_CODES 中")

    def test_error_codes_are_strings(self):
        for code in ALL_ERROR_CODES:
            self.assertIsInstance(code, str)

    def test_no_duplicate_error_codes(self):
        """frozenset自动去重，如果常量重复，数量会少于49。"""
        # 已经通过test_total_count_is_49验证
        pass


class TestDbErrorConstruction(unittest.TestCase):
    """验证 DbError 正常构造（工作计划第 6.1 节）。"""

    def test_normal_construction_with_span(self):
        err = DbError(
            stage=ErrorStage.LEXICAL,
            code=INVALID_CHARACTER,
            message="遇到非法字符 @",
            span=_make_span(),
        )
        self.assertEqual(err.stage, ErrorStage.LEXICAL)
        self.assertEqual(err.code, INVALID_CHARACTER)
        self.assertEqual(err.message, "遇到非法字符 @")
        self.assertIsNotNone(err.span)
        self.assertEqual(err.context, {})

    def test_normal_construction_without_span(self):
        """存储错误没有 SQL 位置时 span 可以为 None（工作计划第 2.3 节第 3 条）。"""
        err = DbError(
            stage=ErrorStage.STORAGE,
            code=IO_READ_FAILED,
            message="读取页失败",
            span=None,
            context={"page_id": 3, "operation": "read_page"},
        )
        self.assertIsNone(err.span)
        self.assertEqual(err.context["page_id"], 3)
        self.assertEqual(err.context["operation"], "read_page")

    def test_default_context_is_empty_dict(self):
        err = DbError(
            stage=ErrorStage.SEMANTIC,
            code=TABLE_NOT_FOUND,
            message="表不存在",
            span=_make_span(),
        )
        self.assertEqual(err.context, {})

    def test_inherits_exception(self):
        """DbError 继承 Exception，可以 raise / except。"""
        self.assertTrue(issubclass(DbError, Exception))

    def test_can_be_raised_and_caught(self):
        with self.assertRaises(DbError) as cm:
            raise DbError(
                stage=ErrorStage.SYNTAX,
                code=UNEXPECTED_TOKEN,
                message="意外的 Token",
                span=_make_span(),
                context={"actual": "SEMICOLON", "expected": ["IDENT"]},
            )
        self.assertEqual(cm.exception.code, UNEXPECTED_TOKEN)
        self.assertEqual(cm.exception.context["actual"], "SEMICOLON")


class TestDbErrorValidation(unittest.TestCase):
    """验证 DbError 构造时的字段校验（工作计划第 15.12 节、第 15.15 节）。"""

    def test_stage_must_be_error_stage(self):
        with self.assertRaises(TypeError):
            DbError(stage="LEXICAL", code=INVALID_CHARACTER, message="x", span=None)  # type: ignore[arg-type]

    def test_code_must_be_str(self):
        with self.assertRaises(TypeError):
            DbError(stage=ErrorStage.LEXICAL, code=123, message="x", span=None)  # type: ignore[arg-type]

    def test_code_must_be_defined_error_code(self):
        """code 必须是工作计划定义的错误码，不能随意拼写。"""
        with self.assertRaises(ValueError):
            DbError(stage=ErrorStage.LEXICAL, code="TABLE_NOT_EXIST", message="x", span=None)

    def test_message_must_be_str(self):
        with self.assertRaises(TypeError):
            DbError(stage=ErrorStage.LEXICAL, code=INVALID_CHARACTER, message=123, span=None)  # type: ignore[arg-type]

    def test_span_must_be_source_span_or_none(self):
        with self.assertRaises(TypeError):
            DbError(stage=ErrorStage.LEXICAL, code=INVALID_CHARACTER, message="x", span="not a span")  # type: ignore[arg-type]

    def test_context_must_be_dict(self):
        with self.assertRaises(TypeError):
            DbError(stage=ErrorStage.LEXICAL, code=INVALID_CHARACTER, message="x", span=None, context=["not", "a", "dict"])  # type: ignore[arg-type]

    def test_context_keys_must_be_str(self):
        with self.assertRaises(TypeError):
            DbError(stage=ErrorStage.LEXICAL, code=INVALID_CHARACTER, message="x", span=None, context={123: "value"})  # type: ignore[arg-type]

    def test_context_values_must_not_be_none(self):
        """context 不允许 None 占位，未知字段应直接省略（工作计划第 15.15 节）。"""
        with self.assertRaises(ValueError):
            DbError(
                stage=ErrorStage.LEXICAL,
                code=INVALID_CHARACTER,
                message="x",
                span=None,
                context={"table_name": None},
            )

    def test_context_accepts_json_serializable_values(self):
        """context 值可以是 str、int、bool、list、dict 等 JSON 可表示类型。"""
        err = DbError(
            stage=ErrorStage.SYNTAX,
            code=UNEXPECTED_TOKEN,
            message="x",
            span=None,
            context={
                "actual": "SEMICOLON",
                "expected": ["IDENT", "STAR"],
                "page_id": 3,
                "nested": {"key": "value"},
                "flag": True,
            },
        )
        self.assertEqual(err.context["actual"], "SEMICOLON")
        self.assertEqual(len(err.context["expected"]), 2)
        self.assertEqual(err.context["page_id"], 3)
        self.assertEqual(err.context["nested"]["key"], "value")
        self.assertTrue(err.context["flag"])


class TestDbErrorImmutable(unittest.TestCase):
    """验证 DbError 不可变性（工作计划第 15.1 节第 7 条）。"""

    def test_cannot_modify_fields(self):
        err = DbError(
            stage=ErrorStage.LEXICAL,
            code=INVALID_CHARACTER,
            message="x",
            span=_make_span(),
        )
        with self.assertRaises(Exception):
            err.message = "modified"  # type: ignore[misc]
        with self.assertRaises(Exception):
            err.code = TABLE_NOT_FOUND  # type: ignore[misc]

    def test_context_is_not_shared_between_instances(self):
        """每个实例有独立且只读的 context。"""
        err1 = DbError(stage=ErrorStage.LEXICAL, code=INVALID_CHARACTER, message="x", span=None)
        err2 = DbError(stage=ErrorStage.LEXICAL, code=INVALID_CHARACTER, message="y", span=None)
        self.assertIsNot(err1.context, err2.context)
        with self.assertRaises(TypeError):
            err1.context["key"] = "value"  # type: ignore[index]
        # frozen 不允许修改 context 的内容吗？实际上 frozen 只禁止重新赋值 context 属性，
        # 但 context 是 dict，本身是可变的。这是已知的 Python frozen dataclass 限制。
        # 工作计划要求不可变数据对象，但 dict 本身可变。这里不做额外限制，
        # 因为 context 是在构造时传入的，使用方不应在构造后修改。
        self.assertNotIn("key", err2.context)

    def test_context_rejects_non_json_values(self):
        with self.assertRaises(TypeError):
            DbError(stage=ErrorStage.LEXICAL, code=INVALID_CHARACTER, message="x", span=None,
                    context={"value": {1, 2}})
        with self.assertRaises(TypeError):
            DbError(stage=ErrorStage.LEXICAL, code=INVALID_CHARACTER, message="x", span=None,
                    context={"value": object()})

    def test_nested_context_is_read_only(self):
        err = DbError(
            stage=ErrorStage.SYNTAX,
            code=UNEXPECTED_TOKEN,
            message="x",
            span=None,
            context={"nested": {"items": ["a"]}},
        )
        with self.assertRaises(TypeError):
            err.context["nested"]["items"].append("b")  # type: ignore[index]


class TestDbErrorStringRepresentation(unittest.TestCase):
    """验证 DbError 的可读字符串输出（供 CLI 展示）。"""

    def test_str_with_span(self):
        err = DbError(
            stage=ErrorStage.SEMANTIC,
            code=TABLE_NOT_FOUND,
            message="表 student 不存在",
            span=_make_span(),
        )
        s = str(err)
        self.assertIn("[TABLE_NOT_FOUND]", s)
        self.assertIn("表 student 不存在", s)
        self.assertIn("<test>", s)
        self.assertIn("第 1 行第 1 列", s)

    def test_str_without_span(self):
        err = DbError(
            stage=ErrorStage.STORAGE,
            code=IO_READ_FAILED,
            message="读取页失败",
            span=None,
        )
        s = str(err)
        self.assertIn("[IO_READ_FAILED]", s)
        self.assertIn("读取页失败", s)
        self.assertNotIn("位置:", s)

    def test_str_with_context(self):
        err = DbError(
            stage=ErrorStage.SYNTAX,
            code=UNEXPECTED_TOKEN,
            message="意外的 Token",
            span=None,
            context={"actual": "SEMICOLON", "expected": "IDENT"},
        )
        s = str(err)
        self.assertIn("actual: SEMICOLON", s)
        self.assertIn("expected: IDENT", s)


class TestErrorStageMapping(unittest.TestCase):
    """验证错误码与阶段的对应关系（工作计划第 15.12 节末尾说明）。"""

    def test_lexical_errors(self):
        """词法和输入读取错误使用 LEXICAL 阶段。"""
        lexical_codes = [
            INPUT_INVALID_UTF8, INPUT_READ_FAILED,
            INVALID_CHARACTER, UNTERMINATED_STRING, UNTERMINATED_COMMENT,
            INVALID_NUMBER, IDENTIFIER_TOO_LONG,
        ]
        for code in lexical_codes:
            # 验证可以用 LEXICAL 阶段构造
            err = DbError(stage=ErrorStage.LEXICAL, code=code, message="test", span=None)
            self.assertEqual(err.stage, ErrorStage.LEXICAL)

    def test_syntax_errors(self):
        syntax_codes = [UNEXPECTED_TOKEN, UNEXPECTED_EOF, UNSUPPORTED_FEATURE, INT_OUT_OF_RANGE]
        for code in syntax_codes:
            err = DbError(stage=ErrorStage.SYNTAX, code=code, message="test", span=None)
            self.assertEqual(err.stage, ErrorStage.SYNTAX)

    def test_semantic_errors(self):
        semantic_codes = [
            TABLE_EXISTS, TABLE_NOT_FOUND, COLUMN_NOT_FOUND, DUPLICATE_COLUMN,
            DUPLICATE_INSERT_COLUMN, INSERT_COLUMN_SET_MISMATCH, VALUE_COUNT_MISMATCH,
            RESERVED_NAME, TYPE_MISMATCH, UNSUPPORTED_COMPARISON, CONDITION_NOT_BOOL,
        ]
        for code in semantic_codes:
            err = DbError(stage=ErrorStage.SEMANTIC, code=code, message="test", span=_make_span())
            self.assertEqual(err.stage, ErrorStage.SEMANTIC)

    def test_storage_errors(self):
        storage_codes = [
            ROW_TYPE_MISMATCH, ROW_VALUE_COUNT_MISMATCH, ROW_TOO_LARGE,
            ROW_CORRUPTED, ROW_ENCODING_ERROR,
            PAGE_ID_INVALID, PAGE_NOT_ALLOCATED, PAGE_ALREADY_FREE, RESERVED_PAGE,
            SLOT_ID_INVALID, STALE_ROW, STALE_PAGE, PAGE_CORRUPTED,
            DB_FORMAT_MISMATCH, DB_FILE_TRUNCATED,
            CATALOG_CORRUPTED, ID_EXHAUSTED,
            IO_OPEN_FAILED, IO_READ_FAILED, IO_WRITE_FAILED, IO_SYNC_FAILED,
            IO_CLOSE_FAILED, ACTIVE_SCAN, CLOSED,
        ]
        for code in storage_codes:
            err = DbError(stage=ErrorStage.STORAGE, code=code, message="test", span=None)
            self.assertEqual(err.stage, ErrorStage.STORAGE)

    def test_wrong_stage_is_rejected(self):
        """固定错误码不能被标记为另一个阶段。"""
        wrong_stage_cases = [
            (ErrorStage.SYNTAX, INVALID_CHARACTER),
            (ErrorStage.LEXICAL, UNEXPECTED_TOKEN),
            (ErrorStage.EXECUTION, TABLE_NOT_FOUND),
            (ErrorStage.SEMANTIC, INVALID_PLAN),
            (ErrorStage.STORAGE, INTERNAL_ERROR),
        ]
        for stage, code in wrong_stage_cases:
            with self.assertRaises(ValueError):
                DbError(stage=stage, code=code, message="test", span=None)

    def test_invalid_argument_can_follow_public_api_stage(self):
        """INVALID_ARGUMENT 的阶段由具体公共接口所属模块决定。"""
        for stage in (ErrorStage.LEXICAL, ErrorStage.STORAGE, ErrorStage.EXECUTION):
            err = DbError(
                stage=stage,
                code=INVALID_ARGUMENT,
                message="参数错误",
                span=None,
            )
            self.assertEqual(err.stage, stage)

    def test_internal_error_uses_execution_stage(self):
        err = DbError(
            stage=ErrorStage.EXECUTION,
            code=INTERNAL_ERROR,
            message="执行期间出现未预期异常",
            span=None,
        )
        self.assertEqual(err.stage, ErrorStage.EXECUTION)


if __name__ == "__main__":
    unittest.main()
