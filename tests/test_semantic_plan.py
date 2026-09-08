"""张振的语义与计划测试，分开记录独立逻辑测试和正式接口对接测试。

独立测试只拦截张振自己的错误/依赖边界；Mock 不实现 AST 或公共类型。
完整 analyze → build 测试只使用正式 AST、SourceSpan、ResultColumn、DbError，
依赖未交付时跳过，不能把独立测试视为已经完成编译链对接。
"""

import importlib.util
import unittest
from dataclasses import replace
from unittest.mock import Mock, patch

from fixtures.contracts import STUDENT_SCHEMA, STUDENT_TABLE
from fixtures.semantic_cases import create_case, delete_case, insert_case, select_case
from minidb.catalog.catalog import Catalog
from minidb.compiler.bound import BoundDelete, BoundInsert, BoundLiteral, BoundSelect
from minidb.compiler.plan import DeletePlan, FilterPlan, ProjectPlan, SeqScanPlan
from minidb.compiler.planner import Planner
from minidb.compiler.semantic import Semantic, _name, _operation_type
from minidb.core.expressions import ExprOp
from minidb.core.schema import DataType


class SemanticLogicTests(unittest.TestCase):
    """在正式 AST 可用前，先验证张振私有方法中的业务规则。"""

    def setUp(self):
        """Mock 仅提供被测私有方法需要的属性，未通过正式 AST 结构校验。"""
        self.semantic = Semantic()
        self.stmt = Mock(
            span=None,
            columns=tuple(Mock(text=name, span=None) for name in ("name", "age", "id")),
            values=(Mock(value="Alice", data_type=DataType.VARCHAR, span=None),
                    Mock(value=20, data_type=DataType.INT, span=None),
                    Mock(value=1, data_type=DataType.INT, span=None)),
        )
        # 单独隔离关键字查询，避免复制 TokenKind；正式枚举行为留给对接测试。
        keyword = patch("minidb.compiler.semantic._is_keyword", return_value=False)
        keyword.start()
        self.addCleanup(keyword.stop)

    def assert_semantic_error(self, action, code):
        """只检查是否走到本模块规定的错误出口，正式 DbError 另测。"""
        with patch("minidb.compiler.semantic._error", side_effect=RuntimeError("语义拒绝")) as report:
            with self.assertRaises(RuntimeError):
                action()
        self.assertEqual(report.call_args.args[0], code)
        return report.call_args

    def test_insert_reorders_by_schema_and_preserves_string(self):
        """(name,age,id) 变成 (id,name,age)，字符串内的中文、换行及引号保持原样。"""
        self.stmt.values[0].value = "张三\nA;B's"
        bound = self.semantic._insert(self.stmt, STUDENT_TABLE)
        self.assertEqual(bound.row, (1, "张三\nA;B's", 20))
        self.assertIs(bound.table, STUDENT_TABLE)

    def test_insert_count_checked_before_column_lookup(self):
        """值数不匹配应先报错，即使列名本身也不存在。"""
        self.stmt.columns = (Mock(text="missing", span=None),)
        self.assert_semantic_error(lambda: self.semantic._insert(self.stmt, STUDENT_TABLE), "VALUE_COUNT_MISMATCH")

    def test_insert_duplicate_missing_and_incomplete_columns(self):
        """重复列、不存在列、缺必需列按规划给出不同错误码。"""
        for names, code in ((["id", "id", "age"], "DUPLICATE_INSERT_COLUMN"),
                            (["name", "missing", "id"], "COLUMN_NOT_FOUND"),
                            (["name", "age"], "INSERT_COLUMN_SET_MISMATCH")):
            with self.subTest(names=names):
                self.stmt.columns = tuple(Mock(text=name, span=None) for name in names)
                self.stmt.values = self.stmt.values[:len(names)]
                args = self.assert_semantic_error(lambda: self.semantic._insert(self.stmt, STUDENT_TABLE), code)
                if code == "INSERT_COLUMN_SET_MISMATCH":
                    self.assertEqual(args.kwargs["missing_columns"], ["id"])

    def test_insert_mismatch_points_to_value(self):
        """类型不匹配定位到具体字面量，而不是语句开头。"""
        marker = object()
        self.stmt.values[0].span = marker
        self.stmt.values[0].data_type = DataType.INT
        args = self.assert_semantic_error(lambda: self.semantic._insert(self.stmt, STUDENT_TABLE), "TYPE_MISMATCH")
        self.assertIs(args.args[2], marker)
        self.assertEqual(args.kwargs["column_name"], "name")

    def test_create_builds_schema_without_allocating_identity(self):
        """CREATE 只生成列结构，不在语义阶段创建 TableRef、分配页或登记表。"""
        stmt = Mock(span=None, columns=tuple(
            Mock(name=Mock(), data_type=column.data_type, type_span=None, span=None)
            for column in STUDENT_SCHEMA.columns
        ))
        # Mock 的 name 参数有特殊用途，因此明确给业务字段赋值。
        for declaration, column in zip(stmt.columns, STUDENT_SCHEMA.columns):
            declaration.name = Mock(text=column.name.upper(), span=None)
        bound = self.semantic._create(stmt, "student")
        self.assertEqual(bound.schema, STUDENT_SCHEMA)
        self.assertFalse(hasattr(bound, "table"))

    def test_create_column_limits_and_duplicates(self):
        """先拒绝列数越界，重复列则定位到第二个声明。"""
        for count in (0, 65):
            stmt = Mock(columns=(None,) * count, span=None)
            self.assert_semantic_error(lambda: self.semantic._create(stmt, "t"), "UNSUPPORTED_FEATURE")
        column = Mock(data_type=DataType.INT)
        column.name = Mock(text="ID", span=None)
        stmt = Mock(columns=(column, column), span=None)
        self.assert_semantic_error(lambda: self.semantic._create(stmt, "t"), "DUPLICATE_COLUMN")

    def test_identifier_normalization_and_reserved_table_prefix(self):
        """只把名字转为小写；超长、非法格式和系统表前缀分别拒绝。"""
        self.assertEqual(_name("Student", None), "student")
        self.assertEqual(_name("_sys_value", None), "_sys_value")
        for name, code in (("x" * 65, "IDENTIFIER_TOO_LONG"), (" student", "INVALID_ARGUMENT"),
                           ("_SYS_catalog", "RESERVED_NAME")):
            self.assert_semantic_error(lambda: _name(name, None, table=True), code)

    def test_keyword_is_rejected_when_frontend_reports_it(self):
        """前端报告某名称是关键字时，语义阶段不能把它当普通列名。"""
        with patch("minidb.compiler.semantic._is_keyword", return_value=True):
            self.assert_semantic_error(lambda: _name("SELECT", None), "RESERVED_NAME")

    def test_operator_error_codes_and_position(self):
        """字符串顺序比较报 UNSUPPORTED_COMPARISON，跨类型相等报 TYPE_MISMATCH。"""
        integer = BoundLiteral(18, DataType.INT, None)
        string = BoundLiteral("18", DataType.VARCHAR, None)
        self.assertIs(_operation_type(ExprOp.GE, (integer, integer), None), DataType.BOOL)
        for op, operands, code in ((ExprOp.GT, (string, string), "UNSUPPORTED_COMPARISON"),
                                   (ExprOp.EQ, (integer, string), "TYPE_MISMATCH"),
                                   (ExprOp.NOT, (integer,), "TYPE_MISMATCH")):
            marker = object()
            args = self.assert_semantic_error(lambda: _operation_type(op, operands, marker), code)
            self.assertIs(args.args[2], marker)
            self.assertEqual(args.kwargs["operator"], op.name)

    def test_where_requires_bool_after_expression_binding(self):
        """没有 WHERE 返回 None，裸整数 WHERE 在表达式绑定完成后被拒绝。"""
        self.assertIsNone(self.semantic._where(None, STUDENT_TABLE))
        with patch.object(self.semantic, "_expression", return_value=BoundLiteral(18, DataType.INT, None)):
            self.assert_semantic_error(lambda: self.semantic._where(Mock(span=None), STUDENT_TABLE), "CONDITION_NOT_BOOL")

    def test_planner_node_order_without_external_metadata_validation(self):
        """单独检查树形拼装；这里隔离校验器，不宣称这些占位位置通过正式校验。"""
        predicate = BoundLiteral(True, DataType.BOOL, None)
        selected = BoundSelect(STUDENT_TABLE, (1,), (), predicate, None)
        with patch("minidb.compiler.planner.validate_bound"), patch("minidb.compiler.planner.validate_plan"):
            plan = Planner().build(selected)
            deletion = Planner().build(BoundDelete(STUDENT_TABLE, predicate, None))
            plain = Planner().build(replace(selected, predicate=None))
            inserted = Planner().build(BoundInsert(STUDENT_TABLE, (1, "Alice", 20), None))
        self.assertIsInstance(plan, ProjectPlan)
        self.assertIsInstance(plan.child, FilterPlan)
        self.assertIsInstance(plan.child.child, SeqScanPlan)
        self.assertIsInstance(deletion, DeletePlan)
        self.assertIsInstance(deletion.child, FilterPlan)
        self.assertIsInstance(plain.child, SeqScanPlan)
        self.assertEqual(inserted.row, (1, "Alice", 20))


