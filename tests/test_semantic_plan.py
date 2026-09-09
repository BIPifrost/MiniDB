"""张振的语义与计划测试，使用正式 AST、位置、Token 和公共错误接口。"""

import unittest
from dataclasses import replace
from unittest.mock import Mock, patch

from fixtures.contracts import STUDENT_SCHEMA, STUDENT_TABLE, span
from fixtures.semantic_cases import create_case, delete_case, insert_case, select_case
from minidb.catalog.catalog import Catalog
from minidb.compiler.ast import ColumnDecl, CreateTableStmt, NameRef
from minidb.compiler.bound import BoundDelete, BoundInsert, BoundLiteral, BoundSelect, BoundUnary
from minidb.compiler.plan import DeletePlan, FilterPlan, ProjectPlan, SeqScanPlan
from minidb.compiler.planner import Planner
from minidb.compiler.semantic import Semantic, _name, _operation_type
from minidb.core.errors import DbError, ErrorStage, COLUMN_NOT_FOUND, TYPE_MISMATCH
from minidb.core.expressions import ExprOp
from minidb.core.result import ResultColumn
from minidb.core.schema import DataType


class SemanticLogicTests(unittest.TestCase):
    """集中验证值重排、名称检查、错误位置和简单计划结构。"""

    def setUp(self):
        """直接复用正式 INSERT 样例，不再模拟 AST 和关键字接口。"""
        self.semantic = Semantic()
        self.stmt = insert_case()

    def assert_semantic_error(self, action, code):
        """错误模块已经接通，直接断言真实异常，不再替换语义报错函数。"""
        with self.assertRaises(DbError) as raised:
            action()
        self.assertEqual(raised.exception.code, code)
        self.assertIs(raised.exception.stage, ErrorStage.SEMANTIC)
        self.assertEqual(raised.exception.context["operation"], "Semantic.analyze")
        return raised.exception

    def test_insert_reorders_by_schema_and_preserves_string(self):
        """(name,age,id) 变成 (id,name,age)，字符串内的中文、换行及引号保持原样。"""
        stmt = replace(self.stmt, values=(replace(self.stmt.values[0], value="张三\nA;B's"), *self.stmt.values[1:]))
        bound = self.semantic.analyze(stmt, Catalog((STUDENT_TABLE,)))
        self.assertEqual(bound.row, (1, "张三\nA;B's", 20))
        self.assertIs(bound.table, STUDENT_TABLE)

    def test_insert_count_checked_before_column_lookup(self):
        """值数不匹配应先报错，即使列名本身也不存在。"""
        stmt = replace(self.stmt, columns=(replace(self.stmt.columns[0], text="missing"),))
        self.assert_semantic_error(lambda: self.semantic.analyze(stmt, Catalog((STUDENT_TABLE,))), "VALUE_COUNT_MISMATCH")

    def test_insert_duplicate_missing_and_incomplete_columns(self):
        """重复列、不存在列、缺必需列按规划给出不同错误码。"""
        for names, code in ((["id", "id", "age"], "DUPLICATE_INSERT_COLUMN"),
                            (["name", "missing", "id"], "COLUMN_NOT_FOUND"),
                            (["name", "age"], "INSERT_COLUMN_SET_MISMATCH")):
            with self.subTest(names=names):
                stmt = replace(self.stmt,
                               columns=tuple(replace(column, text=name) for column, name in zip(self.stmt.columns, names)),
                               values=self.stmt.values[:len(names)])
                error = self.assert_semantic_error(lambda: self.semantic.analyze(stmt, Catalog((STUDENT_TABLE,))), code)
                if code == "INSERT_COLUMN_SET_MISMATCH":
                    self.assertEqual(error.context["missing_columns"], ["id"])

    def test_insert_mismatch_points_to_value(self):
        """类型不匹配定位到具体字面量，而不是语句开头。"""
        marker = self.stmt.values[0].span
        stmt = replace(self.stmt, values=(replace(self.stmt.values[0], value=123, data_type=DataType.INT), *self.stmt.values[1:]))
        error = self.assert_semantic_error(lambda: self.semantic.analyze(stmt, Catalog((STUDENT_TABLE,))), "TYPE_MISMATCH")
        self.assertIs(error.span, marker)
        self.assertEqual(error.context["column_name"], "name")

    def test_create_builds_schema_without_allocating_identity(self):
        """CREATE 只生成列结构，不在语义阶段创建 TableRef、分配页或登记表。"""
        location = span("CREATE")
        stmt = CreateTableStmt(NameRef("Student", location), tuple(
            ColumnDecl(NameRef(column.name.upper(), location), column.data_type, location, location)
            for column in STUDENT_SCHEMA.columns
        ), location)
        catalog = Catalog()
        bound = self.semantic.analyze(stmt, catalog)
        self.assertEqual(bound.schema, STUDENT_SCHEMA)
        self.assertFalse(hasattr(bound, "table"))
        self.assertEqual(catalog.list_tables(), [])

    def test_create_column_limits_and_duplicates(self):
        """先拒绝列数越界，重复列则定位到第二个声明。"""
        for count in (0, 65):
            sample = create_case()
            stmt = replace(sample, columns=(sample.columns[0],) * count)
            self.assert_semantic_error(lambda: self.semantic.analyze(stmt, Catalog()), "UNSUPPORTED_FEATURE")
        stmt = replace(sample, columns=(sample.columns[0], sample.columns[0]))
        self.assert_semantic_error(lambda: self.semantic.analyze(stmt, Catalog()), "DUPLICATE_COLUMN")

    def test_identifier_normalization_and_reserved_table_prefix(self):
        """只把名字转为小写；超长、非法格式和系统表前缀分别拒绝。"""
        self.assertEqual(_name("Student", None), "student")
        self.assertEqual(_name("_sys_value", None), "_sys_value")
        for name, code in (("x" * 65, "IDENTIFIER_TOO_LONG"), (" student", "INVALID_ARGUMENT"),
                           ("_SYS_catalog", "RESERVED_NAME")):
            self.assert_semantic_error(lambda: _name(name, None, table=True), code)

    def test_keyword_is_rejected_using_formal_token_kinds(self):
        """语义名称检查复用正式 TokenKind，不能把 SELECT 当普通列名。"""
        self.assert_semantic_error(lambda: _name("SELECT", span("SELECT")), "RESERVED_NAME")

    def test_manual_ast_long_name_reports_semantic_error_before_lookup(self):
        """绕过 Lexer 构造 AST 时，语义入口仍复核名字长度并使用真实错误位置。"""
        name = "x" * 65
        sql = f"CREATE TABLE {name}(id INT);"
        location = span(sql, "id INT")
        stmt = CreateTableStmt(NameRef(name, span(sql, name)),
                               (ColumnDecl(NameRef("id", span(sql, "id")), DataType.INT,
                                           span(sql, "INT"), location),), span(sql))
        catalog = Catalog()
        with patch.object(Catalog, "find_table") as lookup:
            error = self.assert_semantic_error(lambda: self.semantic.analyze(stmt, catalog), "IDENTIFIER_TOO_LONG")
            lookup.assert_not_called()
        self.assertEqual(error.span, stmt.table_name.span)

    def test_operator_error_codes_and_position(self):
        """字符串顺序比较报 UNSUPPORTED_COMPARISON，跨类型相等报 TYPE_MISMATCH。"""
        integer = BoundLiteral(18, DataType.INT, None)
        string = BoundLiteral("18", DataType.VARCHAR, None)
        self.assertIs(_operation_type(ExprOp.GE, (integer, integer), None), DataType.BOOL)
        for op, operands, code in ((ExprOp.GT, (string, string), "UNSUPPORTED_COMPARISON"),
                                   (ExprOp.EQ, (integer, string), "TYPE_MISMATCH"),
                                   (ExprOp.NOT, (integer,), "TYPE_MISMATCH")):
            marker = span(op.name)
            error = self.assert_semantic_error(lambda: _operation_type(op, operands, marker), code)
            self.assertIs(error.span, marker)
            self.assertEqual(error.context["operator"], op.name)

    def test_where_requires_bool_after_expression_binding(self):
        """没有 WHERE 返回 None，裸整数 WHERE 在表达式绑定完成后被拒绝。"""
        self.assertIsNone(self.semantic._where(None, STUDENT_TABLE))
        stmt = replace(select_case(), where=select_case().where.right)
        self.assert_semantic_error(lambda: self.semantic.analyze(stmt, Catalog((STUDENT_TABLE,))), "CONDITION_NOT_BOOL")

    def test_order_comparison_error_expects_two_integers(self):
        """顺序比较只支持整数；拒绝字符串时，错误详情也不能提示 VARCHAR 可用。"""
        operands = (BoundLiteral("18", DataType.VARCHAR, None),
                    BoundLiteral("20", DataType.VARCHAR, None))
        for op in (ExprOp.LT, ExprOp.LE, ExprOp.GT, ExprOp.GE):
            with self.subTest(operator=op.name):
                error = self.assert_semantic_error(
                    lambda: _operation_type(op, operands, None), "UNSUPPORTED_COMPARISON",
                )
                self.assertEqual(error.context["expected"], ["INT", "INT"])
                self.assertEqual(error.context["actual"], ["VARCHAR", "VARCHAR"])

    def test_planner_node_order_with_formal_positions(self):
        """检查扫描、过滤、投影和删除的装配顺序，不替换位置检查。"""
        location = span("SELECT")
        predicate = BoundLiteral(True, DataType.BOOL, location)
        selected = BoundSelect(STUDENT_TABLE, (1,), (ResultColumn("name", DataType.VARCHAR),), predicate, location)
        plan = Planner().build(selected)
        deletion = Planner().build(BoundDelete(STUDENT_TABLE, predicate, location))
        plain = Planner().build(replace(selected, predicate=None))
        inserted = Planner().build(BoundInsert(STUDENT_TABLE, (1, "Alice", 20), location))
        self.assertIsInstance(plan, ProjectPlan)
        self.assertIsInstance(plan.child, FilterPlan)
        self.assertIsInstance(plan.child.child, SeqScanPlan)
        self.assertIsInstance(deletion, DeletePlan)
        self.assertIsInstance(deletion.child, FilterPlan)
        self.assertIsInstance(plain.child, SeqScanPlan)
        self.assertEqual(inserted.row, (1, "Alice", 20))


