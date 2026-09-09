"""Session 调度测试：验证 CLI 会话与真实编译、目录、存储链路的配合。"""

import tempfile
import unittest
from pathlib import Path

from minidb.cli.session import Session
from minidb.core.errors import (
    COLUMN_NOT_FOUND,
    INPUT_INVALID_UTF8,
    UNEXPECTED_TOKEN,
    DbError,
)


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
                    "SELECT * FROM student;"
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
                result = reopened.execute_text("SELECT name FROM student;")[0]
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
                        "INSERT INTO student(id) VALUES (1);"
                    )
                self.assertEqual(raised.exception.code, COLUMN_NOT_FOUND)
                self.assertEqual(session.execute_text("SELECT * FROM student;")[0].rows, [])
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


if __name__ == "__main__":
    unittest.main()
