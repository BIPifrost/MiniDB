"""Session 流式结果测试（工作计划 7.4 / 9.2）。

覆盖 D4 要求：SELECT 惰性返回 ResultCursor、活动游标期间的写与关闭保护、
execute_text 兼容入口的显式 materialize 开关和 10000 行 / 16 MiB 上限。
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from minidb.cli import session as session_module
from minidb.cli.session import Session
from minidb.core.errors import ACTIVE_SCAN, INVALID_ARGUMENT, RESOURCE_LIMIT, DbError
from minidb.core.result import QueryResult, ResultCursor


def _seed(session: Session) -> None:
    session.execute_text(
        "CREATE TABLE student(id INT, name VARCHAR, age INT);"
        "INSERT INTO student(id,name,age) VALUES (1,'Alice',20);"
        "INSERT INTO student(id,name,age) VALUES (2,'Bob',17);"
        "INSERT INTO student(id,name,age) VALUES (3,'Cara',21);"
    )


class SessionStreamingTests(unittest.TestCase):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        path = Path(self._directory.name) / "stream.db"
        self.session = Session.open(str(path), buffer_pages=4)
        self.addCleanup(self._close_quietly)
        _seed(self.session)

    def _close_quietly(self):
        try:
            if self.session._active_cursor is not None:
                self.session._active_cursor.close()
            self.session.abort()
        except BaseException:
            pass

    def test_select_returns_lazy_cursor_and_streams_rows(self):
        stream = self.session.iter_results("SELECT name FROM student WHERE age >= 18;")
        cursor = next(stream)
        self.assertIsInstance(cursor, ResultCursor)
        self.assertEqual([column.name for column in cursor.columns], ["name"])
        self.assertEqual(next(cursor), ("Alice",))
        self.assertFalse(cursor.closed)
        self.assertEqual(list(cursor), [("Cara",)])
        self.assertTrue(cursor.closed)
        with self.assertRaises(StopIteration):
            next(stream)

    def test_active_cursor_blocks_write_and_normal_close(self):
        stream = self.session.iter_results("SELECT * FROM student;")
        cursor = next(stream)
        try:
            with self.assertRaises(DbError) as raised:
                self.session.execute_text(
                    "INSERT INTO student(id,name,age) VALUES (9,'Zed',1);"
                )
            self.assertEqual(raised.exception.code, ACTIVE_SCAN)

            with self.assertRaises(DbError) as closed:
                self.session.close()
            self.assertEqual(closed.exception.code, ACTIVE_SCAN)
        finally:
            cursor.close()
            stream.close()

        # 游标关闭后写语句和正常关闭都恢复可用。
        self.session.execute_text("INSERT INTO student(id,name,age) VALUES (9,'Zed',1);")
        self.assertEqual(
            self.session.execute_text("SELECT id FROM student;", materialize=True)[0].rows,
            [(1,), (2,), (3,), (9,)],
        )
        self.assertFalse(self.session.is_closed)

    def test_second_stream_is_rejected_while_cursor_active(self):
        stream = self.session.iter_results("SELECT * FROM student;")
        cursor = next(stream)
        try:
            with self.assertRaises(DbError) as raised:
                self.session.iter_results("SELECT * FROM student;")
            self.assertEqual(raised.exception.code, ACTIVE_SCAN)
        finally:
            cursor.close()
            stream.close()

    def test_closing_the_generator_releases_the_cursor(self):
        stream = self.session.iter_results(
            "SELECT * FROM student;INSERT INTO student(id,name,age) VALUES (7,'Gil',3);"
        )
        cursor = next(stream)
        stream.close()
        self.assertTrue(cursor.closed)
        self.assertIsNone(self.session._active_cursor)
        # 生成器关闭后第一条 SELECT 的后续语句不应再执行。
        self.assertEqual(
            self.session.execute_text("SELECT id FROM student;", materialize=True)[0].rows,
            [(1,), (2,), (3,)],
        )

    def test_mixed_statements_yield_results_in_order(self):
        stream = self.session.iter_results(
            "INSERT INTO student(id,name,age) VALUES (4,'Dan',30);"
            "SELECT id FROM student WHERE id = 4;"
        )
        update = next(stream)
        self.assertIsInstance(update, QueryResult)
        self.assertEqual(update.affected_rows, 1)
        cursor = next(stream)
        self.assertEqual(list(cursor), [(4,)])
        with self.assertRaises(StopIteration):
            next(stream)

    def test_execute_text_requires_explicit_materialize_for_select(self):
        with self.assertRaises(DbError) as raised:
            self.session.execute_text("SELECT * FROM student;")
        self.assertEqual(raised.exception.code, INVALID_ARGUMENT)
        self.assertIn("iter_results", str(raised.exception))
        # 拒绝物化不会破坏会话，也不会残留活动游标。
        self.assertIsNone(self.session._active_cursor)
        self.assertEqual(
            self.session.execute_text("SELECT id FROM student;", materialize=True)[0].rows,
            [(1,), (2,), (3,)],
        )

    def test_materialize_rejects_non_bool_flag(self):
        with self.assertRaises(DbError) as raised:
            self.session.execute_text("SELECT * FROM student;", materialize="yes")
        self.assertEqual(raised.exception.code, INVALID_ARGUMENT)

    def test_materialize_row_limit_is_enforced(self):
        with patch.object(session_module, "MATERIALIZE_MAX_ROWS", 2):
            with self.assertRaises(DbError) as raised:
                self.session.execute_text("SELECT id FROM student;", materialize=True)
        self.assertEqual(raised.exception.code, RESOURCE_LIMIT)
        self.assertEqual(raised.exception.context["kind"], "rows")
        self.assertEqual(raised.exception.context["limit"], 2)
        # 超限后游标已关闭，会话仍可继续使用。
        self.assertIsNone(self.session._active_cursor)
        self.assertEqual(
            self.session.execute_text("SELECT id FROM student;", materialize=True)[0].rows,
            [(1,), (2,), (3,)],
        )

    def test_materialize_byte_limit_is_enforced(self):
        with patch.object(session_module, "MATERIALIZE_MAX_BYTES", 4):
            with self.assertRaises(DbError) as raised:
                self.session.execute_text("SELECT name FROM student;", materialize=True)
        self.assertEqual(raised.exception.code, RESOURCE_LIMIT)
        self.assertEqual(raised.exception.context["kind"], "bytes")
        self.assertIsNone(self.session._active_cursor)

    def test_use_after_close_is_reported(self):
        path = Path(self._directory.name) / "closed.db"
        session = Session.open(str(path))
        session.close()
        with self.assertRaises(DbError) as raised:
            session.iter_results("SELECT * FROM student;")
        self.assertEqual(raised.exception.code, "CLOSED")


if __name__ == "__main__":
    unittest.main()