class SemanticPlanContractTests(unittest.TestCase):
    """只使用正式公共类，验证完整语义与计划链。"""

    def setUp(self):
        """新建只读目录；语义与计划阶段都不应改变它。"""
        self.semantic = Semantic()
        self.catalog = Catalog((STUDENT_TABLE,))

    def _select_with_where(self, sql, where):
        """用正式 AST 类组装测试语句，每个位置都来自本条 SQL。"""
        from minidb.compiler.ast import NameRef, SelectStmt

        return SelectStmt(NameRef("student", span(sql, "student")), False,
                          (NameRef("name", span(sql, "name")),), where, span(sql))

    def test_short_circuit_constants_do_not_hide_right_branch_errors(self):
        """运行时可短路的 AND/OR，在语义阶段仍须报告右侧的缺列和类型错误。"""
        from minidb.compiler.ast import BinaryExpr, IdentifierExpr, LiteralExpr

        for op, value in ((ExprOp.AND, 0), (ExprOp.OR, 1)):
            for right_sql, code in (("missing_col = 1", COLUMN_NOT_FOUND),
                                    ("age = 'old'", TYPE_MISMATCH)):
                with self.subTest(op=op, right=right_sql):
                    left_sql = f"1 = {value}"
                    condition_sql = f"{left_sql} {op.name} {right_sql}"
                    sql = f"SELECT name FROM student WHERE {condition_sql};"
                    left_start = sql.index(left_sql)
                    right_start = sql.index(right_sql)
                    left = BinaryExpr(
                        ExprOp.EQ, LiteralExpr(1, DataType.INT, span(sql, "1", start=left_start)),
                        LiteralExpr(value, DataType.INT, span(sql, str(value), start=left_start + 4)),
                        span(sql, "=", start=left_start), span(sql, left_sql),
                    )
                    if code == COLUMN_NOT_FOUND:
                        column = IdentifierExpr("missing_col", span(sql, "missing_col"))
                        literal = LiteralExpr(1, DataType.INT, span(sql, "1", start=right_start))
                        expected_span = column.span
                    else:
                        column = IdentifierExpr("age", span(sql, "age"))
                        literal = LiteralExpr("old", DataType.VARCHAR, span(sql, "'old'"))
                        expected_span = span(sql, "=", start=right_start)
                    right = BinaryExpr(ExprOp.EQ, column, literal,
                                       span(sql, "=", start=right_start), span(sql, right_sql))
                    where = BinaryExpr(op, left, right, span(sql, op.name), span(sql, condition_sql))
                    with self.assertRaises(DbError) as raised:
                        self.semantic.analyze(self._select_with_where(sql, where), self.catalog)
                    self.assertEqual(raised.exception.code, code)
                    self.assertIs(raised.exception.stage, ErrorStage.SEMANTIC)
                    self.assertEqual(raised.exception.span, expected_span)
                    self.assertEqual(self.catalog.list_tables(), [STUDENT_TABLE])

    def test_expression_reports_left_error_before_right_error(self):
        """两侧都有缺列时先报告左侧，保证检查顺序和报错位置稳定。"""
        from minidb.compiler.ast import BinaryExpr, IdentifierExpr, LiteralExpr

        sql = "SELECT name FROM student WHERE missing_left = 1 AND missing_right = 1;"
        comparisons = []
        for name in ("missing_left", "missing_right"):
            start = sql.index(name)
            comparisons.append(BinaryExpr(
                ExprOp.EQ, IdentifierExpr(name, span(sql, name)),
                LiteralExpr(1, DataType.INT, span(sql, "1", start=start)),
                span(sql, "=", start=start), span(sql, f"{name} = 1"),
            ))
        where = BinaryExpr(ExprOp.AND, *comparisons, span(sql, "AND"),
                           span(sql, "missing_left = 1 AND missing_right = 1"))
        with self.assertRaises(DbError) as raised:
            self.semantic.analyze(self._select_with_where(sql, where), self.catalog)
        self.assertEqual(raised.exception.code, COLUMN_NOT_FOUND)
        self.assertEqual(raised.exception.context["column_name"], "missing_left")
        self.assertEqual(raised.exception.span, span(sql, "missing_left"))

    def test_nested_not_binds_comparison_inside_out(self):
        """NOT NOT age >= 18 保留两层 NOT，最内层仍按原表索引绑定 age。"""
        from minidb.compiler.ast import BinaryExpr, IdentifierExpr, LiteralExpr, UnaryExpr

        sql = "SELECT name FROM student WHERE NOT NOT age >= 18;"
        first_not = sql.index("NOT")
        second_not = sql.index("NOT", first_not + 3)
        comparison = BinaryExpr(
            ExprOp.GE, IdentifierExpr("age", span(sql, "age")),
            LiteralExpr(18, DataType.INT, span(sql, "18")), span(sql, ">="), span(sql, "age >= 18"),
        )
        inner = UnaryExpr(ExprOp.NOT, comparison, span(sql, "NOT", start=second_not),
                          span(sql, "NOT age >= 18"))
        outer = UnaryExpr(ExprOp.NOT, inner, span(sql, "NOT", start=first_not),
                          span(sql, "NOT NOT age >= 18"))
        bound = self.semantic.analyze(self._select_with_where(sql, outer), self.catalog)
        self.assertIsInstance(bound.predicate, BoundUnary)
        self.assertIsInstance(bound.predicate.operand, BoundUnary)
        self.assertIs(bound.predicate.data_type, DataType.BOOL)
        self.assertIs(bound.predicate.operand.data_type, DataType.BOOL)
        self.assertEqual(bound.predicate.operand.operand.left.index, 2)
        self.assertEqual(bound.predicate.op_span, span(sql, "NOT", start=first_not))
        self.assertEqual(bound.predicate.operand.op_span, span(sql, "NOT", start=second_not))
        self.assertEqual(Planner().build(bound).child.predicate, bound.predicate)

    def test_nested_not_reports_invalid_inner_operand(self):
        """NOT NOT age 的内层输入是 INT，错误应指向内层 NOT。"""
        from minidb.compiler.ast import IdentifierExpr, UnaryExpr

        sql = "SELECT name FROM student WHERE NOT NOT age;"
        first_not = sql.index("NOT")
        second_not = sql.index("NOT", first_not + 3)
        inner = UnaryExpr(ExprOp.NOT, IdentifierExpr("age", span(sql, "age")),
                          span(sql, "NOT", start=second_not), span(sql, "NOT age"))
        outer = UnaryExpr(ExprOp.NOT, inner, span(sql, "NOT", start=first_not),
                          span(sql, "NOT NOT age"))
        with self.assertRaises(DbError) as raised:
            self.semantic.analyze(self._select_with_where(sql, outer), self.catalog)
        self.assertEqual(raised.exception.code, TYPE_MISMATCH)
        self.assertIs(raised.exception.stage, ErrorStage.SEMANTIC)
        self.assertEqual(raised.exception.span, inner.op_span)
        self.assertEqual(raised.exception.context["actual"], "INT")

    def test_four_statement_contracts(self):
        """四类真实 AST 均能生成相应 Bound 和合法计划，并保持目录不变。"""
        for build in (create_case, insert_case, select_case, delete_case):
            with self.subTest(case=build.__name__):
                bound = self.semantic.analyze(build(), self.catalog)
                plan = Planner().build(bound)
                self.assertEqual(plan.span, bound.span)
        self.assertEqual(self.catalog.list_tables(), [STUDENT_TABLE])

    def test_shared_ast_is_checked_and_bound_once(self):
        """手工共享 AST 只检查、绑定各节点一次；不作为 Parser 输出正确的证据。"""
        from minidb.compiler.ast import BinaryExpr, IdentifierExpr, LiteralExpr
        from minidb.compiler._ast_validation import validate_ast
        from minidb.compiler._checks import Check
        from minidb.compiler.semantic import _column

        sql = "SELECT name FROM student WHERE age >= 18 AND age >= 18;"
        where_span = span(sql, "age >= 18 AND age >= 18")
        shared = BinaryExpr(
            ExprOp.GE, IdentifierExpr("age", span(sql, "age")),
            LiteralExpr(18, DataType.INT, span(sql, "18")), span(sql, ">="), span(sql, "age >= 18"),
        )
        for _ in range(10):
            shared = BinaryExpr(ExprOp.AND, shared, shared, span(sql, "AND"), where_span)
        stmt = self._select_with_where(sql, shared)
        # 保留 Check.value 的真实执行，只统计同一个字面量被检查几次。
        with patch.object(Check, "value", autospec=True, side_effect=Check.value) as check_value:
            validate_ast(stmt)
        self.assertEqual(check_value.call_count, 1)
        with patch("minidb.compiler.semantic._column", wraps=_column) as lookup, \
             patch("minidb.compiler.semantic._operation_type", wraps=_operation_type) as operation:
            bound = self.semantic.analyze(stmt, self.catalog)
        # 一次选择列 name，一次条件列 age；同一子树的多条引用不会重新按列名查找。
        self.assertEqual(lookup.call_count, 2)
        self.assertEqual(operation.call_count, 11)
        self.assertIs(bound.predicate.left, bound.predicate.right)

    def test_shared_ast_still_rejects_bad_parent_range_and_cycle(self):
        """先通过的共享分支不能掩盖另一父范围越界或祖先环。"""
        from minidb.compiler.ast import BinaryExpr, UnaryExpr
        from minidb.core.errors import INVALID_ARGUMENT

        stmt = select_case()
        shared = stmt.where
        narrow = shared.right.span
        second = UnaryExpr(ExprOp.NOT, shared, narrow, narrow)
        root = BinaryExpr(ExprOp.AND, shared, second, shared.op_span, shared.span)
        for field in ("expression.span", "expression"):
            if field == "expression":
                # frozen 对象正常不能改写，这里故意制造一条回到根节点的坏引用。
                object.__setattr__(root, "right", root)
            with self.subTest(field=field):
                with self.assertRaises(DbError) as raised:
                    self.semantic.analyze(replace(stmt, where=root), self.catalog)
                self.assertEqual(raised.exception.code, INVALID_ARGUMENT)
                self.assertIs(raised.exception.stage, ErrorStage.SEMANTIC)
                self.assertEqual(raised.exception.context["field"], field)

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