# 这里只检查模块是否存在，不捕获模块内部导入错误，以免把损坏依赖误报为跳过。
_MISSING = [name for name in (
    "minidb.compiler.ast", "minidb.core.source", "minidb.core.tokens",
    "minidb.core.result", "minidb.core.errors",
) if importlib.util.find_spec(name) is None]


@unittest.skipIf(bool(_MISSING), "等待正式接口：" + ", ".join(_MISSING))
class SemanticPlanContractTests(unittest.TestCase):
    """只使用正式公共类，接入依赖后自动验证完整语义与计划链。"""

    def setUp(self):
        """新建只读目录；语义与计划阶段都不应改变它。"""
        self.semantic = Semantic()
        self.catalog = Catalog((STUDENT_TABLE,))

    def test_four_statement_contracts(self):
        """四类真实 AST 均能生成相应 Bound 和合法计划，并保持目录不变。"""
        for build in (create_case, insert_case, select_case, delete_case):
            with self.subTest(case=build.__name__):
                bound = self.semantic.analyze(build(), self.catalog)
                plan = Planner().build(bound)
                self.assertEqual(plan.span, bound.span)
        self.assertEqual(self.catalog.list_tables(), [STUDENT_TABLE])

    def test_insert_reordered_row(self):
        """正式 AST 的列重排结果与独立手写预期一致。"""
        bound = self.semantic.analyze(insert_case(), self.catalog)
        self.assertEqual(bound.row, (1, "Alice", 20))

    def test_filter_sees_unprojected_age_column(self):
        """只输出 name 时，Filter 仍从完整行的第 2 个索引读取 age。"""
        bound = self.semantic.analyze(select_case(), self.catalog)
        plan = Planner().build(bound)
        self.assertEqual(plan.column_indexes, (1,))
        self.assertEqual(plan.child.predicate.left.index, 2)
        self.assertIsInstance(plan.child.child, SeqScanPlan)

    def test_star_and_duplicate_projection(self):
        """SELECT * 展开全部列，重复选择列保持重复输出元数据。"""
        stmt = select_case()
        star = self.semantic.analyze(replace(stmt, select_all=True, columns=(), where=None), self.catalog)
        repeated = self.semantic.analyze(replace(stmt, columns=stmt.columns * 2, where=None), self.catalog)
        self.assertEqual(star.projection, (0, 1, 2))
        self.assertEqual(repeated.projection, (1, 1))
        self.assertEqual([column.name for column in repeated.output_columns], ["name", "name"])

    def test_unknown_column_has_its_own_span(self):
        """列不存在时错误指向该 IdentifierExpr 的位置。"""
        from minidb.core.errors import COLUMN_NOT_FOUND, DbError, ErrorStage

        stmt = select_case()
        missing = replace(stmt.where.left, name="missing")
        with self.assertRaises(DbError) as raised:
            self.semantic.analyze(replace(stmt, where=replace(stmt.where, left=missing)), self.catalog)
        self.assertEqual(raised.exception.code, COLUMN_NOT_FOUND)
        self.assertIs(raised.exception.stage, ErrorStage.SEMANTIC)
        self.assertEqual(raised.exception.span, missing.span)

    def test_invalid_ast_is_rejected_before_catalog_lookup(self):
        """select_all 类型错误属于结构问题，在查询目录之前拒绝。"""
        from minidb.core.errors import DbError, INVALID_ARGUMENT

        catalog = Mock(spec=["find_table", "list_tables"])
        with self.assertRaises(DbError) as raised:
            self.semantic.analyze(replace(select_case(), select_all=1), catalog)
        self.assertEqual(raised.exception.code, INVALID_ARGUMENT)
        catalog.find_table.assert_not_called()


if __name__ == "__main__":
    unittest.main()
