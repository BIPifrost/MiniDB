"""SQL 文本经正式 Session 落盘的集成测试。"""
from tests.fakes.file_bytes import read_file_bytes
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from minidb.core import errors
from tests.test_real_storage_integration import open_session


def execute_sql(session, text):
    """通过 Session 逐条绑定、事务化执行并物化测试查询结果。"""
    return session.execute_text(text, materialize=True)


class SqlDiskIntegrationTests(unittest.TestCase):
    def test_multi_statement_create_insert_filter_project_delete(self):
        sql = """
        -- 建表之后，后续语句必须看到更新的真实目录。
        CREATE TABLE student(id INT, name VARCHAR, age INT);
        INSERT INTO student(name, age, id) VALUES ('小明;O''Brien', 20, 1);
        INSERT INTO student(id,name,age) VALUES (2, 'Bob', 17);
        INSERT INTO student(id,name,age) VALUES (3, '小红', 22);
        SELECT name, id, id FROM student WHERE age >= 18 AND NOT id = 3;
        DELETE FROM student WHERE age < 18 OR id = 3;
        SELECT * FROM student;
        """
        for policy in ('lru', 'fifo'):
            with self.subTest(policy=policy), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'sql.db'
                session = open_session(path, policy)
                try:
                    result = execute_sql(session, sql)
                    self.assertEqual(len(result), 7)
                    self.assertEqual([r.affected_rows for r in result], [0, 1, 1, 1, None, 2, None])
                    self.assertEqual(result[4].rows, [("小明;O'Brien", 1, 1)])
                    self.assertEqual([c.name for c in result[4].columns], ['name', 'id', 'id'])
                    self.assertEqual(result[6].rows, [(1, "小明;O'Brien", 20)])
                    self.assertEqual(session.storage.active_scan_count, 0)
                    session.close()
                    session = open_session(path, policy)
                    self.assertEqual(execute_sql(session, 'SELECT name FROM student;')[0].rows,
                                     [("小明;O'Brien",)])
                    session.close()
                finally:
                    session.abort()

    def test_invalid_sql_does_not_modify_file_and_later_statement_works(self):
        cases = [
            ('SELECT missing FROM student;', errors.COLUMN_NOT_FOUND),
            ("INSERT INTO student(id,name,age) VALUES ('bad', 'name', 20);", errors.TYPE_MISMATCH),
            ('SELECT * FROM absent;', errors.TABLE_NOT_FOUND),
            ('CREATE TABLE student(id INT);', errors.TABLE_EXISTS),
            ('SELECT FROM student;', errors.UNEXPECTED_TOKEN),
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'errors.db'
            session = open_session(path)
            try:
                execute_sql(session, 'CREATE TABLE student(id INT, name VARCHAR, age INT);')
                for sql, code in cases:
                    with self.subTest(sql=sql):
                        before = read_file_bytes(session.file_manager)
                        with self.assertRaises(errors.DbError) as caught:
                            execute_sql(session, sql)
                        self.assertEqual(caught.exception.code, code)
                        self.assertIsNotNone(caught.exception.span)
                        self.assertEqual(read_file_bytes(session.file_manager), before)
                execute_sql(session, "INSERT INTO student(id,name,age) VALUES (1, 'valid', 20);")
                self.assertEqual(execute_sql(session, 'SELECT * FROM student;')[0].rows,
                                 [(1, 'valid', 20)])
                session.close()
            finally:
                session.abort()

    def test_sql_across_processes_with_cross_page_delete_and_reuse(self):
        program = '''
import json, sys
from test_sql_disk_integration import execute_sql, open_session
session = open_session(sys.argv[1], sys.argv[2])
try:
    sql = sys.stdin.read()
    results = execute_sql(session, sql)
    output = [{'rows': result.rows, 'affected': result.affected_rows} for result in results]
    session.close()
    print(json.dumps(output))
finally:
    session.abort()
'''
        for policy in ('lru', 'fifo'):
            with self.subTest(policy=policy), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'restart.db'
                def run(sql):
                    result = subprocess.run([sys.executable, '-B', '-c',
                                             'import sys; sys.path.insert(0, "tests")\n' + program,
                                             str(path), policy], input=sql, encoding='utf-8',
                                            env={**__import__('os').environ, 'PYTHONIOENCODING': 'utf-8'},
                                            cwd=Path(__file__).resolve().parents[1],
                                            capture_output=True, timeout=30)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    return json.loads(result.stdout)
                inserts = ''.join(
                    "INSERT INTO student(id,name,age) VALUES (%d, '%s', 20);"
                    % (index, character * 1000)
                    for index, character in enumerate(("甲", "乙", "丙"))
                )
                run('CREATE TABLE student(id INT, name VARCHAR, age INT);' + inserts)
                size = path.stat().st_size
                changed = run("DELETE FROM student WHERE id = 1; INSERT INTO student(id,name,age) VALUES (4, '%s', 30);"
                              " SELECT id, age FROM student;" % ('中' * 900))
                self.assertEqual(changed[0]['affected'], 1)
                self.assertEqual(changed[1]['affected'], 1)
                self.assertEqual(changed[2]['rows'], [[0, 20], [2, 20], [4, 30]])
                self.assertEqual(path.stat().st_size, size)
                read = run('SELECT name FROM student WHERE id = 4; SELECT id FROM student;')
                self.assertEqual(read[0]['rows'], [['中' * 900]])
                self.assertEqual(read[1]['rows'], [[0], [2], [4]])


if __name__ == '__main__':
    unittest.main()
