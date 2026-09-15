"""Session 调度测试：验证 CLI 会话与真实编译、目录、存储链路的配合。"""

import tempfile
import unittest
from pathlib import Path

from minidb.cli.session import Session, _explain_result
from minidb.compiler.bound import BoundBinary, BoundColumn, BoundLiteral
from minidb.compiler.optimizer import Optimizer
from minidb.compiler.plan import FilterPlan, IndexScanPlan, ProjectPlan
from minidb.core.errors import (
    COLUMN_NOT_FOUND,
    INPUT_INVALID_UTF8,
    UNEXPECTED_TOKEN,
    DbError,
)
from minidb.core.expressions import ExprOp
from minidb.core.result import ResultColumn
from minidb.core.schema import (
    ColumnDef,
    DataType,
    IndexDef,
    IndexOrigin,
    Schema,
    TableDef,
    TableRef,
    TypeSpec,
)
from minidb.core.source import SourcePos, SourceSpan


class SessionTests(unittest.TestCase):
    def test_execute_text_runs_the_full_sql_pipeline_and_syncs_writes(self):
        # 测试 session.py 的 Lexer→Parser→Semantic→Planner→Executor 调度，
        # 并验证 CREATE/INSERT/DELETE 成功后执行同步。
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.db"
            session = Session.open(str(path), buffer_pages=2, policy="fifo")
            try:
                results = session.execute_text(
                    "CREATE TABLE student(id INT, name VARCHAR, age INT);"
                    "INSERT INTO student(id,name,age) VALUES (1,'Alice',20);"
                    "INSERT INTO student(id,name,age) VALUES (2,'Bob',17);"
                    "SELECT name,id FROM student WHERE age >= 18;"
                    "DELETE FROM student WHERE id = 2;"
                    "SELECT * FROM student;",
                    materialize=True,
                )
                self.assertEqual(len(results), 6)
                self.assertEqual(results[3].rows, [("Alice", 1)])
                self.assertEqual(results[4].affected_rows, 1)
                self.assertEqual(results[5].rows, [(1, "Alice", 20)])
            finally:
                session.close()

            # 重新打开同一文件，确认 Session 的 sync/close 后目录和数据可恢复。
            reopened = Session.open(str(path), buffer_pages=2, policy="fifo")
            try:
                result = reopened.execute_text("SELECT name FROM student;", materialize=True)[0]
                self.assertEqual(result.rows, [("Alice",)])
            finally:
                reopened.close()

    def test_first_error_stops_text_and_does_not_execute_following_statement(self):
        # 测试 session.py 遇到语义错误立即停止；后续 INSERT 不应执行。
        with tempfile.TemporaryDirectory() as directory:
            session = Session.open(str(Path(directory) / "errors.db"))
            try:
                session.execute_text("CREATE TABLE student(id INT);")
                with self.assertRaises(DbError) as raised:
                    session.execute_text(
                        "SELECT missing FROM student;"
                        "INSERT INTO student(id) VALUES (1);",
                        materialize=True,
                    )
                self.assertEqual(raised.exception.code, COLUMN_NOT_FOUND)
                self.assertEqual(
                    session.execute_text("SELECT * FROM student;", materialize=True)[0].rows,
                    [],
                )
            finally:
                session.close()

    def test_file_input_reports_utf8_errors_and_syntax_check_has_no_database(self):
        # 测试 execute_file() 的 UTF-8 错误分类，以及静态语法检查不创建数据库文件。
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.db"
            self.assertEqual(
                Session.check_syntax("SELECT FROM t; SELECT * FROM t;").valid_statement_count,
                1,
            )
            self.assertFalse(path.exists())

            session = Session.open(str(path))
            try:
                bad_file = Path(directory) / "bad.sql"
                bad_file.write_bytes(b"SELECT \xff;")
                with self.assertRaises(DbError) as raised:
                    session.execute_file(str(bad_file))
                self.assertEqual(raised.exception.code, INPUT_INVALID_UTF8)
            finally:
                session.close()

    def test_syntax_error_is_reported_without_aborting_session(self):
        # 测试可继续的语法错误：错误语句被丢弃，会话仍可执行下一次提交。
        with tempfile.TemporaryDirectory() as directory:
            session = Session.open(str(Path(directory) / "syntax.db"))
            try:
                with self.assertRaises(DbError) as raised:
                    session.execute_text("SELECT FROM student;")
                self.assertEqual(raised.exception.code, UNEXPECTED_TOKEN)
                self.assertFalse(session.is_closed)
            finally:
                session.abort()

    def test_describe_reports_fixed_columns_without_writing_pages(self):
        # 测试工作计划 7.3：DESCRIBE 输出固定六列，只读目录、不写页。
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "describe.db"
            session = Session.open(str(path))
            try:
                session.execute_text(
                    "CREATE TABLE student("
                    "id INT PRIMARY KEY, name VARCHAR(64) NOT NULL,"
                    "email VARCHAR(128) UNIQUE, active BOOL DEFAULT TRUE,"
                    "birthday DATE, balance DECIMAL(12,2) DEFAULT 0.00);"
                )
                before = (path.stat().st_size, path.stat().st_mtime_ns)

                result = session.execute_text("DESCRIBE student;", materialize=True)[0]

                self.assertEqual(
                    [column.name for column in result.columns],
                    ["name", "type", "nullable", "primary_key", "unique", "default"],
                )
                self.assertEqual(result.rows, [
                    ("id", "INT", False, True, True, None),
                    ("name", "VARCHAR(64)", False, False, False, None),
                    ("email", "VARCHAR(128)", True, False, True, None),
                    ("active", "BOOL", True, False, False, "TRUE"),
                    ("birthday", "DATE", True, False, False, None),
                    ("balance", "DECIMAL(12,2)", True, False, False, "0.00"),
                ])
                self.assertEqual((path.stat().st_size, path.stat().st_mtime_ns), before)
                self.assertFalse(session.is_closed)
            finally:
                session.close()

    def test_explain_prints_plan_nodes_without_executing_the_inner_statement(self):
        # 测试工作计划 7.3 与验收 S08：EXPLAIN 每行一个可读节点，不写页，
        # 也不执行内部语句；没有可用索引时如实显示 SeqScan。
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "explain.db"
            session = Session.open(str(path))
            try:
                session.execute_text(
                    "CREATE TABLE student(id INT, name VARCHAR(64));"
                    "INSERT INTO student(id,name) VALUES (1,'Alice');"
                )
                before = (path.stat().st_size, path.stat().st_mtime_ns)

                select_plan = session.execute_text(
                    "EXPLAIN SELECT name FROM student WHERE id = 1;", materialize=True
                )[0]
                insert_plan = session.execute_text(
                    "EXPLAIN INSERT INTO student(id,name) VALUES (2,'Bob');",
                    materialize=True,
                )[0]

                self.assertEqual(
                    [column.name for column in select_plan.columns], ["plan"]
                )
                self.assertEqual(select_plan.rows, [
                    ("Project",),
                    ("  Filter (id = 1)",),
                    ("    SeqScan student",),
                ])
                self.assertEqual(insert_plan.rows, [("Insert student",)])

                self.assertEqual((path.stat().st_size, path.stat().st_mtime_ns), before)
                # EXPLAIN 不执行内部语句：INSERT 只被展示，没有真的写入。
                self.assertEqual(
                    session.execute_text(
                        "SELECT id,name FROM student;", materialize=True
                    )[0].rows,
                    [(1, "Alice")],
                )
                self.assertFalse(session.is_closed)
            finally:
                session.close()

    def test_explain_shows_index_scan_and_bounds_when_an_index_is_usable(self):
        # 工作计划 7.3：索引可用时必须如实显示 IndexScan 及边界。当前
        # CREATE INDEX 尚未接通，只能构造计划验证 EXPLAIN 的渲染规则。
        span = SourceSpan(SourcePos(1, 1, 0), SourcePos(1, 40, 39), "<explain>")
        int_spec, bool_spec = TypeSpec(DataType.INT), TypeSpec(DataType.BOOL)
        table = TableDef(
            TableRef(1, "student", 3),
            Schema((ColumnDef("id", int_spec, nullable=False),)),
        )
        index = IndexDef(1, "ix_id", 1, 0, 4, False, IndexOrigin.USER)
        bound = BoundLiteral(7, int_spec, span)
        predicate = BoundBinary(
            ExprOp.EQ,
            BoundColumn(0, int_spec, span, False),
            bound,
            bool_spec,
            span,
            span,
            False,
        )
        scan = IndexScanPlan(
            table, index, True, bound, True, True, bound, True, False, span
        )
        plan = ProjectPlan(
            FilterPlan(scan, predicate, span),
            (0,),
            (ResultColumn("id", DataType.INT),),
            span,
        )

        result = _explain_result(plan, (index,), Optimizer())

        self.assertEqual(result.rows, [
            ("Project",),
            ("  Filter (id = 7)",),
            ("    IndexScan ix_id student(id) = 7",),
        ])


if __name__ == "__main__":
    unittest.main()
