"""SQL 文本至真实磁盘的集成测试；装配辅助函数不是正式 CLI/Session。"""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from minidb.compiler.lexer import Lexer
from minidb.compiler.parser import Parser
from minidb.compiler.semantic import Semantic
from minidb.compiler.planner import Planner
from minidb.core import errors
from minidb.core.source import SourceText
from minidb.engine.context import ExecutionContext
from minidb.engine.executor import Executor
from tests.test_real_storage_integration import open_session


def execute_sql(storage, catalog, text):
    """逐条绑定到最新目录，写语句完成后同步；错误直接交给测试调用者。"""
    results = []
    source = SourceText('<integration>', text)
    for statement in Parser().iter_statements(Lexer().scan(source)):
        bound = Semantic().analyze(statement, catalog)
        plan = Planner().build(bound)
        result = Executor().execute(plan, ExecutionContext(catalog, storage))
        if result.affected_rows is not None:
            storage.sync()
        results.append(result)
    return results


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
                fm, buffer, storage, catalog = open_session(path, policy)
                try:
                    result = execute_sql(storage, catalog, sql)
                    self.assertEqual(len(result), 7)
                    self.assertEqual([r.affected_rows for r in result], [0, 1, 1, 1, None, 2, None])
                    self.assertEqual(result[4].rows, [("小明;O'Brien", 1, 1)])
                    self.assertEqual([c.name for c in result[4].columns], ['name', 'id', 'id'])
                    self.assertEqual(result[6].rows, [(1, "小明;O'Brien", 20)])
                    self.assertEqual(storage.active_scan_count, 0)
                    storage.close()
                    fm, buffer, storage, catalog = open_session(path, policy)
                    self.assertEqual(execute_sql(storage, catalog, 'SELECT name FROM student;')[0].rows,
                                     [("小明;O'Brien",)])
                    storage.close()
                finally:
                    storage.abort()

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
            fm, buffer, storage, catalog = open_session(path)
            try:
                execute_sql(storage, catalog, 'CREATE TABLE student(id INT, name VARCHAR, age INT);')
                for sql, code in cases:
                    with self.subTest(sql=sql):
                        before = path.read_bytes()
                        stats = buffer.stats()
                        with self.assertRaises(errors.DbError) as caught:
                            execute_sql(storage, catalog, sql)
                        self.assertEqual(caught.exception.code, code)
                        self.assertIsNotNone(caught.exception.span)
                        self.assertEqual(path.read_bytes(), before)
                        self.assertEqual(buffer.stats(), stats)
                execute_sql(storage, catalog, "INSERT INTO student(id,name,age) VALUES (1, 'valid', 20);")
                self.assertEqual(execute_sql(storage, catalog, 'SELECT * FROM student;')[0].rows,
                                 [(1, 'valid', 20)])
                storage.close()
            finally:
                storage.abort()

    def test_sql_across_processes_with_cross_page_delete_and_reuse(self):
        program = '''
import json, sys
from test_sql_disk_integration import execute_sql, open_session
fm, buffer, storage, catalog = open_session(sys.argv[1], sys.argv[2])
try:
    sql = sys.stdin.read()
    results = execute_sql(storage, catalog, sql)
    output = [{'rows': result.rows, 'affected': result.affected_rows} for result in results]
    storage.close()
    print(json.dumps(output))
finally:
    storage.abort()
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
                inserts = ''.join("INSERT INTO student(id,name,age) VALUES (%d, '%s', 20);" % (i, chr(65+i)*3000)
                                  for i in range(3))
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
