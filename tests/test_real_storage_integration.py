"""真实 Session、目录、数据页、缓存与文件的集成测试。"""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from minidb.cli.session import Session
from minidb.core import errors
from minidb.core.disk_types import INVALID_PAGE_ID
from minidb.storage.data_page import DataPage
from tests.fakes.file_bytes import read_file_bytes


def open_session(path, policy="lru", *, buffer_pages=1):
    """测试统一使用生产 Session 装配，不再绕过 CatalogServices/guard。"""
    return Session.open(str(path), buffer_pages=buffer_pages, policy=policy)


def create_table(session):
    session.execute_text(
        "CREATE TABLE student(id INT, name VARCHAR, age INT);"
    )
    return session.catalog.find_table("student")


def insert_student(session, row):
    session.execute_text(
        "INSERT INTO student(id, name, age) VALUES "
        f"({row[0]}, '{row[1]}', {row[2]});"
    )


class RealStorageIntegrationTests(unittest.TestCase):
    def test_consecutive_reclaim_reuse_and_root_reset_for_both_policies(self):
        for policy in ("lru", "fifo"):
            with self.subTest(policy=policy), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "real.db"
                session = open_session(path, policy)
                try:
                    table = create_table(session)
                    rows = [
                        (index, character * 1000, 20)
                        for index, character in enumerate(("甲", "乙", "丙", "丁"))
                    ]
                    for row in rows:
                        insert_student(session, row)
                    records = list(session.storage.scan_rows(table))
                    ids = [record.row_id for record in records]
                    self.assertEqual(len({row_id.page_id for row_id in ids}), 4)
                    size = path.stat().st_size

                    for row_id, row in zip(ids[1:3], rows[1:3]):
                        result = session.execute_text(
                            f"DELETE FROM student WHERE id = {row[0]};"
                        )[0]
                        self.assertEqual(result.affected_rows, 1)
                    self.assertEqual(
                        [record.values for record in session.storage.scan_rows(table)],
                        [rows[0], rows[3]],
                    )

                    replacement = (9, "新" * 1000, 30)
                    insert_student(session, replacement)
                    replacement_record = next(
                        record
                        for record in list(session.storage.scan_rows(table))
                        if record.values[0] == replacement[0]
                    )
                    self.assertEqual(replacement_record.row_id.page_id, ids[2].page_id)
                    self.assertEqual(path.stat().st_size, size)
                finally:
                    session.close()

                reopened = open_session(path, policy)
                try:
                    table = reopened.catalog.find_table("student")
                    records = list(reopened.storage.scan_rows(table))
                    self.assertEqual(
                        [record.values for record in records],
                        [rows[0], rows[3], replacement],
                    )
                    deleted = reopened.execute_text("DELETE FROM student;")[0]
                    self.assertEqual(deleted.affected_rows, 3)
                    root = DataPage(
                        reopened.buffer_pool.get_page(table.ref.root_page_id),
                        page_id=table.ref.root_page_id,
                        expected_table_id=table.ref.table_id,
                    )
                    self.assertEqual(root.header.slot_count, 0)
                    self.assertEqual(root.header.next_page_id, INVALID_PAGE_ID)

                    insert_student(reopened, (10, "root reused", 40))
                    new_record = list(reopened.storage.scan_rows(table))[0]
                    self.assertEqual(new_record.row_id.page_id, table.ref.root_page_id)
                finally:
                    reopened.close()

    def test_real_insert_maximum_and_oversize_before_any_page_write(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "boundary.db"
            session = open_session(path)
            try:
                session.execute_text(
                    "CREATE TABLE payload(a VARCHAR, b VARCHAR, "
                    "c VARCHAR, d VARCHAR);"
                )
                valid = tuple(character * 1000 for character in "abcd")
                session.execute_text(
                    "INSERT INTO payload(a,b,c,d) VALUES "
                    f"('{valid[0]}','{valid[1]}','{valid[2]}','{valid[3]}');"
                )
                before = read_file_bytes(session.file_manager)
                oversized = tuple(character * 1024 for character in "wxyz")
                with patch.object(
                    session.buffer_pool,
                    "new_page",
                    wraps=session.buffer_pool.new_page,
                ) as allocate, patch.object(
                    session.buffer_pool,
                    "write_if_current",
                    wraps=session.buffer_pool.write_if_current,
                ) as write:
                    with self.assertRaises(errors.DbError) as caught:
                        session.execute_text(
                            "INSERT INTO payload(a,b,c,d) VALUES "
                            f"('{oversized[0]}','{oversized[1]}',"
                            f"'{oversized[2]}','{oversized[3]}');"
                        )
                    self.assertEqual(caught.exception.code, errors.ROW_TOO_LARGE)
                    allocate.assert_not_called()
                    write.assert_not_called()
                self.assertEqual(read_file_bytes(session.file_manager), before)
            finally:
                session.close()

    def test_failed_real_writeback_closes_without_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            session = open_session(Path(directory) / "failure.db")
            create_table(session)
            failure = errors.DbError(
                errors.ErrorStage.STORAGE,
                errors.IO_WRITE_FAILED,
                "injected",
                context={"operation": "write_page"},
            )
            file_manager = session.file_manager
            with patch.object(
                file_manager, "_write_raw", side_effect=failure
            ) as write:
                with self.assertRaises(errors.DbError) as caught:
                    insert_student(session, (1, "pending", 20))
                self.assertEqual(caught.exception.code, errors.COMMIT_OUTCOME_UNKNOWN)
                session.abort()
                session.abort()
                self.assertEqual(write.call_count, 1)
            self.assertTrue(session.is_closed)
            self.assertEqual(session.storage.active_scan_count, 0)

    def test_new_process_restores_catalog_rows_and_free_list(self):
        common = r'''
import json, sys
from minidb.cli.session import Session
session = Session.open(sys.argv[1], buffer_pages=1, policy=sys.argv[2])
'''
        writer = common + r'''
try:
    session.execute_text('CREATE TABLE student(id INT, name VARCHAR, age INT);')
    rows = [(1, '中' * 900, 20), (2, '乙' * 1000, 21), (3, '丙' * 1000, 22)]
    for row in rows:
        session.execute_text("INSERT INTO student(id,name,age) VALUES (%d,'%s',%d);" % row)
    table = session.catalog.find_table('student')
    records = list(session.storage.scan_rows(table))
    session.execute_text('DELETE FROM student WHERE id = 2;')
    session.close()
    print(json.dumps({'root': table.ref.root_page_id, 'freed': records[1].row_id.page_id,
                      'rows': [rows[0], rows[2]]}))
finally:
    session.abort()
'''
        reader = common + r'''
try:
    table = session.catalog.find_table('student')
    rows = [record.values for record in session.storage.scan_rows(table)]
    size = __import__('os').path.getsize(sys.argv[1])
    session.execute_text("INSERT INTO student(id,name,age) VALUES (4,'%s',23);" % ('丁' * 1000))
    record = next(row for row in list(session.storage.scan_rows(table)) if row.values[0] == 4)
    assert __import__('os').path.getsize(sys.argv[1]) == size
    session.close()
    print(json.dumps({'root': table.ref.root_page_id, 'reused': record.row_id.page_id,
                      'rows': rows}))
finally:
    session.abort()
'''
        verifier = common + r'''
try:
    table = session.catalog.find_table('student')
    print(json.dumps([record.values for record in session.storage.scan_rows(table)]))
    session.close()
finally:
    session.abort()
'''
        with tempfile.TemporaryDirectory() as directory:
            for policy in ("lru", "fifo"):
                with self.subTest(policy=policy):
                    path = Path(directory) / (policy + ".db")

                    def run(code):
                        result = subprocess.run(
                            [sys.executable, "-B", "-c", code, str(path), policy],
                            cwd=Path(__file__).resolve().parents[1],
                            capture_output=True,
                            text=True,
                            encoding="utf-8",
                            env={
                                **__import__("os").environ,
                                "PYTHONIOENCODING": "utf-8",
                            },
                            timeout=30,
                        )
                        self.assertEqual(result.returncode, 0, result.stderr)
                        return json.loads(result.stdout)

                    first, second = run(writer), run(reader)
                    self.assertEqual(second["root"], first["root"])
                    self.assertEqual(second["rows"], first["rows"])
                    self.assertEqual(second["reused"], first["freed"])
                    self.assertEqual(
                        run(verifier),
                        first["rows"] + [[4, "丁" * 1000, 23]],
                    )


if __name__ == "__main__":
    unittest.main()
