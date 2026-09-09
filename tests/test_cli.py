"""CLI 入口测试：验证 main.py 与 Session 的参数和输出边界。"""

import io
import tempfile
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path

from minidb.cli.main import main


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


if __name__ == "__main__":
    unittest.main()
