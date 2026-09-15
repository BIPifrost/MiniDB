"""CLI 入口测试：验证 main.py 与 Session 的参数和输出边界。"""

import io
import tempfile
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path

from minidb.cli.main import _emit_cursor, _run_script, main
from minidb.core.errors import PAGE_CORRUPTED, DbError, ErrorStage
from minidb.core.result import ResultColumn, ResultCursor
from minidb.core.schema import DataType


@contextmanager
def redirect_stdin(stream):
    """兼容当前测试运行时的标准输入替换。"""
    import sys

    original = sys.stdin
    sys.stdin = stream
    try:
        yield
    finally:
        sys.stdin = original


class CliTests(unittest.TestCase):
    def test_syntax_check_does_not_create_database(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sql = root / "errors.sql"
            db = root / "should-not-exist.db"
            sql.write_text("SELECT FROM t; SELECT * FROM t;", encoding="utf-8", newline="")
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = main(["--syntax-check", "--file", str(sql), "--db", "data/demo.db"])
            self.assertEqual(code, 1)
            self.assertFalse(db.exists())
            self.assertIn('"kind":"SYNTAX_CHECK_RESULT"', stdout.getvalue())
            self.assertIn('"code":"UNEXPECTED_TOKEN"', stderr.getvalue())

    def test_file_mode_executes_sql_and_prints_results(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sql = root / "demo.sql"
            db = root / "demo.db"
            sql.write_text(
                "CREATE TABLE t(id INT); INSERT INTO t(id) VALUES (1); SELECT * FROM t;",
                encoding="utf-8",
                newline="",
            )
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = main(["--db", str(db), "--file", str(sql)])
            self.assertEqual(code, 0)
            self.assertTrue(db.exists())
            self.assertIn("Query OK, 0 rows affected", stdout.getvalue())
            self.assertIn("Query OK, 1 row affected", stdout.getvalue())
            self.assertIn("+----+", stdout.getvalue())
            self.assertEqual(stderr.getvalue(), "")

    def test_invalid_syntax_check_combination_is_rejected(self):
        with self.assertRaises(SystemExit) as raised:
            main(["--syntax-check", "--trace"])
        self.assertEqual(raised.exception.code, 2)

    def test_readable_trace_groups_compilation_stages(self):
        # 测试 main.py 的可读 trace：阶段标签应按编译流程出现，且不输出 JSON 事件。
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sql = root / "trace.sql"
            db = root / "trace.db"
            sql.write_text(
                "CREATE TABLE t(id INT); SELECT * FROM t;",
                encoding="utf-8",
                newline="",
            )
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = main(["--db", str(db), "--file", str(sql), "--trace-readable"])
            self.assertEqual(code, 0)
            output = stderr.getvalue()
            self.assertIn("语句 1 · Token 流", output)
            self.assertIn("语句 1 · AST 语法树", output)
            self.assertIn("语句 1 · 语义检查结果", output)
            self.assertIn("语句 1 · 执行计划", output)
            self.assertIn("语句 1 · 优化后计划", output)
            self.assertIn("KW_CREATE", output)
            self.assertNotIn('{"data"', output)
            self.assertIn("Query OK, 0 rows affected", stdout.getvalue())

    def test_interactive_mode_executes_statements_and_accepts_quit(self):
        # 测试 main.py 的交互循环：分号提交、结果显示和 quit 退出。
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "interactive.db"
            stdin = io.StringIO(
                "CREATE TABLE t(id INT, name VARCHAR);\n"
                "INSERT INTO t(id,name) VALUES (1,'张三');\n"
                "SELECT * FROM t;\n"
                "quit\n"
            )
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdin(stdin), redirect_stdout(stdout), redirect_stderr(stderr):
                code = main(["--db", str(db)])
            self.assertEqual(code, 0)
            self.assertIn("Query OK, 0 rows affected", stdout.getvalue())
            self.assertIn("张三", stdout.getvalue())
            self.assertIn("| id", stdout.getvalue())
            self.assertEqual(stderr.getvalue(), "")

    def test_interactive_semicolon_inside_string_waits_for_real_end(self):
        # 字符串中的分号不能提前提交当前 SQL。
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "interactive-string.db"
            stdin = io.StringIO(
                "CREATE TABLE t(id INT, name VARCHAR);\n"
                "INSERT INTO t(id,name) VALUES (1,'A;\n"
                "B');\n"
                "SELECT name FROM t;\n"
                "exit\n"
            )
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdin(stdin), redirect_stdout(stdout), redirect_stderr(stderr):
                code = main(["--db", str(db)])
            self.assertEqual(code, 0)
            self.assertIn("A;\nB", stdout.getvalue())
            self.assertEqual(stderr.getvalue(), "")

    def test_interactive_error_allows_next_submission(self):
        # 当前提交的错误不会关闭会话；下一次提交仍可执行。
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "interactive-error.db"
            stdin = io.StringIO(
                "SELECT FROM missing;\n"
                "CREATE TABLE t(id INT);\n"
                "exit\n"
            )
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdin(stdin), redirect_stdout(stdout), redirect_stderr(stderr):
                code = main(["--db", str(db)])
            self.assertEqual(code, 0)
            self.assertIn("UNEXPECTED_TOKEN", stderr.getvalue())
            self.assertIn("Query OK, 0 rows affected", stdout.getvalue())

    def test_file_mode_streams_select_rows_and_counts_them(self):
        # SELECT 通过 iter_results 逐行输出，并给出结果行数。
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sql = root / "stream.sql"
            db = root / "stream.db"
            sql.write_text(
                "CREATE TABLE t(id INT, name VARCHAR);"
                "INSERT INTO t(id,name) VALUES (1,'A');"
                "INSERT INTO t(id,name) VALUES (2,'B');"
                "INSERT INTO t(id,name) VALUES (3,'C');"
                "SELECT id,name FROM t;",
                encoding="utf-8",
                newline="",
            )
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = main(["--db", str(db), "--file", str(sql)])
            self.assertEqual(code, 0)
            output = stdout.getvalue()
            self.assertIn("| id | name |", output)
            self.assertIn("| 1  | A    |", output)
            self.assertIn("| 3  | C    |", output)
            self.assertIn("3 rows in set", output)
            self.assertEqual(stderr.getvalue(), "")

    def test_empty_select_prints_empty_set(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sql = root / "empty.sql"
            sql.write_text(
                "CREATE TABLE t(id INT); SELECT id FROM t;",
                encoding="utf-8",
                newline="",
            )
            stdout = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
                code = main(["--db", str(root / "empty.db"), "--file", str(sql)])
            self.assertEqual(code, 0)
            self.assertIn("Empty set", stdout.getvalue())

    def test_stream_failure_reports_incomplete_result_and_nonzero_exit(self):
        # 第 N 行读取失败：已输出行保留，并明确标记 result_complete=false。
        def rows():
            yield (1,)
            yield (2,)
            raise DbError(
                ErrorStage.STORAGE,
                PAGE_CORRUPTED,
                "读取第 3 行时页校验失败",
                context={"operation": "test.stream"},
            )

        cursor = ResultCursor((ResultColumn("id", DataType.INT),), rows())
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = _emit_cursor(cursor, trace=False)

        self.assertEqual(code, 1)
        self.assertTrue(cursor.closed)
        self.assertIn("| 1  |", stdout.getvalue())
        self.assertIn("| 2  |", stdout.getvalue())
        self.assertIn(PAGE_CORRUPTED, stderr.getvalue())
        self.assertIn("result_complete=false", stderr.getvalue())
        self.assertIn("rows_shown=2", stderr.getvalue())

    def test_trace_mode_reports_stream_result_as_json(self):
        def rows():
            yield (1,)
            yield (2,)

        cursor = ResultCursor((ResultColumn("id", DataType.INT),), rows())
        stdout = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
            code = _emit_cursor(cursor, trace=True)
        self.assertEqual(code, 0)
        self.assertIn('"result_complete":true', stdout.getvalue())
        self.assertIn('"row_count":2', stdout.getvalue())

    def test_script_stops_after_incomplete_stream_result(self):
        # 与 execute_text 一致：读取中途失败后不再执行同一输入里的后续语句。
        events = []

        def failing_rows():
            yield (1,)
            raise DbError(
                ErrorStage.STORAGE,
                PAGE_CORRUPTED,
                "读取第 2 行失败",
                context={"operation": "test.stream"},
            )

        def stream():
            try:
                yield ResultCursor((ResultColumn("id", DataType.INT),), failing_rows())
                events.append("second-statement-ran")
                yield QueryResult(affected_rows=1, message="1 row inserted")
            finally:
                events.append("stream-closed")

        class FakeSession:
            def iter_results(self, text, *, source_name, trace_sink):
                return stream()

        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = _run_script(
                FakeSession(), "SELECT 1; INSERT ...;",
                source_name="<test>", sink=None, trace_json=False,
            )

        self.assertEqual(code, 1)
        self.assertEqual(events, ["stream-closed"])
        self.assertIn("result_complete=false", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
